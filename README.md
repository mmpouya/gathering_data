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
| `--delay SEC` | Delay before each request in each worker (default 0.2) |
| `--retries N` | Attempts per request before fallback (default 5) |
| `--timeout SEC` | Per-request timeout (default 60) |
| `--limit N` | Process at most N pending/failed rows |
| `--ids A,B,C` | Process only these IDs; skips sitemap seeding |
| `--full-seed` | Scan all source books to discover missing IDs |
| `--enrich-fallback` | Retry primary endpoints for `fallback` stages |

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
  application-level 403/500 responses.
- The main endpoint falls back to `GET /api/hadith/HadithById?id=`. That legacy
  endpoint has core text but no group/toc IDs, so the stage becomes `fallback`.
  Running with `--enrich-fallback` later tries the primary endpoint again.
- Rejal falls back from `HadithRejalList/v2` to `HadithRejalList`.
- Translations and explanations have no alternate endpoint. They retry, then
  remain `failed` for the next run.
- A failed stage never deletes existing rows. A successful stage replaces only
  its own rows.
- `crawl` automatically selects `pending` and `failed` rows. Only the stages
  that need work are fetched, so an interruption after the main hadith does not
  cause the main hadith to be fetched again.

The optional `--full-seed` uses a separate `source_seed` table and resumes each
book from its last completed page.

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
- Rate limiting is intentionally not solved here. Retries and fallback make
  progress recoverable, but aggressive `--workers` values may trigger 403s.
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
