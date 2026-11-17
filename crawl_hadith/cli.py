"""Command line entry point."""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from . import db
from .api import ApiError, Client
from .crawl import crawl, ensure_initialized, full_seed


def _parse_ids(value: str) -> list[int]:
    try:
        return [int(part.strip()) for part in value.split(",") if part.strip()]
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"invalid id list: {value}") from exc


def _add_db_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--db",
        default="hadith.db",
        help="SQLite database path (default: hadith.db)",
    )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="crawl-hadith",
        description="Crawl hadith.inoor.ir into SQLite.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    crawl_parser = sub.add_parser("crawl", help="crawl pending hadiths")
    _add_db_argument(crawl_parser)
    crawl_parser.add_argument(
        "--workers", type=int, default=2, help="parallel HTTP workers (default 2)"
    )
    crawl_parser.add_argument(
        "--delay",
        type=float,
        default=0.2,
        help="delay before each request per worker (default 0.2s)",
    )
    crawl_parser.add_argument(
        "--retries", type=int, default=5, help="request attempts before fallback"
    )
    crawl_parser.add_argument(
        "--timeout", type=float, default=60.0, help="request timeout in seconds"
    )
    crawl_parser.add_argument("--limit", type=int, help="maximum rows to process")
    crawl_parser.add_argument(
        "--ids",
        type=_parse_ids,
        help="comma-separated hadith IDs; skips sitemap seeding",
    )
    crawl_parser.add_argument(
        "--full-seed",
        action="store_true",
        help="also enumerate every source book to find IDs missing from the sitemap",
    )
    crawl_parser.add_argument(
        "--enrich-fallback",
        action="store_true",
        help="retry primary endpoints for rows currently using fallback data",
    )

    status_parser = sub.add_parser("status", help="show crawl progress")
    _add_db_argument(status_parser)

    return parser


def _print_status(path: Path, conn) -> None:
    counts = db.counts(conn)
    size = 0
    for candidate in (path, Path(f"{path}-wal")):
        if candidate.exists():
            size += candidate.stat().st_size
    size_mb = size / (1024 * 1024)
    print(f"DB: {path} ({size_mb:.1f} MB)")
    print(
        "rows: "
        + ", ".join(
            f"{table}={counts[table]}"
            for table in (
                "hadiths",
                "translations",
                "explanations",
                "hadith_sanad",
                "ravis",
                "sources",
                "tocs",
                "crawl_status",
            )
        )
    )
    for stage in db.STAGES:
        states = counts[stage]
        print(
            f"{stage}: "
            + ", ".join(f"{state}={states.get(state, 0)}" for state in (
                "done",
                "fallback",
                "skipped",
                "missing",
                "failed",
                "pending",
            ))
        )
    seed_states = {
        row[0]: row[1]
        for row in conn.execute(
            "SELECT status, COUNT(*) FROM source_seed GROUP BY status"
        )
    }
    if seed_states:
        print(
            "source seed: "
            + ", ".join(
                f"{state}={seed_states.get(state, 0)}"
                for state in ("done", "failed", "pending")
            )
        )


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )
    parser = _build_parser()
    args = parser.parse_args(argv)
    db_path = Path(args.db)

    if args.command == "status":
        if not db_path.exists():
            print(f"database not found: {db_path}")
            return 1
        conn = db.connect(db_path)
        try:
            db.ensure_schema(conn)
            _print_status(db_path, conn)
        except RuntimeError as exc:
            print(f"database error: {exc}", file=sys.stderr)
            return 1
        finally:
            conn.close()
        return 0

    if args.command == "crawl":
        conn = db.connect(db_path)
        client = Client(
            timeout=args.timeout,
            retries=args.retries,
            delay=args.delay,
        )
        try:
            ensure_initialized(conn, client, seed=args.ids is None)
            if args.full_seed:
                full_seed(conn, client)
            crawl(
                conn,
                client,
                limit=args.limit,
                ids=args.ids,
                enrich=args.enrich_fallback,
                workers=max(1, args.workers),
            )
            _print_status(db_path, conn)
        except ApiError as exc:
            print(f"request failed: {exc}", file=sys.stderr)
            return 1
        except RuntimeError as exc:
            print(f"database error: {exc}", file=sys.stderr)
            return 1
        except KeyboardInterrupt:
            return 130
        finally:
            conn.close()
        return 0

    parser.error(f"unknown command: {args.command}")
    return 2
