"""Crawl orchestration: seeding, fetching, saving, and resuming."""

from __future__ import annotations

import gzip
import json
import logging
import re
import time
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Any

from . import db, parse
from .api import SITE_URL, ApiError, Client, RateLimited

logger = logging.getLogger(__name__)


@dataclass
class StageResult:
    state: str
    data: Any = None
    error: str | None = None


@dataclass
class Bundle:
    hadith_id: int
    stages: dict[str, StageResult] = field(default_factory=dict)


# ----------------------------------------------------------------------
# fetching
# ----------------------------------------------------------------------


def _needs(state: str, *, enrich: bool = False) -> bool:
    if state in {"pending", "failed"}:
        return True
    return enrich and state == "fallback"


def _error_text(primary: BaseException | None, fallback: BaseException | None) -> str:
    parts = []
    if primary is not None:
        parts.append(f"primary: {primary}")
    if fallback is not None:
        parts.append(f"fallback: {fallback}")
    return "; ".join(parts) or "unknown error"


def fetch_bundle(client: Client, row: Any, *, enrich: bool = False) -> Bundle:
    hadith_id = int(row["hadith_id"])
    bundle = Bundle(hadith_id)
    main_state = row["main_state"]

    main_data: dict[str, Any] | None = None
    if _needs(main_state, enrich=enrich):
        primary_error: BaseException | None = None
        try:
            main_data = client.hadith_detail(hadith_id)
        except RateLimited as exc:
            # Both main endpoints share the same IP and limit; do not double
            # the request volume while the service is throttling us.
            state = "fallback" if main_state == "fallback" else "failed"
            bundle.stages["main"] = StageResult(state, None, str(exc))
            return bundle
        except ApiError as exc:
            primary_error = exc
            main_data = None

        if main_data is not None:
            bundle.stages["main"] = StageResult("done", main_data)
        else:
            fallback_error: BaseException | None = None
            try:
                fallback_data = client.hadith_detail_fallback(hadith_id)
            except ApiError as exc:
                fallback_error = exc
                fallback_data = None

            if fallback_data is not None:
                bundle.stages["main"] = StageResult("fallback", fallback_data)
            elif main_state == "fallback":
                # An enrichment attempt failed; keep the existing fallback data.
                bundle.stages["main"] = StageResult(
                    "fallback", None, _error_text(primary_error, fallback_error)
                )
            elif primary_error is not None or fallback_error is not None:
                bundle.stages["main"] = StageResult(
                    "failed", None, _error_text(primary_error, fallback_error)
                )
            elif main_state == "failed":
                bundle.stages["main"] = StageResult(
                    "failed", None, "primary and fallback returned empty"
                )
            else:
                bundle.stages["main"] = StageResult("missing")

    else:
        main_data = None

    main_enriched = main_state == "fallback" and isinstance(main_data, dict)

    if main_data is None and bundle.stages.get("main") is None:
        # Main is already done/fallback. Its counts live in the hadith row.
        translate_count = row["translate_count"]
        explanation_count = row["explanation_count"]
    elif isinstance(main_data, dict):
        translate_count = parse.as_int(main_data.get("translateCount"))
        explanation_count = parse.as_int(main_data.get("explanationCount"))
    else:
        translate_count = None
        explanation_count = None

    if bundle.stages.get("main") and bundle.stages["main"].state == "missing":
        for stage in ("translate", "explanation", "rejal"):
            bundle.stages[stage] = StageResult("missing")
        return bundle

    # If the main request itself failed, retry the whole hadith next run.
    if bundle.stages.get("main") and bundle.stages["main"].state == "failed":
        return bundle

    translate_need = _needs(row["translate_state"]) or (
        main_enriched
        and row["translate_state"] == "skipped"
        and bool(translate_count and translate_count > 0)
    )
    if translate_need:
        if translate_count == 0:
            bundle.stages["translate"] = StageResult("skipped")
        else:
            try:
                payload = client.translations(hadith_id)
            except ApiError as exc:
                bundle.stages["translate"] = StageResult("failed", None, str(exc))
            else:
                items = payload.get("data") or []
                if items:
                    bundle.stages["translate"] = StageResult("done", payload)
                elif translate_count and translate_count > 0:
                    bundle.stages["translate"] = StageResult(
                        "failed",
                        None,
                        f"translateCount={translate_count} but list is empty",
                    )
                else:
                    bundle.stages["translate"] = StageResult("skipped", payload)

    explanation_need = _needs(row["explanation_state"]) or (
        main_enriched
        and row["explanation_state"] == "skipped"
        and bool(explanation_count and explanation_count > 0)
    )
    if explanation_need:
        if explanation_count == 0:
            bundle.stages["explanation"] = StageResult("skipped")
        else:
            try:
                payload = client.explanations(hadith_id)
            except ApiError as exc:
                bundle.stages["explanation"] = StageResult("failed", None, str(exc))
            else:
                items = payload.get("data") or []
                if items:
                    bundle.stages["explanation"] = StageResult("done", payload)
                elif explanation_count and explanation_count > 0:
                    bundle.stages["explanation"] = StageResult(
                        "failed",
                        None,
                        f"explanationCount={explanation_count} but list is empty",
                    )
                else:
                    bundle.stages["explanation"] = StageResult("skipped", payload)

    if _needs(row["rejal_state"], enrich=enrich):
        primary_error = None
        try:
            payload = client.rejal(hadith_id)
        except RateLimited as exc:
            # Skip the v1 fallback: same IP, same limit, extra requests.
            state = "fallback" if row["rejal_state"] == "fallback" else "failed"
            bundle.stages["rejal"] = StageResult(state, None, str(exc))
        except ApiError as exc:
            primary_error = exc
        else:
            data = payload.get("data")
            has_data = isinstance(data, dict) and bool(
                data.get("sanadList") or data.get("raviList")
            )
            bundle.stages["rejal"] = StageResult(
                "done" if has_data else "skipped", payload if has_data else None
            )
            payload = None

        if "rejal" not in bundle.stages:
            fallback_error = None
            try:
                fallback_payload = client.rejal_fallback(hadith_id)
            except ApiError as exc:
                fallback_error = exc
                fallback_payload = None
            data = (fallback_payload or {}).get("data")
            has_data = isinstance(data, dict) and bool(
                data.get("sanadList") or data.get("raviList")
            )
            if has_data:
                bundle.stages["rejal"] = StageResult("fallback", fallback_payload)
            elif row["rejal_state"] == "fallback":
                bundle.stages["rejal"] = StageResult(
                    "fallback",
                    None,
                    _error_text(primary_error, fallback_error),
                )
            else:
                bundle.stages["rejal"] = StageResult(
                    "failed", None, _error_text(primary_error, fallback_error)
                )

    return bundle


