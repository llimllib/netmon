-- List outages found in ~/netmon/data (Parquet for past days, CSV for today).
--
--   duckdb -c ".read outages.sql"
--
-- A second is "bad" if any target failed to reply. Bad seconds less than
-- 5s apart are merged into one outage. Outages with fewer than 3 bad
-- seconds are dropped as noise; change the 3 at the bottom to see them.
--
-- pings, bad_seconds and outages are tables rather than views because
-- they're referenced more than once; as views, every reference re-read
-- and re-unpivoted all the CSVs.

-- Cast CSV columns the way to_parquet.sh does, so both agree on types
CREATE OR REPLACE TEMP TABLE pings AS
SELECT regexp_extract(filename, '([^/]+)-\d{4}-\d{2}-\d{2}\.\w+$', 1) AS host,
       * EXCLUDE (filename)
FROM (
  SELECT * FROM read_parquet('~/netmon/data/*.parquet', union_by_name = true,
                             filename = true)
  UNION ALL BY NAME
  SELECT ts::TIMESTAMPTZ AS ts, COLUMNS(* EXCLUDE (ts, filename))::DOUBLE, filename
  FROM read_csv('~/netmon/data/*.csv', all_varchar = true, union_by_name = true,
                filename = true)
);

-- One row per host, second and target. UNPIVOT drops NULLs, so failures
-- are stored as -1 first.
CREATE OR REPLACE TEMP VIEW results AS
SELECT host, ts, replace(target, '_ms', '') AS target, nullif(ms, -1) AS ms
FROM (UNPIVOT (SELECT host, ts, coalesce(COLUMNS('_ms$'), -1) FROM pings)
      ON COLUMNS('_ms$') INTO NAME target VALUE ms);

CREATE OR REPLACE TEMP TABLE bad_seconds AS
SELECT host, ts, list(target ORDER BY target) AS failed
FROM results
WHERE ms IS NULL
  -- union_by_name gives NULLs for columns another host's files don't have
  AND (host, target) IN (SELECT host, target FROM results WHERE ms IS NOT NULL)
GROUP BY host, ts;

CREATE OR REPLACE TEMP TABLE outages AS
WITH marked AS (
  SELECT *,
         coalesce(ts - lag(ts) OVER w > INTERVAL 5 SECOND, true) AS is_start
  FROM bad_seconds
  WINDOW w AS (PARTITION BY host ORDER BY ts)
), numbered AS (
  SELECT *, sum(is_start::int) OVER (PARTITION BY host ORDER BY ts) AS n
  FROM marked
)
SELECT host,
       min(ts) AS start,
       max(ts) AS "end",
       max(ts) - min(ts) + INTERVAL 1 SECOND AS duration,
       count(*) AS bad_secs,
       arg_min(failed, ts) AS first_failed,
       list_sort(list_distinct(flatten(list(failed)))) AS all_failed
FROM numbered
GROUP BY host, n;

CREATE OR REPLACE TEMP VIEW report AS
SELECT o.host,
       strftime(o.start, '%Y-%m-%d %H:%M:%S') AS start,
       o.duration,
       o.bad_secs,
       o.first_failed,
       o.all_failed,
       CASE
         WHEN list_contains(o.all_failed, 'deco') THEN 'LAN / Wi-Fi: lost the main Deco'
         WHEN list_contains(o.all_failed, 'isp_gw') THEN 'modem / Spectrum link'
         WHEN list_contains(o.all_failed, 'cf') AND list_contains(o.all_failed, 'google')
           THEN 'Spectrum upstream'
         WHEN o.all_failed = ['dns'] THEN 'DNS only'
         ELSE 'partial'
       END AS diagnosis,
       -- What the other monitors saw during the same window
       (SELECT string_agg(DISTINCT p.host || ': ' || array_to_string(p.all_failed, '+'), ', '
                          ORDER BY p.host || ': ' || array_to_string(p.all_failed, '+'))
        FROM outages p
        WHERE p.host != o.host
          AND p.start <= o."end" + INTERVAL 5 SECOND
          AND p."end" >= o.start - INTERVAL 5 SECOND) AS other_hosts,
       o.start AS start_ts,
       o."end" AS end_ts
FROM outages o
WHERE o.bad_secs >= 3;

SELECT * EXCLUDE (start_ts, end_ts) FROM report ORDER BY start_ts, host;
