# crawl_hadith

Crawler for [hadith.inoor.ir](https://hadith.inoor.ir/), storing hadiths, their
parallel sources, translations, explanations, chapter paths, and rejal/sanad
data in SQLite.

The site is an Angular application backed by a JSON service, so the crawler
calls the API directly. No browser or external Python package is required.

## Quick start

```powershell
# Python 3.12 (the project uses uv)
uv run python -m crawl_hadith crawl
```

This creates `hadith.db`, fetches the reference metadata, seeds hadith IDs from
the site sitemap, and crawls every pending hadith. The process is resumable:
stop it at any time and run the same command again.

Useful commands:

```powershell
# Try a single hadith in a scratch database
uv run python -m crawl_hadith crawl --db test.db --ids 105542

# Crawl only 100 pending hadiths
uv run python -m crawl_hadith crawl --limit 100

# Show progress and row counts
uv run python -m crawl_hadith status

# Retry primary endpoints for rows that fell back to the legacy main/rejal API
uv run python -m crawl_hadith crawl --enrich-fallback

# Add IDs that are not in the sitemap by scanning every source book
uv run python -m crawl_hadith crawl --full-seed
```

Useful flags for `crawl`:

| Flag | Meaning |
|---|---|
| `--db PATH` | SQLite file (default `hadith.db`) |
| `--workers N` | Parallel HTTP workers (default 2) |
| `--delay SEC` | Delay before each request (default 0.2; per proxy when a pool is used) |
| `--retries N` | Attempts per request before fallback (default 5) |
| `--timeout SEC` | Per-request timeout (default 60) |
| `--proxy URL` | HTTP proxy URL; repeat for a rotating pool |
| `--proxies-file PATH` | File with one HTTP proxy URL per line (`#` comments ignored) |
| `--limit N` | Process at most N pending/failed rows |
| `--ids A,B,C` | Process only these IDs; skips sitemap seeding |
| `--full-seed` | Scan all source books to discover missing IDs |
| `--bulk` | Fetch hadiths book by book from the list endpoint instead of by ID |
| `--enrich-fallback` | Retry primary endpoints for `fallback` stages |

## Run log

Every `crawl` and `status` run appends to a `.log` file next to the database:

- `hadith.db` → `hadith.log`
- `test.db` → `test.log`

Each run starts with the command, working directory, database path, and log
path, then captures progress, warnings, errors, and the final status table.
Log files are git-ignored.

## Proxy rotation (NekoRay)

The service rate-limits by exit IP. `crawl` can rotate a pool of HTTP proxies;
each proxy is paced separately, at most one request is in flight per proxy, and
a proxy that returns 429/403 cools down (honoring `Retry-After`) while another
one is used. On 429 the main/rejal fallback endpoints are skipped, so a
throttled request is not retried against a second endpoint on the same IP.

NekoRay runs one active profile per process. The bundled Windows helper starts
one isolated NekoRay instance per profile, each with its own local ports:

```powershell
# Show profile IDs and names
uv run python -m crawl_hadith.nekoray_pool list

# Start four verified exits (DE-PP, DE-YT, TR, AL) and probe their exit IPs
uv run python -m crawl_hadith.nekoray_pool start --profiles 0,3,6,22 --elevate

# Crawl through the generated list
uv run python -m crawl_hadith crawl --proxies-file proxies.txt --workers 4 --delay 0.5

# Stop the pool
uv run python -m crawl_hadith.nekoray_pool stop --elevate
```

`--elevate` triggers one UAC prompt for the whole batch; all child NekoRay
processes inherit it. For zero prompts, open an elevated terminal once, run
`start --print-task`, then run the printed `schtasks` command in that same
terminal. It registers a highest-privilege logon task; afterwards the pool
starts silently (or via `schtasks /Run /TN NekoRayPool`).

Notes:

- NekoRay's `nekoray.exe` requires administrator rights, so the helper must run
  elevated. The crawler itself does not.
- The helper copies NekoRay's config into
  `%LOCALAPPDATA%\crawl_hadith\nekoray-pool`; it does not modify the main
  instance or its TUN/system-proxy settings.
- Only HTTP proxies are supported (stdlib `urllib` has no SOCKS support). Use
  the HTTP inbound port (`2081` on the main instance), not the SOCKS port.
- Only four unique exit IPs were found among the current NekoRay profiles;
  more profiles do not create more rate-limit budget.
- Start with `--workers 4 --delay 0.5` and lower the delay only if 429s stay
  away. `--delay` is per proxy when a pool is used.

## Data collected

| Tab / data | API |
|---|---|
| Main hadith, source, volume, page, qael, toc path, group members | `POST /api/elastic/ElasticHadithById` |
| Translations | `GET /api/hadith/TranslateList?hadithId=` |
| Explanations (`شرح`) | `GET /api/hadith/ExplanationList?hadithId=` |
| Rejal / sanad chains | `GET /api/hadith/HadithRejalList/v2?hadithId=` |
| Book metadata | `GET /api/hadith/SourceList` |
| Formats, languages, qael roles, statistics | `GET /api/meta/FormatList`, `LanguageList`, `QaelRoleList`, `Statistics` |

`احادیث هم باب` is not fetched per hadith. Each hadith records its complete
`tocInfo` path, so same-chapter hadiths are derived with a SQL view.

## Resume, retry, and fallback

Each hadith has four independent stages: `main`, `translate`, `explanation`,
and `rejal`. Every stage is in one of these states:

`pending`, `done`, `fallback`, `failed`, `skipped`, `missing`.

- Requests retry with exponential backoff on timeouts, HTTP 403/429/5xx, and
  application-level 403/500 responses. With a proxy pool, a 429/403 puts that
  proxy on cooldown and the request retries through another one; `Retry-After`
  is honored.
- A 429 skips the main `HadithById` fallback and the rejal v1 fallback. Both
  endpoints share the same IP and limit, so retrying them only adds load; the
  stage stays `failed` for the next run.
- HTTP 430 `Max Limit` is a server-side query quota, not an IP limit: every exit
  IP fails at once and it does not clear quickly. `crawl` stops with exit code 1
  and writes no `fallback` rows; rerun later when the endpoint answers again.
  This is why the exact same response must never be retried or rotated through
  proxies.
- The main endpoint falls back to `GET /api/hadith/HadithById?id=`. That legacy
  endpoint has core text but no group/toc IDs, so the stage becomes `fallback`.
  Running with `--enrich-fallback` later tries the primary endpoint again. When
  the fallback succeeds, the primary error is still kept in
  `crawl_status.last_error` (visible in the error queries), so a blocked primary
  is never silent.
- Rejal falls back from `HadithRejalList/v2` to `HadithRejalList`.
- Translations and explanations have no alternate endpoint. They retry, then
  remain `failed` for the next run. When the translation/explanation count is
  unknown (a `fallback` main row), no request is made; the stage becomes
  `skipped` and enrichment re-fetches it once the count is known.
- A failed stage never deletes existing rows. A successful stage replaces only
  its own rows.
- `crawl` automatically selects `pending` and `failed` rows. Only the stages
  that need work are fetched, so an interruption after the main hadith does not
  cause the main hadith to be fetched again.

The optional `--full-seed` uses a separate `source_seed` table and resumes each
book from its last completed page.

## Bulk mode

`POST /api/elastic/ElasticHadithById` is capped by a server-side daily quota
(HTTP 430 `Max Limit`), and the cap is global: rotating proxies does not help.
`crawl --bulk` therefore pages `POST /api/elastic/v2/ElasticHadithList` book by
book (`bookId`, 50 per page) and saves each item through the normal
`save_bundle`, exactly as if the by-id endpoint had returned it.

```powershell
uv run python -m crawl_hadith crawl --bulk --proxies-file proxies.txt --delay 1.0
uv run python -m crawl_hadith crawl      # fills translate/explanation/rejal
```

- Resume lives in `source_seed`: one row per book, `next_page` advances only
  after a page is committed, and a failed book is retried on the next run.
- A page returns the full record (text, source, book, volume, page, author,
  qael list, group id, translate/explanation counts), so main rows are written
  as `done` with their counts.
- It covers the whole corpus in ~7,900 requests instead of 393k, and it also
  discovers IDs that are missing from the sitemap.
- **Gap:** list items carry no `tocInfo` and no group links, so `hadith_toc` and
  `hadith_group_members` stay empty for these rows. Those columns need a
  separate by-id pass once the quota allows it (reset `main_state` to `pending`
  for the rows to revisit).
- Translations, explanations, and rejal are not fetched in bulk mode; the
  following plain `crawl` run fills them stage by stage.

## Schema

Reference data:

- `metadata` — schema version, seed timestamps, site statistics
- `sources`, `translation_sources`, `explanation_sources`
- `formats`, `languages`, `qael_roles`, `qael`
- `tocs` — chapter nodes collected from crawled hadith paths

Hadith data:

- `hadiths` — main text (raw markup + `text_plain`), book/volume/page, counts, links
- `hadith_group_members` — parallel narrations across books
- `hadith_qael` — speakers and roles
- `hadith_toc` — complete chapter path, with depth
- `translations`, `explanations` — text, source, language, volume/page, Noorlib link
- `ravis` — global narrator profiles (`infoList`/`infoList2` as JSON)
- `hadith_sanad`, `hadith_sanad_ravi` — relational sanad chains

Operational tables:

- `crawl_status` — one stage state per hadith
- `source_seed` — resume progress for `--full-seed`

### Same-chapter query

```sql
SELECT h.hadith_id, h.text_plain
FROM v_same_toc_hadith v
JOIN hadiths h ON h.hadith_id = v.same_toc_hadith_id
WHERE v.hadith_id = 105542;
```

### Useful queries

```sql
-- Sources of a hadith (including the hadith itself)
SELECT m.*
FROM hadith_group_members seed
JOIN hadith_group_members m ON m.group_id = seed.group_id
WHERE seed.hadith_id = 105542;

-- Sanad chain for one hadith
SELECT s.sanad_index, s.segment_order, s.segment_type, s.title,
       r.ravi_id, r.hint
FROM hadith_sanad s
LEFT JOIN hadith_sanad_ravi r
  ON r.hadith_id = s.hadith_id
 AND r.sanad_index = s.sanad_index
 AND r.segment_order = s.segment_order
WHERE s.hadith_id = 105542
ORDER BY s.sanad_index, s.segment_order, r.ravi_index;
```

## Notes and limitations

- The sitemap currently lists about 391,090 unique hadith IDs; the site reports
  about 393,320. `--full-seed` scans source books to add the remainder; that
  discovery pass makes roughly 61,000 listing requests before crawling starts.
- A full crawl makes roughly 0.8–1.6 million requests and can take hours to
  days depending on worker count and server responses.
- The database will be multiple gigabytes for a full crawl.
- Rate limiting is handled with an optional HTTP proxy pool
  (`--proxies-file`) and per-proxy cooldowns. The current NekoRay subscription
  exposes only four distinct exit IPs, so the gain is bounded; if the service
  throttles globally, rotation will not help. See "Proxy rotation (NekoRay)".
- Aggressive `--workers` values can still trigger 429s; use `--delay` to pace
  each proxy.
- Related/similar hadiths, Noorlib/Noormags tabs, and `HadithInBook` listings
  are not stored. `HadithInBook` is derivable from source/volume/page; the
  other tabs are outside the agreed scope.
- Ravi profiles are global reference records, not duplicated per hadith. The
  nested `infoList`/`infoList2` metadata is kept as JSON in `ravis.info_json`.

## Tests

```powershell
uv run python -m unittest discover -s tests -v
```

The tests use recorded API fixtures and a temporary SQLite database; they do
not touch the network.