# ----------------------------------------------------------------------
# saving
# ----------------------------------------------------------------------


def save_bundle(conn: Any, bundle: Bundle) -> None:
    hadith_id = bundle.hadith_id
    with conn:
        main = bundle.stages.get("main")
        if main is not None:
            if main.state == "done" and isinstance(main.data, dict):
                row = parse.parse_hadith(main.data)
                db.write_hadith(conn, row)
                db.write_group_members(
                    conn, hadith_id, parse.parse_group_members(main.data)
                )
                db.write_qael(conn, hadith_id, parse.parse_qael(main.data))
                db.write_tocs(conn, hadith_id, parse.parse_tocs(main.data))
                db.set_stage(conn, hadith_id, "main", "done")
            elif main.state == "fallback" and isinstance(main.data, dict):
                db.write_fallback_hadith(conn, parse.parse_fallback_hadith(main.data))
                db.set_stage(conn, hadith_id, "main", "fallback", main.error)
            elif main.state == "fallback":
                db.set_stage(conn, hadith_id, "main", "fallback", main.error)
            elif main.state == "missing":
                db.mark_all_missing(conn, hadith_id)
                db.clear_error_if_complete(conn, hadith_id)
                return
            elif main.state == "failed":
                db.set_stage(
                    conn,
                    hadith_id,
                    "main",
                    "failed",
                    main.error,
                    increment_attempts=True,
                )

        translate = bundle.stages.get("translate")
        if translate is not None:
            if translate.state in {"done", "skipped"}:
                rows = (
                    parse.parse_translations(translate.data)
                    if translate.state == "done" and isinstance(translate.data, dict)
                    else []
                )
                db.write_translations(conn, hadith_id, rows)
                db.set_stage(conn, hadith_id, "translate", translate.state)
            elif translate.state == "failed":
                db.set_stage(
                    conn,
                    hadith_id,
                    "translate",
                    "failed",
                    translate.error,
                    increment_attempts=True,
                )
            elif translate.state == "missing":
                db.set_stage(conn, hadith_id, "translate", "missing")

        explanation = bundle.stages.get("explanation")
        if explanation is not None:
            if explanation.state in {"done", "skipped"}:
                rows = (
                    parse.parse_explanations(explanation.data)
                    if explanation.state == "done"
                    and isinstance(explanation.data, dict)
                    else []
                )
                db.write_explanations(conn, hadith_id, rows)
                db.set_stage(conn, hadith_id, "explanation", explanation.state)
            elif explanation.state == "failed":
                db.set_stage(
                    conn,
                    hadith_id,
                    "explanation",
                    "failed",
                    explanation.error,
                    increment_attempts=True,
                )
            elif explanation.state == "missing":
                db.set_stage(conn, hadith_id, "explanation", "missing")

        rejal = bundle.stages.get("rejal")
        if rejal is not None:
            if rejal.state in {"done", "fallback"} and isinstance(rejal.data, dict):
                db.write_rejal(conn, hadith_id, parse.parse_rejal(rejal.data))
                db.set_stage(conn, hadith_id, "rejal", rejal.state, rejal.error)
            elif rejal.state == "fallback":
                db.set_stage(conn, hadith_id, "rejal", "fallback", rejal.error)
            elif rejal.state == "skipped":
                existing = conn.execute(
                    "SELECT 1 FROM hadith_sanad WHERE hadith_id = ? LIMIT 1",
                    (hadith_id,),
                ).fetchone()
                if existing is None:
                    db.write_rejal(conn, hadith_id, {"sanads": [], "ravis": []})
                db.set_stage(conn, hadith_id, "rejal", "skipped")
            elif rejal.state == "failed":
                db.set_stage(
                    conn,
                    hadith_id,
                    "rejal",
                    "failed",
                    rejal.error,
                    increment_attempts=True,
                )
            elif rejal.state == "missing":
                db.set_stage(conn, hadith_id, "rejal", "missing")

        db.clear_error_if_complete(conn, hadith_id)


