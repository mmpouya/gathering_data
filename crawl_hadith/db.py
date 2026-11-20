"""SQLite storage for the hadith crawler."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence

SCHEMA_PATH = Path(__file__).with_name("schema.sql")
SCHEMA_VERSION = "1"
STAGES = ("main", "translate", "explanation", "rejal")


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect(path: str | Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path, timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    conn.execute("PRAGMA busy_timeout = 30000")
    return conn


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
    columns = {
        row[1] for row in conn.execute("PRAGMA table_info(hadith_group_members)")
    }
    if "sort_order" not in columns:
        # Pre-final databases created before sort_order was added.
        conn.execute(
            "ALTER TABLE hadith_group_members ADD COLUMN sort_order INTEGER"
        )
    version = get_meta(conn, "schema_version")
    if version is None:
        set_meta(conn, "schema_version", SCHEMA_VERSION)
    elif version != SCHEMA_VERSION:
        raise RuntimeError(
            f"database schema version {version} is not supported "
            f"(expected {SCHEMA_VERSION})"
        )
    conn.commit()


# ----------------------------------------------------------------------
# metadata
# ----------------------------------------------------------------------


def get_meta(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute(
        "SELECT meta_value FROM metadata WHERE meta_key = ?", (key,)
    ).fetchone()
    return row[0] if row else None


def set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        """
        INSERT INTO metadata(meta_key, meta_value) VALUES (?, ?)
        ON CONFLICT(meta_key) DO UPDATE SET meta_value = excluded.meta_value
        """,
        (key, value),
    )


# ----------------------------------------------------------------------
# reference tables
# ----------------------------------------------------------------------


def upsert_sources(conn: sqlite3.Connection, rows: Iterable[dict[str, Any]]) -> None:
    conn.executemany(
        """
        INSERT INTO sources
            (id, main_title, title, group_title, order_num, sample_word,
             author_title, hadith_count, century_id)
        VALUES
            (:id, :main_title, :title, :group_title, :order_num, :sample_word,
             :author_title, :hadith_count, :century_id)
        ON CONFLICT(id) DO UPDATE SET
            main_title   = COALESCE(excluded.main_title, sources.main_title),
            title        = COALESCE(excluded.title, sources.title),
            group_title  = COALESCE(excluded.group_title, sources.group_title),
            order_num    = COALESCE(excluded.order_num, sources.order_num),
            sample_word  = COALESCE(excluded.sample_word, sources.sample_word),
            author_title = COALESCE(excluded.author_title, sources.author_title),
            hadith_count = COALESCE(excluded.hadith_count, sources.hadith_count),
            century_id   = COALESCE(excluded.century_id, sources.century_id)
        """,
        [
            {
                "id": row["id"],
                "main_title": row.get("main_title"),
                "title": row.get("title"),
                "group_title": row.get("group_title"),
                "order_num": row.get("order_num"),
                "sample_word": row.get("sample_word"),
                "author_title": row.get("author_title"),
                "hadith_count": row.get("hadith_count"),
                "century_id": row.get("century_id"),
            }
            for row in rows
            if row.get("id") is not None
        ],
    )


def ensure_source(
    conn: sqlite3.Connection,
    source_id: int | None,
    title: str | None = None,
    century_id: int | None = None,
) -> None:
    if source_id is None:
        return
    conn.execute(
        """
        INSERT INTO sources(id, main_title, century_id) VALUES (?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            main_title = COALESCE(sources.main_title, excluded.main_title),
            century_id = COALESCE(sources.century_id, excluded.century_id)
        """,
        (source_id, title, century_id),
    )


def _upsert_text_sources(
    conn: sqlite3.Connection, table: str, rows: Iterable[dict[str, Any]]
) -> None:
    conn.executemany(
        f"""
        INSERT INTO {table}(id, title, short_title, author_title)
        VALUES (:id, :title, :short_title, :author_title)
        ON CONFLICT(id) DO UPDATE SET
            title        = COALESCE(excluded.title, {table}.title),
            short_title  = COALESCE(excluded.short_title, {table}.short_title),
            author_title = COALESCE(excluded.author_title, {table}.author_title)
        """,
        [
            {
                "id": row["id"],
                "title": row.get("title"),
                "short_title": row.get("short_title"),
                "author_title": row.get("author_title"),
            }
            for row in rows
            if row.get("id") is not None
        ],
    )


def upsert_translation_sources(
    conn: sqlite3.Connection, rows: Iterable[dict[str, Any]]
) -> None:
    _upsert_text_sources(conn, "translation_sources", rows)


def upsert_explanation_sources(
    conn: sqlite3.Connection, rows: Iterable[dict[str, Any]]
) -> None:
    _upsert_text_sources(conn, "explanation_sources", rows)


def _ensure_text_source(
    conn: sqlite3.Connection,
    table: str,
    source_id: int | None,
    title: str | None = None,
    short_title: str | None = None,
    author_title: str | None = None,
) -> None:
    if source_id is None:
        return
    conn.execute(
        f"""
        INSERT INTO {table}(id, title, short_title, author_title)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            title        = COALESCE({table}.title, excluded.title),
            short_title  = COALESCE({table}.short_title, excluded.short_title),
            author_title = COALESCE({table}.author_title, excluded.author_title)
        """,
        (source_id, title, short_title, author_title),
    )


def upsert_formats(conn: sqlite3.Connection, rows: Iterable[dict[str, Any]]) -> None:
    conn.executemany(
        """
        INSERT INTO formats(id, title, tag_name, access_level, order_num)
        VALUES (:id, :title, :tag_name, :access_level, :order_num)
        ON CONFLICT(id) DO UPDATE SET
            title        = COALESCE(excluded.title, formats.title),
            tag_name     = COALESCE(excluded.tag_name, formats.tag_name),
            access_level = COALESCE(excluded.access_level, formats.access_level),
            order_num    = COALESCE(excluded.order_num, formats.order_num)
        """,
        list(rows),
    )


def upsert_languages(conn: sqlite3.Connection, rows: Iterable[dict[str, Any]]) -> None:
    conn.executemany(
        """
        INSERT INTO languages
            (id, title, native_name, two_char, is_active, is_rtl,
             order_num, is_for_data, is_for_interface)
        VALUES
            (:id, :title, :native_name, :two_char, :is_active, :is_rtl,
             :order_num, :is_for_data, :is_for_interface)
        ON CONFLICT(id) DO UPDATE SET
            title            = COALESCE(excluded.title, languages.title),
            native_name      = COALESCE(excluded.native_name, languages.native_name),
            two_char         = COALESCE(excluded.two_char, languages.two_char),
            is_active        = COALESCE(excluded.is_active, languages.is_active),
            is_rtl           = COALESCE(excluded.is_rtl, languages.is_rtl),
            order_num        = COALESCE(excluded.order_num, languages.order_num),
            is_for_data      = COALESCE(excluded.is_for_data, languages.is_for_data),
            is_for_interface = COALESCE(excluded.is_for_interface, languages.is_for_interface)
        """,
        list(rows),
    )


def ensure_language(conn: sqlite3.Connection, language_id: int | None) -> None:
    if language_id is None:
        return
    conn.execute("INSERT OR IGNORE INTO languages(id) VALUES (?)", (language_id,))


def upsert_qael_roles(conn: sqlite3.Connection, rows: Iterable[dict[str, Any]]) -> None:
    conn.executemany(
        """
        INSERT INTO qael_roles(id, title) VALUES (:id, :title)
        ON CONFLICT(id) DO UPDATE SET title = COALESCE(excluded.title, qael_roles.title)
        """,
        list(rows),
    )


def ensure_qael_role(conn: sqlite3.Connection, role_id: int) -> None:
    conn.execute("INSERT OR IGNORE INTO qael_roles(id) VALUES (?)", (role_id,))


def ensure_qael(
    conn: sqlite3.Connection, qael_id: int, title: str | None = None
) -> None:
    conn.execute(
        """
        INSERT INTO qael(id, title) VALUES (?, ?)
        ON CONFLICT(id) DO UPDATE SET title = COALESCE(qael.title, excluded.title)
        """,
        (qael_id, title),
    )


def upsert_tocs(conn: sqlite3.Connection, rows: Iterable[dict[str, Any]]) -> None:
    conn.executemany(
        """
        INSERT INTO tocs
            (id, source_id, parent_id, title, sample_title, access_level,
             level_num, hadith_count, child_count, is_toc, sort_order)
        VALUES
            (:id, :source_id, :parent_id, :title, :sample_title, :access_level,
             :level_num, :hadith_count, :child_count, :is_toc, :sort_order)
        ON CONFLICT(id) DO UPDATE SET
            source_id    = COALESCE(excluded.source_id, tocs.source_id),
            parent_id    = COALESCE(excluded.parent_id, tocs.parent_id),
            title        = COALESCE(excluded.title, tocs.title),
            sample_title = COALESCE(excluded.sample_title, tocs.sample_title),
            access_level = COALESCE(excluded.access_level, tocs.access_level),
            level_num    = COALESCE(excluded.level_num, tocs.level_num),
            hadith_count = COALESCE(excluded.hadith_count, tocs.hadith_count),
            child_count  = COALESCE(excluded.child_count, tocs.child_count),
            is_toc       = COALESCE(excluded.is_toc, tocs.is_toc),
            sort_order   = COALESCE(excluded.sort_order, tocs.sort_order)
        """,
        [
            {
                "id": row["id"],
                "source_id": row.get("source_id"),
                "parent_id": row.get("parent_id"),
                "title": row.get("title"),
                "sample_title": row.get("sample_title"),
                "access_level": row.get("access_level"),
                "level_num": row.get("level_num"),
                "hadith_count": row.get("hadith_count"),
                "child_count": row.get("child_count"),
                "is_toc": row.get("is_toc"),
                "sort_order": row.get("sort_order"),
            }
            for row in rows
            if row.get("id") is not None
        ],
    )


def upsert_ravis(conn: sqlite3.Connection, rows: Iterable[dict[str, Any]]) -> None:
    conn.executemany(
        """
        INSERT INTO ravis(ravi_id, name, hint, info_json)
        VALUES (:ravi_id, :name, :hint, :info_json)
        ON CONFLICT(ravi_id) DO UPDATE SET
            name = COALESCE(excluded.name, ravis.name),
            hint = COALESCE(excluded.hint, ravis.hint),
            info_json = CASE
                WHEN excluded.info_json IS NULL THEN ravis.info_json
                WHEN ravis.info_json IS NULL THEN excluded.info_json
                WHEN length(excluded.info_json) > length(ravis.info_json)
                    THEN excluded.info_json
                ELSE ravis.info_json
            END
        """,
        list(rows),
    )


# ----------------------------------------------------------------------
# crawl queue
# ----------------------------------------------------------------------


def seed_ids(conn: sqlite3.Connection, ids: Iterable[int]) -> None:
    now = utcnow()
    conn.executemany(
        """
        INSERT INTO crawl_status(hadith_id, updated_at) VALUES (?, ?)
        ON CONFLICT(hadith_id) DO NOTHING
        """,
        ((int(hadith_id), now) for hadith_id in ids),
    )


def select_pending_ids(
    conn: sqlite3.Connection,
    *,
    limit: int | None = None,
    ids: Sequence[int] | None = None,
    enrich: bool = False,
    skip_main: bool = False,
) -> list[int]:
    if ids is not None:
        seed_ids(conn, ids)
        conn.commit()
        result = sorted(set(int(value) for value in ids))
        if skip_main:
            # Never select a row whose main data is missing: its text counts
            # are unknown, so translate/explanation would be skipped forever.
            ready: set[int] = set()
            for start in range(0, len(result), 900):
                chunk = result[start : start + 900]
                placeholders = ",".join("?" for _ in chunk)
                ready.update(
                    row[0]
                    for row in conn.execute(
                        "SELECT hadith_id FROM crawl_status"
                        " WHERE main_state NOT IN ('pending','failed')"
                        f" AND hadith_id IN ({placeholders})",
                        chunk,
                    )
                )
            result = [hadith_id for hadith_id in result if hadith_id in ready]
        return result[:limit] if limit is not None else result

    clauses = [
        "main_state IN ('pending','failed')",
        "translate_state IN ('pending','failed')",
        "explanation_state IN ('pending','failed')",
        "rejal_state IN ('pending','failed')",
    ]
    if enrich:
        clauses.append("rejal_state = 'fallback'")
        if not skip_main:
            clauses.append("main_state = 'fallback'")
    sql = (
        "SELECT hadith_id FROM crawl_status WHERE ("
        + " OR ".join(clauses)
        + ")"
    )
    if skip_main:
        # These rows never touch the quota-capped by-id endpoint.
        sql += " AND main_state NOT IN ('pending','failed')"
    sql += (
        " ORDER BY CASE WHEN rejal_state = 'fallback' THEN 0 ELSE 1 END, hadith_id"
        if enrich and skip_main
        else (
            " ORDER BY CASE WHEN main_state = 'fallback'"
            " OR rejal_state = 'fallback' THEN 0 ELSE 1 END, hadith_id"
            if enrich
            else " ORDER BY hadith_id"
        )
    )
    params: list[Any] = []
    if limit is not None:
        sql += " LIMIT ?"
        params.append(limit)
    return [row[0] for row in conn.execute(sql, params)]


def select_main_pending_ids(
    conn: sqlite3.Connection, *, limit: int | None = None
) -> list[int]:
    sql = (
        "SELECT hadith_id FROM crawl_status"
        " WHERE main_state IN ('pending','failed') ORDER BY hadith_id"
    )
    params: list[Any] = []
    if limit is not None:
        sql += " LIMIT ?"
        params.append(limit)
    return [row[0] for row in conn.execute(sql, params)]


def status_rows(
    conn: sqlite3.Connection, ids: Sequence[int]
) -> list[sqlite3.Row]:
    rows: list[sqlite3.Row] = []
    ids = list(ids)
    for start in range(0, len(ids), 900):
        chunk = ids[start : start + 900]
        placeholders = ",".join("?" for _ in chunk)
        rows.extend(
            conn.execute(
                f"""
                SELECT cs.*, h.translate_count, h.explanation_count
                FROM crawl_status cs
                LEFT JOIN hadiths h ON h.hadith_id = cs.hadith_id
                WHERE cs.hadith_id IN ({placeholders})
                ORDER BY cs.hadith_id
                """,
                chunk,
            )
        )
    return rows


def set_stage(
    conn: sqlite3.Connection,
    hadith_id: int,
    stage: str,
    state: str,
    error: str | None = None,
    *,
    increment_attempts: bool = False,
) -> None:
    if stage not in STAGES:
        raise ValueError(f"unknown stage: {stage}")
    column = f"{stage}_state"
    attempts = "attempts + 1" if increment_attempts else "attempts"
    conn.execute(
        f"""
        UPDATE crawl_status
        SET {column} = ?, last_error = COALESCE(?, last_error),
            attempts = {attempts}, updated_at = ?
        WHERE hadith_id = ?
        """,
        (state, error, utcnow(), hadith_id),
    )


def mark_all_missing(conn: sqlite3.Connection, hadith_id: int) -> None:
    conn.execute(
        """
        UPDATE crawl_status
        SET main_state = 'missing',
            translate_state = 'missing',
            explanation_state = 'missing',
            rejal_state = 'missing',
            last_error = NULL,
            updated_at = ?
        WHERE hadith_id = ?
        """,
        (utcnow(), hadith_id),
    )


def clear_error_if_complete(conn: sqlite3.Connection, hadith_id: int) -> None:
    terminal = "('done','skipped','missing')"
    conn.execute(
        f"""
        UPDATE crawl_status SET last_error = NULL
        WHERE hadith_id = ?
          AND main_state IN {terminal}
          AND translate_state IN {terminal}
          AND explanation_state IN {terminal}
          AND rejal_state IN {terminal}
        """,
        (hadith_id,),
    )


# ----------------------------------------------------------------------
# writes
# ----------------------------------------------------------------------

HADITH_COLUMNS = (
    "hadith_id",
    "source_id",
    "author_title",
    "book_title",
    "book_title_short",
    "vol",
    "page_num",
    "start_index_in_page",
    "access_level",
    "text",
    "text_plain",
    "short_text",
    "text_no_erab",
    "qael_title_list",
    "toc_text",
    "translate_count",
    "explanation_count",
    "hadith_in_book_count",
    "hadith_related_count",
    "hadith_same_toc_count",
    "group_hadith_count",
    "has_translate",
    "has_explanation",
    "has_similar",
    "has_family",
    "noor_lib_link",
)


def write_hadith(conn: sqlite3.Connection, row: dict[str, Any]) -> None:
    ensure_source(conn, row.get("source_id"), row.get("book_title"))
    columns = ", ".join(HADITH_COLUMNS)
    placeholders = ", ".join(f":{name}" for name in HADITH_COLUMNS)
    updates = ", ".join(
        f"{name} = excluded.{name}" for name in HADITH_COLUMNS if name != "hadith_id"
    )
    conn.execute(
        f"""
        INSERT INTO hadiths({columns}) VALUES ({placeholders})
        ON CONFLICT(hadith_id) DO UPDATE SET {updates}
        """,
        {name: row.get(name) for name in HADITH_COLUMNS},
    )


def write_fallback_hadith(conn: sqlite3.Connection, row: dict[str, Any]) -> None:
    """Fill missing core fields from the legacy endpoint without overwriting primary data."""
    columns = (
        "hadith_id",
        "book_title",
        "book_title_short",
        "vol",
        "page_num",
        "text",
        "text_plain",
        "short_text",
        "qael_title_list",
        "toc_text",
    )
    updates = ", ".join(
        f"{name} = COALESCE(hadiths.{name}, excluded.{name})"
        for name in columns
        if name != "hadith_id"
    )
    conn.execute(
        f"""
        INSERT INTO hadiths({", ".join(columns)})
        VALUES ({", ".join(f":{name}" for name in columns)})
        ON CONFLICT(hadith_id) DO UPDATE SET {updates}
        """,
        {name: row.get(name) for name in columns},
    )


def write_group_members(
    conn: sqlite3.Connection, hadith_id: int, rows: list[dict[str, Any]]
) -> None:
    if not rows:
        # Keep an existing group relation when a response omits it.
        return
    grouped: dict[int, list[dict[str, Any]]] = {}
    for row in rows:
        grouped.setdefault(row["group_id"], []).append(row)
    for group_id, members in grouped.items():
        existing = conn.execute(
            "SELECT COUNT(*) FROM hadith_group_members WHERE group_id = ?",
            (group_id,),
        ).fetchone()[0]
        if existing > len(members):
            # A partial payload must never shrink a group already stored whole.
            continue
        conn.execute(
            "DELETE FROM hadith_group_members WHERE group_id = ?", (group_id,)
        )
        for member in members:
            ensure_source(
                conn,
                member.get("source_id"),
                member.get("source_main_title"),
                member.get("century_id"),
            )
        conn.executemany(
            """
            INSERT INTO hadith_group_members
                (group_id, hadith_id, source_id, source_main_title,
                 source_short_title, source_hadith_id, vol, page_num,
                 century_id, access_level, section, group_hadith_count,
                 size_id, size_title, size_json, book_json, sort_order)
            VALUES
                (:group_id, :hadith_id, :source_id, :source_main_title,
                 :source_short_title, :source_hadith_id, :vol, :page_num,
                 :century_id, :access_level, :section, :group_hadith_count,
                 :size_id, :size_title, :size_json, :book_json, :sort_order)
            """,
            members,
        )


def write_qael(
    conn: sqlite3.Connection, hadith_id: int, rows: list[dict[str, Any]]
) -> None:
    conn.execute("DELETE FROM hadith_qael WHERE hadith_id = ?", (hadith_id,))
    for row in rows:
        ensure_qael(conn, row["qael_id"], row.get("title"))
        ensure_qael_role(conn, row["role_id"])
    conn.executemany(
        """
        INSERT INTO hadith_qael(hadith_id, qael_id, role_id, sort_order)
        VALUES (?, ?, ?, ?)
        """,
        [
            (hadith_id, row["qael_id"], row["role_id"], row.get("sort_order"))
            for row in rows
        ],
    )


def write_tocs(
    conn: sqlite3.Connection, hadith_id: int, rows: list[dict[str, Any]]
) -> None:
    if not rows:
        # Keep an existing chapter path if a response omits tocInfo.
        return
    for row in rows:
        ensure_source(conn, row.get("source_id"))
    upsert_tocs(conn, rows)
    conn.execute("DELETE FROM hadith_toc WHERE hadith_id = ?", (hadith_id,))
    conn.executemany(
        "INSERT INTO hadith_toc(hadith_id, toc_id, depth) VALUES (?, ?, ?)",
        [(hadith_id, row["id"], row["depth"]) for row in rows],
    )


def _write_text_items(
    conn: sqlite3.Connection,
    *,
    table: str,
    source_table: str,
    hadith_id: int,
    rows: list[dict[str, Any]],
) -> None:
    conn.execute(f"DELETE FROM {table} WHERE hadith_id = ?", (hadith_id,))
    unique = {row["id"]: row for row in rows if row.get("id") is not None}
    for row in unique.values():
        _ensure_text_source(
            conn,
            source_table,
            row.get("source_id"),
            row.get("source_title"),
            row.get("source_short_title"),
            row.get("author_title"),
        )
        ensure_language(conn, row.get("language_id"))
    conn.executemany(
        f"""
        INSERT INTO {table}
            (id, hadith_id, source_id, language_id, text, text_plain,
             short_text, is_shorted, vol, page_num, comment_count,
             insert_date, noor_lib_link)
        VALUES
            (:id, :hadith_id, :source_id, :language_id, :text, :text_plain,
             :short_text, :is_shorted, :vol, :page_num, :comment_count,
             :insert_date, :noor_lib_link)
        ON CONFLICT(id) DO UPDATE SET
            hadith_id     = excluded.hadith_id,
            source_id     = excluded.source_id,
            language_id   = excluded.language_id,
            text          = excluded.text,
            text_plain    = excluded.text_plain,
            short_text    = excluded.short_text,
            is_shorted    = excluded.is_shorted,
            vol           = excluded.vol,
            page_num      = excluded.page_num,
            comment_count = excluded.comment_count,
            insert_date   = excluded.insert_date,
            noor_lib_link = excluded.noor_lib_link
        """,
        list(unique.values()),
    )


def write_translations(
    conn: sqlite3.Connection, hadith_id: int, rows: list[dict[str, Any]]
) -> None:
    _write_text_items(
        conn,
        table="translations",
        source_table="translation_sources",
        hadith_id=hadith_id,
        rows=rows,
    )


def write_explanations(
    conn: sqlite3.Connection, hadith_id: int, rows: list[dict[str, Any]]
) -> None:
    _write_text_items(
        conn,
        table="explanations",
        source_table="explanation_sources",
        hadith_id=hadith_id,
        rows=rows,
    )


def write_rejal(
    conn: sqlite3.Connection, hadith_id: int, parsed: dict[str, Any]
) -> None:
    conn.execute("DELETE FROM hadith_sanad_ravi WHERE hadith_id = ?", (hadith_id,))
    conn.execute("DELETE FROM hadith_sanad WHERE hadith_id = ?", (hadith_id,))
    ravis = parsed.get("ravis") or []
    if ravis:
        upsert_ravis(conn, ravis)

    segment_rows: list[tuple[Any, ...]] = []
    ravi_rows: list[tuple[Any, ...]] = []
    for sanad in parsed.get("sanads") or []:
        sanad_index = sanad["sanad_index"]
        for fallback_order, segment in enumerate(sanad["segments"]):
            order = segment.get("segment_order")
            if order is None:
                order = fallback_order
            segment_rows.append(
                (
                    hadith_id,
                    sanad_index,
                    order,
                    segment.get("segment_type"),
                    segment.get("title"),
                )
            )
            for ravi in segment.get("ravis") or []:
                ravi_rows.append(
                    (
                        hadith_id,
                        sanad_index,
                        order,
                        ravi["ravi_index"],
                        ravi["ravi_id"],
                        ravi.get("hint"),
                    )
                )
    conn.executemany(
        """
        INSERT INTO hadith_sanad
            (hadith_id, sanad_index, segment_order, segment_type, title)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(hadith_id, sanad_index, segment_order) DO UPDATE SET
            segment_type = excluded.segment_type,
            title = excluded.title
        """,
        segment_rows,
    )
    conn.executemany(
        """
        INSERT OR REPLACE INTO hadith_sanad_ravi
            (hadith_id, sanad_index, segment_order, ravi_index, ravi_id, hint)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        ravi_rows,
    )


