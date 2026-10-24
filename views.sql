-- netmon's data as views, shared by outages.sql, dashboard.py and Grafana:
--
--   pings   every row from every monitor, with a host column. Parquet for
--           past days, CSV for today.
--   results one row per host, second and target
--   report  outages, with a diagnosis and what the other monitors saw
--   traffic     each Deco client's traffic, and which Deco it's on, every
--               15s, from deco.py
--   deco_nodes  each Deco's parent, backhaul signal and internet status,
--               every 15s
--   deco_wan    the main Deco's WAN link, DHCP state and address, every 15s
--   deco_log    the main Deco's system log
--
-- A second is "bad" if any target failed to reply. Bad seconds less than
-- 5s apart are merged into one outage. Outages with fewer than 3 bad
-- seconds are dropped as noise; change the 3 in report to see them.
--
-- Paths start with ~, so Grafana's DuckDB data source must set its home
-- directory to the home directory netmon runs in.

-- Cast CSV columns the way to_parquet.sh does, so both agree on types
CREATE OR REPLACE VIEW pings AS
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

-- What to call each host on the dashboard
CREATE OR REPLACE VIEW host_names AS
SELECT * FROM (VALUES
  ('qfwfq', 'Living room Mac mini (wired)'),
  ('adama', 'Laptop (Wi-Fi via Office)')
) t(host, name);

-- Each host's columns, from its files. Parquet schemas and CSV headers are
-- cheap to read, unlike the rows.
CREATE OR REPLACE VIEW host_columns AS
SELECT DISTINCT
       regexp_extract(file, '([^/]+)-\d{4}-\d{2}-\d{2}\.\w+$', 1) AS host, col
FROM (
  SELECT file_name AS file, name AS col
  FROM parquet_schema('~/netmon/data/*.parquet')
  UNION ALL
  SELECT filename, unnest(string_split(split_part(content, chr(10), 1), ','))
  FROM read_text('~/netmon/data/*.csv')
);

CREATE OR REPLACE VIEW host_targets AS
SELECT host, replace(col, '_ms', '') AS target
FROM host_columns
WHERE suffix(col, '_ms');

-- One row per host, second and target. UNPIVOT drops NULLs, so failures
-- are stored as -1 first. union_by_name gives NULLs for columns another
-- host's files don't have, so only keep the host's own targets.
CREATE OR REPLACE VIEW results AS
SELECT host, ts, replace(target, '_ms', '') AS target, nullif(ms, -1) AS ms
FROM (UNPIVOT (SELECT host, ts, coalesce(COLUMNS('_ms$'), -1) FROM pings)
      ON COLUMNS('_ms$') INTO NAME target VALUE ms)
WHERE (host, replace(target, '_ms', '')) IN (SELECT host, target FROM host_targets);

CREATE OR REPLACE VIEW bad_seconds AS
SELECT host, ts, list(target ORDER BY target) AS failed
FROM results
WHERE ms IS NULL
GROUP BY host, ts;

CREATE OR REPLACE VIEW outages AS
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
GROUP BY host, n
-- The laptop reaches everything else through the satellite Decos, so if
-- only they failed, the path through them still worked; they just didn't
-- answer pings to their own addresses
HAVING NOT list_has_all(['hallway', 'office'], all_failed);

CREATE OR REPLACE VIEW report AS
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

-- What to call Deco clients on the dashboard, by MAC. Unlisted clients get
-- the Deco's name and the end of their MAC (unless the name has it
-- already), since names like "iPhone" repeat.
CREATE OR REPLACE VIEW client_names AS
SELECT * FROM (VALUES
  ('14-98-77-35-A5-81', 'Living room Mac mini')
) t(mac, name);

-- Rates are in kbit/s. Bytes are estimated from them: deco.py samples
-- every 15s. deco.py keeps an empty file of each type in each of its
-- tables' directories, because a glob matching nothing would fail to
-- create these views.
CREATE OR REPLACE VIEW traffic AS
SELECT t.*,
       coalesce(n.name,
                CASE WHEN contains(upper(replace(t.name, '-', '')), mac_end) THEN t.name
                     ELSE t.name || ' ' || mac_end END) AS client,
       t.down_kbps * 1000 / 8 * 15 AS down_bytes,
       t.up_kbps * 1000 / 8 * 15 AS up_bytes
FROM (
  SELECT * FROM read_parquet('~/netmon/data/traffic/*.parquet', union_by_name = true)
  UNION ALL BY NAME
  SELECT ts::TIMESTAMPTZ AS ts, * EXCLUDE (ts, down_kbps, up_kbps),
         down_kbps::DOUBLE AS down_kbps, up_kbps::DOUBLE AS up_kbps
  FROM read_csv('~/netmon/data/traffic/*.csv', all_varchar = true, union_by_name = true)
) t
LEFT JOIN client_names n USING (mac),
LATERAL (SELECT replace(right(t.mac, 5), '-', '') AS mac_end);

-- Signal levels are the Deco app's bars, 0-3 or so. backhaul lists the
-- bands a satellite's backhaul can use.
CREATE OR REPLACE VIEW deco_nodes AS
SELECT * FROM read_parquet('~/netmon/data/deco_nodes/*.parquet', union_by_name = true)
UNION ALL BY NAME
SELECT ts::TIMESTAMPTZ AS ts, * EXCLUDE (ts, signal_2g, signal_5g, signal_6g),
       signal_2g::INTEGER AS signal_2g, signal_5g::INTEGER AS signal_5g,
       signal_6g::INTEGER AS signal_6g
FROM read_csv('~/netmon/data/deco_nodes/*.csv', all_varchar = true, union_by_name = true);

-- Normally link 'plugged', dial 'connected', inet 'online'
CREATE OR REPLACE VIEW deco_wan AS
SELECT * FROM read_parquet('~/netmon/data/deco_wan/*.parquet', union_by_name = true)
UNION ALL BY NAME
SELECT ts::TIMESTAMPTZ AS ts, * EXCLUDE (ts, error_code), error_code::INTEGER AS error_code
FROM read_csv('~/netmon/data/deco_wan/*.csv', all_varchar = true, union_by_name = true);

-- Most lines are from nrd, the mesh's client steering, which is noisy
CREATE OR REPLACE VIEW deco_log AS
SELECT * FROM read_parquet('~/netmon/data/deco_log/*.parquet', union_by_name = true)
UNION ALL BY NAME
SELECT ts::TIMESTAMPTZ AS ts, * EXCLUDE (ts, pid), pid::INTEGER AS pid
FROM read_csv('~/netmon/data/deco_log/*.csv', all_varchar = true, union_by_name = true);