# ----------------------------------------------------------------------
# seeding
# ----------------------------------------------------------------------


def sitemap_ids(client: Client) -> list[int]:
    try:
        index = client.get_bytes(
            f"{SITE_URL}/assets/download/sitemap/sitemap-index.xml"
        )
        root = ET.fromstring(index)
        locations = [
            element.text
            for element in root.iter()
            if element.tag.endswith("loc") and element.text
        ]
        logger.info("sitemap index: %s file(s)", len(locations))
        ids: set[int] = set()
        for location in locations:
            raw = client.get_bytes(location)
            if location.endswith(".gz"):
                raw = gzip.decompress(raw)
            root = ET.fromstring(raw)
            for element in root.iter():
                if not element.tag.endswith("loc") or not element.text:
                    continue
                match = re.search(r"/hadith/(\d+)/", element.text)
                if match:
                    ids.add(int(match.group(1)))
    except (ET.ParseError, OSError, EOFError) as exc:
        raise ApiError(f"sitemap download/parse failed: {exc}") from exc
    return sorted(ids)


def seed_sitemap(conn: Any, client: Client) -> int:
    ids = sitemap_ids(client)
    db.seed_ids(conn, ids)
    db.set_meta(conn, "sitemap_seeded_at", db.utcnow())
    db.set_meta(conn, "sitemap_count", str(len(ids)))
    conn.commit()
    logger.info("seeded %s hadith ids from sitemap", len(ids))
    return len(ids)


def full_seed(conn: Any, client: Client) -> int:
    db.init_source_seed(conn)
    conn.commit()
    failed = 0
    sources = db.sources_to_seed(conn)
    if not sources:
        print("full seed: all sources already done", flush=True)
        return 0
    print(f"full seed: {len(sources)} sources pending", flush=True)
    for index, source in enumerate(sources, start=1):
        source_id = source["source_id"]
        page = source["next_page"]
        try:
            while page <= 10_000:
                ids = client.source_hadith_ids(source_id, page)
                if not ids:
                    break
                db.seed_ids(conn, ids)
                db.set_source_seed(
                    conn, source_id, status="pending", next_page=page + 1
                )
                conn.commit()
                page += 1
            else:
                raise ApiError(f"source {source_id} exceeded 10000 pages")
        except ApiError as exc:
            failed += 1
            logger.warning("full seed failed for source %s: %s", source_id, exc)
            db.set_source_seed(
                conn,
                source_id,
                status="failed",
                next_page=page,
                error=str(exc),
                increment_attempts=True,
            )
            conn.commit()
        else:
            db.set_source_seed(conn, source_id, status="done", next_page=page)
            conn.commit()
        if index % 10 == 0 or index == len(sources):
            print(
                f"full seed: {index}/{len(sources)} sources, {failed} failed",
                flush=True,
            )
    return failed