# ----------------------------------------------------------------------
# source seeding and status
# ----------------------------------------------------------------------


def init_source_seed(conn: sqlite3.Connection) -> None:
    now = utcnow()
    conn.execute(
        """
        INSERT OR IGNORE INTO source_seed(source_id, updated_at)
        SELECT id, ? FROM sources
        """,
        (now,),
    )


def sources_to_seed(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return list(
        conn.execute(
            """
            SELECT ss.*, s.title
            FROM source_seed ss
            JOIN sources s ON s.id = ss.source_id
            WHERE ss.status <> 'done'
            ORDER BY ss.source_id
            """
        )
    )


def set_source_seed(
    conn: sqlite3.Connection,
    source_id: int,
    *,
    status: str,
    next_page: int,
    error: str | None = None,
    increment_attempts: bool = False,
) -> None:
    attempts = "attempts + 1" if increment_attempts else "attempts"
    conn.execute(
        f"""
        UPDATE source_seed
        SET status = ?, next_page = ?, last_error = ?,
            attempts = {attempts}, updated_at = ?
        WHERE source_id = ?
        """,
        (status, next_page, error, utcnow(), source_id),
    )


def counts(conn: sqlite3.Connection) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for stage in STAGES:
        rows = conn.execute(
            f"SELECT {stage}_state, COUNT(*) FROM crawl_status GROUP BY {stage}_state"
        ).fetchall()
        result[stage] = {row[0]: row[1] for row in rows}
    for table in (
        "hadiths",
        "translations",
        "explanations",
        "hadith_sanad",
        "hadith_sanad_ravi",
        "ravis",
        "hadith_group_members",
        "hadith_toc",
        "sources",
        "tocs",
        "crawl_status",
    ):
        result[table] = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    return result
