# Status & progress

1. Stage histogram: every stage x state

```sql
WITH stages(stage, state) AS (
  SELECT 'main', main_state FROM crawl_status
  UNION ALL SELECT 'translate', translate_state FROM crawl_status
  UNION ALL SELECT 'explanation', explanation_state FROM crawl_status
  UNION ALL SELECT 'rejal', rejal_state FROM crawl_status
)
SELECT stage, state, count(*) AS n
FROM stages
GROUP BY stage, state
ORDER BY stage, n DESC;
```

2. One-line progress + recent throughput (rows touched in last 10/60 min)

```sql
SELECT
  count(*) AS seeded,
  sum(main_state = 'pending') AS pending,
  sum(main_state = 'done') AS done,
  sum(main_state = 'fallback') AS fallback,
  round(100.0 * sum(main_state <> 'pending') / count(*), 2) AS pct_main_touched,
  sum(datetime(updated_at) >= datetime('now', '-10 minutes')) AS rows_last_10m,
  sum(datetime(updated_at) >= datetime('now', '-60 minutes')) AS rows_last_60m
FROM crawl_status;
```
Rate ≈ rows_last_10m / 600 hadith/s. ETA ≈ pending / rate.

3. ID ranges by main state (spot blocked/odd ranges)

```sql
SELECT main_state, count(*) AS n, min(hadith_id) AS first_id, max(hadith_id) AS last_id
FROM crawl_status
GROUP BY main_state
ORDER BY n DESC;
```

4. Fallback share of stored hadiths (data quality)

```sql
SELECT cs.main_state, count(*) AS n,
       round(100.0 * count(*) / (SELECT count(*) FROM hadiths), 1) AS pct
FROM hadiths h
JOIN crawl_status cs USING (hadith_id)
GROUP BY cs.main_state
ORDER BY n DESC;
```

# Errors & retries

5. Failure reasons
```sql
SELECT substr(last_error, 1, 80) AS error, count(*) AS n
FROM crawl_status
WHERE 'failed' IN (main_state, translate_state, explanation_state, rejal_state)
GROUP BY error
ORDER BY n DESC
LIMIT 20;
```

6. Failed rows, newest first

```sql
SELECT hadith_id, main_state, translate_state, explanation_state, rejal_state,
       attempts, last_error, updated_at
FROM crawl_status
WHERE 'failed' IN (main_state, translate_state, explanation_state, rejal_state)
ORDER BY updated_at DESC
LIMIT 50;
```

7. Fallback rows waiting for --enrich-fallback

```sql
SELECT count(*) AS to_enrich, min(hadith_id) AS first_id, max(hadith_id) AS last_id
FROM crawl_status
WHERE main_state = 'fallback';

SELECT hadith_id FROM crawl_status
WHERE main_state = 'fallback'
ORDER BY hadith_id
LIMIT 20;
```
# Per-source coverage

8. Which books have been crawled (fallback rows have NULL source_id)
```sql
SELECT coalesce(h.source_id, -1) AS source_id,
       coalesce(s.title, '(unknown / fallback)') AS title,
       count(*) AS hadiths,
       sum(cs.main_state = 'done') AS done,
       sum(cs.main_state = 'fallback') AS fallback
FROM hadiths h
JOIN crawl_status cs USING (hadith_id)
LEFT JOIN sources s ON s.id = h.source_id
GROUP BY 1
ORDER BY hadiths DESC
LIMIT 30;
```

9. Per-source queue progress (requires --full-seed; empty otherwise)

```sql
SELECT status, count(*) AS books, sum(next_page - 1) AS pages_seeded
FROM source_seed
GROUP BY status;
```
# Content counts