def ensure_initialized(conn: Any, client: Client, *, seed: bool = True) -> None:
    db.ensure_schema(conn)
    if db.get_meta(conn, "sources_seeded_at") is None:
        logger.info("fetching reference metadata")
        source_rows = parse.parse_sources(client.source_list())
        if not source_rows:
            raise ApiError("SourceList returned no sources")
        db.upsert_sources(conn, source_rows)
        db.set_meta(conn, "sources_seeded_at", db.utcnow())
        conn.commit()
    if db.get_meta(conn, "formats_seeded_at") is None:
        db.upsert_formats(conn, parse.parse_formats(client.format_list()))
        db.set_meta(conn, "formats_seeded_at", db.utcnow())
        conn.commit()
    if db.get_meta(conn, "languages_seeded_at") is None:
        db.upsert_languages(conn, parse.parse_languages(client.language_list()))
        db.set_meta(conn, "languages_seeded_at", db.utcnow())
        conn.commit()
    if db.get_meta(conn, "qael_roles_seeded_at") is None:
        db.upsert_qael_roles(conn, parse.parse_qael_roles(client.qael_role_list()))
        db.set_meta(conn, "qael_roles_seeded_at", db.utcnow())
        conn.commit()
    if (
        db.get_meta(conn, "statistics") is None
        and db.get_meta(conn, "statistics_unavailable_at") is None
    ):
        try:
            payload = client.statistics()
        except ApiError as exc:
            # Statistics are informational and the search service can be
            # unavailable independently of the hadith endpoints.
            logger.warning("statistics unavailable: %s", exc)
            db.set_meta(conn, "statistics_unavailable_at", db.utcnow())
            conn.commit()
        else:
            db.set_meta(
                conn, "statistics", json.dumps(payload.get("data"), ensure_ascii=False)
            )
            conn.commit()
    if seed and (
        db.get_meta(conn, "sitemap_seeded_at") is None
        or conn.execute("SELECT COUNT(*) FROM crawl_status").fetchone()[0] == 0
    ):
        seed_sitemap(conn, client)


# ----------------------------------------------------------------------
# main crawl loop
# ----------------------------------------------------------------------


def crawl(
    conn: Any,
    client: Client,
    *,
    limit: int | None = None,
    ids: list[int] | None = None,
    enrich: bool = False,
    workers: int = 2,
) -> dict[str, int]:
    ids = db.select_pending_ids(conn, limit=limit, ids=ids, enrich=enrich)
    if not ids:
        print("nothing pending", flush=True)
        return {"total": 0, "processed": 0}
    total = len(ids)
    print(f"crawling {total} hadith(s) with {workers} worker(s)", flush=True)
    batch_size = max(16, workers * 4)
    processed = 0
    started = time.monotonic()
    try:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            for start in range(0, total, batch_size):
                chunk = ids[start : start + batch_size]
                rows = db.status_rows(conn, chunk)
                futures = {
                    pool.submit(fetch_bundle, client, row, enrich=enrich): row
                    for row in rows
                }
                for future in as_completed(futures):
                    row = futures[future]
                    hadith_id = int(row["hadith_id"])
                    try:
                        bundle = future.result()
                    except Exception as exc:  # unexpected bug or transport issue
                        logger.exception("fetch failed for hadith %s", hadith_id)
                        bundle = Bundle(hadith_id)
                        with conn:
                            for stage in db.STAGES:
                                if row[f"{stage}_state"] in {"pending", "failed"}:
                                    db.set_stage(
                                        conn,
                                        hadith_id,
                                        stage,
                                        "failed",
                                        str(exc),
                                        increment_attempts=True,
                                    )
                    else:
                        try:
                            save_bundle(conn, bundle)
                        except Exception as exc:
                            logger.exception("save failed for hadith %s", hadith_id)
                            with conn:
                                for stage in bundle.stages:
                                    if row[f"{stage}_state"] in {"pending", "failed"}:
                                        db.set_stage(
                                            conn,
                                            hadith_id,
                                            stage,
                                            "failed",
                                            str(exc),
                                            increment_attempts=True,
                                        )
                    processed += 1
                    if processed % 100 == 0 or processed == total:
                        elapsed = max(time.monotonic() - started, 0.001)
                        rate = processed / elapsed
                        remaining = (total - processed) / rate if rate else 0
                        print(
                            f"{processed}/{total} "
                            f"({processed * 100 // total}%, "
                            f"{rate:.1f}/s, eta {remaining / 60:.1f} min)",
                            flush=True,
                        )
    except KeyboardInterrupt:
        print(
            f"interrupted after {processed}/{total}; committed work is saved",
            flush=True,
        )
        raise
    elapsed = max(time.monotonic() - started, 0.001)
    print(
        f"finished {processed} hadith(s) in {elapsed / 60:.1f} min "
        f"({processed / elapsed:.1f}/s)",
        flush=True,
    )
    return {"total": total, "processed": processed}
