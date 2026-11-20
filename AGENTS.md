# AGENTS.md

Persistent context and working memory for this repository. Read this file,
`README.md`, and the module you intend to change before editing.

## Project Overview

`crawl_hadith` is a resumable crawler for [hadith.inoor.ir](https://hadith.inoor.ir/).
It calls the site's Angular-facing JSON service directly and stores hadiths,
parallel sources, translations, explanations, chapter paths, and rejal/sanad
data in a single SQLite database.

- **Language/runtime:** Python >= 3.12 (`.python-version` pins 3.12).
- **Package manager:** `uv`. The project is a flat, zero-dependency package;
  `pyproject.toml` has an empty `dependencies` list and `uv.lock` is trivial.
- **Standard library only:** HTTP uses `urllib`, XML uses `xml.etree`, storage
  uses `sqlite3`, concurrency uses `concurrent.futures`. Do not add a runtime
  dependency without a clear reason.
- **Network:** crawling needs access to `https://hadith.inoor.ir`; tests never
  touch the network.
- **Local database:** `hadith.db` in the project root is real crawl state and is
  git-ignored (`*.db`, `*.db-wal`, `*.db-shm`). It is not a test fixture. Do not
  delete, reset, or commit it. Use a scratch path such as `test.db` for
  experiments.
- **Scope:** the crawler stores data only. There is no web server, UI, build
  step, container, or CI configuration.

## Architecture & Directory Map

```text
main.py                     Thin wrapper: calls crawl_hadith.cli.main().
crawl_hadith/
  __main__.py               Enables `python -m crawl_hadith`.
  cli.py                    argparse, logging setup, status printing, exit codes.
  api.py                    Stdlib HTTP client, retries/backoff, endpoint methods.
  parse.py                  Pure JSON-to-dict normalizers; no I/O, no DB.
  crawl.py                  Orchestration: seeding, fetch bundles, fallback, save.
  db.py                     SQLite connection, schema bootstrap, all SQL writes.
  schema.sql                Tables, indexes, stage-state checks, same-toc views.
  nekoray_pool.py           Windows-only helper: isolated NekoRay instances -> proxy list.
tests/
  test_pipeline.py          Offline unittest suite using recorded fixtures.
  fixtures/*.json           Recorded API responses for hadith IDs 105540/105542.
```

### Module responsibilities (keep these boundaries)

| Module | Owns | Must not do |
|---|---|---|
| `api.py` | URL building, HTTP transport, retry policy, `ApiError`/`NotFound`, endpoint-specific request shapes | Touch SQLite, parse domain records |
| `parse.py` | Defensive field normalization (`as_int`, `as_text`, `as_bool_int`, `plain_text`) and record shaping | Network, DB, logging decisions |
| `db.py` | Connection pragmas, schema versioning, upserts, stage-state transitions, row writes | HTTP, response-shape decisions |
| `crawl.py` | Seeding strategies, per-hadith stage orchestration/fallback, worker pool, transactional save | Raw HTTP calls, raw SQL details |
| `cli.py` | Argument parsing, logging config, user-facing progress/status, exit codes | Business logic |
| `nekoray_pool.py` | Windows-only NekoRay instance pool: profile discovery, isolated config copies, launch/stop, exit-IP probe | SQLite, crawl data |

### Control flow

1. `cli.main()` opens SQLite via `db.connect()` (WAL, foreign keys on, 30 s busy
   timeout) and constructs `api.Client` from CLI flags.
2. `crawl.ensure_initialized()` runs `schema.sql`, then lazily fetches reference
   metadata once per database, gated by `metadata` keys:
   sources, formats, languages, qael roles, statistics, and the sitemap seed.
3. Sitemap seeding downloads `sitemap-index.xml`, then each child sitemap,
   extracting every `/hadith/<id>/` into `crawl_status` (all stages `pending`).
   `--full-seed` additionally pages every source book through
   `ElasticHadithList`, resuming from `source_seed.next_page`.
4. `crawl.crawl()` selects IDs from `crawl_status`, fetches chunks of at least 16
   IDs with `ThreadPoolExecutor`, and calls `save_bundle()` in the main thread.
   `--skip-main` selects only rows whose main stage is not pending/failed, so
   translate/explanation/rejal keep progressing while the by-id quota is down.
5. `crawl.main_batch_crawl()` (`--main-batch`) selects `main_state IN
   ('pending','failed')` and posts up to 50 IDs per `ElasticHadithById` request,
   saving each returned item with `main=done`; omitted IDs stay pending for the
   single-ID path. It is the cheap way to clear the by-id backlog.
6. `crawl.bulk_crawl()` (`--bulk`) pages
   `POST /api/elastic/v2/ElasticHadithList` per book instead, saving each item
   through the same `save_bundle()` with `main=done`; resume is tracked in
   `source_seed.next_page`. It is the fallback for the by-id quota cap: list
   items carry no `tocInfo` (so `hadith_toc` stays empty), but the request sends
   `isGroup: true`, so `groupTogetherList` comes back and group members are
   written from the same pages. The list endpoint caps each `bookId` at 20,100
   items (page 402 at 50/page); a book resumed past that page is marked `done`
   with a `list capped at ...` note, and the overflow is left to `--main-batch`.
7. `status` prints table counts and stage-state histograms.

Proxy rotation is optional and outside the crawl pipeline: `api.Client` accepts
a proxy list, and `nekoray_pool.py` starts one isolated NekoRay instance per
profile and writes a list for `--proxies-file`. The helper is Windows-only and
requires elevation because `nekoray.exe` does.

### Per-hadith stage model

`db.STAGES = ("main", "translate", "explanation", "rejal")`. Each hadith has
four independent states in `crawl_status`:

`pending`, `done`, `fallback`, `failed`, `skipped`, `missing`.

- `_needs()` is the single source of truth: fetch `pending`/`failed`; include
  `fallback` only when `--enrich-fallback` (`enrich=True`).
- Primary main endpoint: `POST /api/elastic/ElasticHadithById`. Fallback:
  `GET /api/hadith/HadithById` (core text only, no group/toc IDs) -> `fallback`.
- Rejal primary: `HadithRejalList/v2`; fallback `HadithRejalList`.
- Translations/explanations have no fallback; failures stay `failed` for retry.
- Empty but successful responses become `skipped` when the count is zero.
- If main is `missing` (both endpoints returned empty without error), all four
  stages become `missing` and no further requests are made.
- If main fails, other stages are left alone and the whole hadith is retried on
  a later run.
- Enriching a `fallback` main row can re-fetch previously `skipped`
  translations/explanations if the primary response now reports a positive count.
- Same-chapter hadiths are never fetched. `hadith_toc` records the full
  `tocInfo` path; `hadith_leaf_toc` and `v_same_toc_hadith` derive relationships.

### Data write rules

- `save_bundle()` wraps one hadith's entire bundle in a single transaction
  (`with conn:`). Do not commit mid-hadith.
- Seeding loops commit after each page so an interruption resumes cleanly.
- A failed stage never deletes existing rows. A successful stage deletes only
  its own rows before rewriting them.
- `write_fallback_hadith()` uses `COALESCE` so legacy data fills gaps but never
  overwrites fields written by the primary endpoint.
- `write_group_members()` and `write_tocs()` no-op on empty input to preserve
  existing relations when a response omits them.
- Child rows require parents first: `ensure_source`, `ensure_language`,
  `ensure_qael`, `ensure_qael_role`, and `upsert_ravis` are called before
  foreign-keyed inserts.

### API surface

`SERVICE_URL = https://hadith.inoor.ir/service`. Key endpoints are wrapped in
`api.Client`: `SourceList`, `FormatList`, `LanguageList`, `QaelRoleList`,
`ElasticStatistics`, `ElasticHadithById`, `HadithById`, `TranslateList`,
`ExplanationList`, `HadithRejalList/v2`, `HadithRejalList`, and
`ElasticHadithList`. `ElasticHadithById` accepts a list of IDs, so
`api.Client.hadith_details()` fetches up to `MAIN_BATCH_SIZE` rows in one
request; `hadith_detail()` is the single-ID wrapper. `README.md` documents the
API-to-table mapping and useful SQL queries; treat it as the user-facing
reference. `ElasticHadithList` also accepts `tocInfo: [tocId]` to filter by
chapter and `isGroup: true` to include `groupTogetherList`; its page size is
capped at 50, and a `bookId` query stops at 20,100 items. `TocListBySource` and
`TocListByToc` return the chapter tree.

## Development & Tooling Commands

Run from the repository root (PowerShell on Windows; commands are portable).

```powershell
# Install/sync the environment (no third-party packages)
uv sync

# Run the crawler (creates or resumes hadith.db)
uv run python -m crawl_hadith crawl

# Local smoke test against a scratch database, no sitemap seeding
uv run python -m crawl_hadith crawl --db test.db --ids 105542

# Bounded run, then progress report
uv run python -m crawl_hadith crawl --limit 100
uv run python -m crawl_hadith status

# Retry primary endpoints for rows that used fallback
uv run python -m crawl_hadith crawl --enrich-fallback

# Bypass the capped by-id endpoint: page every book from the list endpoint
uv run python -m crawl_hadith crawl --bulk --proxies-file proxies.txt --delay 1.0

# While by-id is capped, fetch only translate/explanation/rejal
uv run python -m crawl_hadith crawl --skip-main --proxies-file proxies.txt

# Clear main-pending rows cheaply: up to 50 IDs per by-id request
uv run python -m crawl_hadith crawl --main-batch --limit 5000

# Rotate exit IPs through isolated NekoRay instances (Windows, one UAC prompt)
uv run python -m crawl_hadith.nekoray_pool start --profiles 0,3,6,22 --elevate
uv run python -m crawl_hadith crawl --proxies-file proxies.txt --workers 4 --delay 0.5
uv run python -m crawl_hadith.nekoray_pool stop --elevate

# Discover IDs missing from the sitemap (slow, ~61k listing requests)
uv run python -m crawl_hadith crawl --full-seed

# Tests: offline, temp SQLite, recorded fixtures
uv run python -m unittest discover -s tests -v

# One test (Python 3.12 supports -k)
uv run python -m unittest discover -s tests -k test_ingest_and_idempotent -v

# Syntax check when no linter is configured
uv run python -m compileall -q crawl_hadith tests
```

Crawl flags: `--db`, `--workers` (default 2), `--delay` (default 0.2 s, per
proxy when a pool is used), `--retries` (default 5), `--timeout` (default 60 s),
`--proxy`, `--proxies-file`, `--limit`, `--ids`, `--full-seed`, `--bulk`,
`--main-batch`, `--skip-main`, `--enrich-fallback`.

There is **no** lint, format, type-check, build, or CI configuration in the
repo. Match surrounding style and keep the stdlib-only constraint.

## Coding Conventions & Invariants

- **Python style:** `from __future__ import annotations` at the top of every
  module; builtin generics (`list[int]`, `dict[str, Any]`), `| None` unions,
  module-level `logger = logging.getLogger(__name__)`, snake_case functions,
  UPPER_SNAKE constants, private helpers prefixed `_`.
- **Layering:** respect the module table above. Parsing decisions belong in
  `parse.py`; SQL belongs in `db.py`; endpoint shapes belong in `api.py`.
- **SQL safety:** always parameterize values. Only internal constants/module
  names may be interpolated into SQL. Keep `PRAGMA foreign_keys = ON`; insert
  parent rows before child rows.
- **Transactions:** one `with conn:` transaction per hadith save; explicit
  per-page commits in seeding. Do not share the SQLite connection with worker
  threads. Workers only call `fetch_bundle`; the main thread writes results.
- **Stage state machine:** preserve the six allowed states and the `STAGES`
  order. Add new stages only by updating `schema.sql`, `db.STAGES`,
  `fetch_bundle`, `save_bundle`, status output, and tests together.
- **Resume safety:** never destroy committed data on a failed or partial
  response. Failed stages keep rows; fallback data must not be cleared by a
  failed enrichment attempt (`main_state == "fallback"` stays `fallback`).
- **Schema changes:** `schema.sql` is idempotent (`IF NOT EXISTS`). Additive
  columns need an explicit `ALTER TABLE` migration in `db.ensure_schema`
  (follow the existing `hadith_group_members.sort_order` pattern). Bumping
  `SCHEMA_VERSION` makes `ensure_schema` raise `RuntimeError` on existing
  databases, so only bump it with a deliberate migration path that updates
  `metadata.schema_version`.
- **Time:** persist timestamps with `db.utcnow()` (UTC ISO-8601, seconds).
- **Error handling:** expected transport/app errors become `ApiError`/
  `NotFound`; per-stage failures are recorded in `crawl_status`, not raised.
  Let truly unexpected bugs surface through the worker exception guard and
  `logger.exception`. Fast paths (404) are not retried; 403/408/425/429/5xx,
  timeouts, connection errors, and application-level 403/500 messages are.
  429s become `RateLimited`: with a proxy pool the proxy cools down (honoring
  `Retry-After`) and another is used; `fetch_bundle` skips the fallback
  endpoint on 429 so one throttled request does not become several.
  430 `Max Limit` becomes `MaxLimit`, which is deliberately **not** an
  `ApiError`: the quota is global (every exit IP fails at once), so it is never
  retried, never degraded to `fallback`, and aborts the run with exit 1 —
  otherwise a blocked run silently writes thousands of degraded rows.
- **Parsing defensiveness:** use the `as_*` helpers; skip malformed list items
  instead of aborting a whole crawl. `parse_hadith` and
  `parse_fallback_hadith` may raise `ValueError` when the payload lacks an ID.
- **CLI behavior:** exit `0` on success, `1` on API/database error, `130` on
  `KeyboardInterrupt`, `2` on argparse usage errors. Progress goes to stdout;
  errors to stderr.
- **Tests:** keep them offline. Use recorded fixtures plus fake clients as in
  `tests/test_pipeline.py`; mock `urllib.request.urlopen` for transport tests.
  New non-trivial branching/parsing logic should get a small unittest.
- **Dependencies:** do not add packages for HTTP, XML, SQLite, retries, or CLI;
  the standard library already covers them.
- **Ignored/generated:** never commit `*.db`, WAL/SHM files, `*.log`, `.venv`,
  or `__pycache__`.

## Session Operating Rules

Any agent working in this repository must:

1. Read `AGENTS.md` (this file), `README.md`, and the modules affected by the
   task before editing. Check callers, tests, and schema dependencies first.
2. Prefer the smallest complete change that preserves resumability and existing
   data. Do not silently change stored-data semantics or the stage model.
3. Run the offline test suite after changes. Do not run a live crawl or
   `--full-seed` unless the user explicitly asks; it is slow and network-heavy.
4. Never delete or rewrite `hadith.db`, and never commit database files.
5. **At the end of every task**, update the Session Log below:
   - Add a new bullet at the **top** of the list (reverse chronological,
     append-only). Never edit or remove earlier entries.
   - Use exactly: `YYYY-MM-DD - Brief goal: key changes made and pending follow-ups`.
   - Keep it to one concise bullet covering what changed, verification run, and
     any follow-up. If nothing is pending, say so.
   - This log is working memory for future sessions; be specific about files and
     unfinished work.

## Session Log

<!-- Newest entry first. Append-only; do not modify older entries. -->

- 2026-11-20 - Batched by-id + stage split: diagnosed the repeated 430 as the global by-id quota (exhausted at 09:33 after the overnight run; plain crawl stalls at the first main-pending ID 118851) and the list endpoint's 20,100-item `bookId` cap (the four largest books stuck at `source_seed.next_page=403`). Added `api.Client.hadith_details` (multi-ID POST), `crawl.main_batch_crawl` (`--main-batch`, 50 IDs/request, omitted IDs stay pending), `db.select_main_pending_ids`, `db.select_pending_ids(skip_main=True)` plus `crawl --skip-main` (never touches by-id), and bulk cap handling (a resume past page 402 marks the book `done` with a `list capped at ...` note). 27 offline tests pass (4 new: batch API, skip-main selection/fetch, main batch, bulk cap). Pending: run `--main-batch` when the quota resets, then `--skip-main`/normal crawl; toc backfill for the 313k bulk rows is still open.

- 2026-11-18 - Bulk group links: `api.source_hadith_page` now sends `isGroup: true`, so `ElasticHadithList` returns `groupTogetherList` and bulk mode fills `hadith_group_members`/`group_hadith_count` from the same pages; added a never-shrink guard in `db.write_group_members` (a partial payload cannot delete a larger stored group), `test_list_request_asks_for_groups`, and group assertions in the bulk test. Verified live on scratch DB book 628: 1,378 hadiths, 4,775 group rows, 1,378 groups, 0 toc rows; 23 offline tests pass. Pending: toc only - test whether `ElasticHadithById` accepts batched `hadithId` when its 430 quota clears; otherwise a chapter pass (`tocInfo: [tocId]` + `TocListBySource`/`TocListByToc`) is the fallback.

- 2026-11-18 - Option B bulk path: added `api.Client.source_hadith_page` (`/api/elastic/v2/ElasticHadithList`, `bookId`, 50/page; `source_hadith_ids` now wraps it) and `crawl.bulk_crawl` (`--bulk`) that saves each list item via `save_bundle` as `main=done`, resuming per book from `source_seed.next_page`; CLI branches to it and skips `crawl`. Deep paging verified to the last page of the largest book (70,018 hadiths). Smoke-tested book 628 against the live API on a scratch DB: 1,386/1,386 hadiths with source, text, qael, counts, cursor at page 29; 22 offline tests pass. Known gap: list items have no `tocInfo`/group links, so `hadith_toc` and `hadith_group_members` stay empty for bulk rows; by-id is still capped. Pending: run `crawl --bulk` on `hadith.db`, then plain `crawl` for translate/explanation/rejal.

- 2026-11-18 - Stop silent fallback bleed: `ElasticHadithById` hit a global HTTP 430 `Max Limit` quota, so every hadith silently degraded to `fallback` (88% of stored rows, no source/toc/qael, zero translations). Added `MaxLimit` in `api.py` (not an `ApiError`, so stage handlers cannot swallow it), 430 + application-level `max limit` detection, abort propagation in `crawl.py`, exit-1 message in `cli.py`; fallback now keeps the primary error in `last_error`; translation/explanation requests are skipped when the count is unknown. Stopped the live run, backed up to `hadith.backup-20261118.db`, reset 3,471 fallback rows to `pending` (rejal kept, nothing deleted). 21 offline tests pass. Pending: the by-id endpoint is still capped, so the corpus needs the `ElasticHadithList` (+`bookId`, `is_short=true`) bulk path; `tocInfo` is `null` there.

- 2026-11-18 - Proxy rotation and 429 handling: added `RateLimited`, HTTP proxy pool with per-proxy pacing/cooldown and `Retry-After` in `api.py`, skip main/rejal fallbacks on 429 in `crawl.py`, `--proxy`/`--proxies-file` in `cli.py`, Windows `nekoray_pool.py` helper (isolated instances, profile selection, exit-IP probe, `--elevate`/`--print-task`), tests, README/.gitignore; verified 19 offline tests pass and a live single-request check through NekoRay's HTTP port 2081. Pending: tune `--workers`/`--delay` on a real run; only four exit IPs were distinct (profiles 0/3/6/22).
- 2026-11-18 - Per-run logging: added appended `<db>.log` files in `cli.py` via a stdout/stderr tee and configured logger, recording each command, cwd, db path, progress, status, errors, and run end; added `test_cli_appends_run_log_with_command`, README logging docs, and `*.log` ignore. Verification: 9 offline tests pass. Pending: none.
- 2026-11-18 - Repository indexing and AGENTS.md creation: inspected all source modules, `schema.sql`, CLI, tests, fixtures, README, git state, and current DB crawl status; verified all 8 offline tests pass; documented architecture, commands, and invariants. Pending: none.