10. Row counts per table
```sql
SELECT 'hadiths' AS table_name, count(*) AS rows FROM hadiths
UNION ALL SELECT 'translations', count(*) FROM translations
UNION ALL SELECT 'explanations', count(*) FROM explanations
UNION ALL SELECT 'sources', count(*) FROM sources
UNION ALL SELECT 'tocs', count(*) FROM tocs
UNION ALL SELECT 'hadith_toc', count(*) FROM hadith_toc
UNION ALL SELECT 'hadith_group_members', count(*) FROM hadith_group_members
UNION ALL SELECT 'hadith_qael', count(*) FROM hadith_qael
UNION ALL SELECT 'hadith_sanad', count(*) FROM hadith_sanad
UNION ALL SELECT 'hadith_sanad_ravi', count(*) FROM hadith_sanad_ravi
UNION ALL SELECT 'ravis', count(*) FROM ravis
ORDER BY rows DESC;
```

11. What percentage of hadiths has each relation

```sql
SELECT count(*) AS hadiths,
       sum(source_id IS NULL) AS no_source,
       sum(toc_text IS NULL) AS no_toc_text,
       sum(hadith_id IN (SELECT hadith_id FROM hadith_toc)) AS with_toc,
       sum(hadith_id IN (SELECT hadith_id FROM hadith_sanad)) AS with_sanad,
       sum(hadith_id IN (SELECT hadith_id FROM hadith_group_members)) AS with_group,
       sum(hadith_id IN (SELECT hadith_id FROM hadith_qael)) AS with_qael,
       sum(translate_count > 0) AS has_translations,
       sum(explanation_count > 0) AS has_explanations
FROM hadiths;
```

12. Why translate/explanation are ignored

```sql
SELECT CASE
         WHEN translate_count IS NULL THEN 'unknown (fallback row)'
         WHEN translate_count = 0 THEN '0 reported'
         ELSE '> 0 reported'
       END AS translate_count,
       count(*) AS n
FROM hadiths
GROUP BY 1
ORDER BY n DESC;
```

13. Translations / explanations by language

```sql
SELECT l.title AS language, count(*) AS n
FROM translations t LEFT JOIN languages l ON l.id = t.language_id
GROUP BY l.title ORDER BY n DESC;

SELECT l.title AS language, count(*) AS n
FROM explanations e LEFT JOIN languages l ON l.id = e.language_id
GROUP BY l.title ORDER BY n DESC;
```

# Structure: tocs, sanad, groups

14. Toc depth distribution
```sql
SELECT depth, count(*) AS n FROM hadith_toc GROUP BY depth ORDER BY depth;
```

15. Same-chapter pairs (the view emits both directions)

```sql
SELECT count(*) AS directed_pairs, count(DISTINCT hadith_id) AS hadiths
FROM v_same_toc_hadith;

SELECT toc_id, count(*) AS same_count
FROM v_same_toc_hadith
GROUP BY toc_id
ORDER BY same_count DESC
LIMIT 10;
```

16. Top narrators

```sql
SELECT r.name, count(*) AS segments
FROM hadith_sanad_ravi sr
JOIN ravis r ON r.ravi_id = sr.ravi_id
GROUP BY r.ravi_id
ORDER BY segments DESC
LIMIT 15;
```

17. Largest hadith groups

```sql
SELECT group_id, count(*) AS members
FROM hadith_group_members
GROUP BY group_id
ORDER BY members DESC
LIMIT 10;
```

18. Metadata (schema version, seed markers) + DB size
```sql
SELECT meta_key, meta_value FROM metadata ORDER BY meta_key;
SELECT round((SELECT * FROM pragma_page_count())
           * (SELECT * FROM pragma_page_size()) / 1048576.0, 1) AS size_mb;
```


One thing I saw while validating
Your 11:19 run has added ~950 hadiths, all of them main=fallback with source_id NULL (done stayed at 418), at 0.5/s. The log has no warnings, so ElasticHadithById is returning an empty data list for the new ID range and the crawler quietly falls back to HadithById — the same "fallback" state that lacks toc/group/qael. Queries 3, 4, and 8 will track it. Want me to dig into why the elastic endpoint returns empty for IDs above ~20393 before you let this run for days?