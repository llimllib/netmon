-- List outages found in ~/netmon/data. See views.sql for how they're found.
--
--   cd ~/netmon && duckdb -c ".read outages.sql"

.read views.sql
SELECT * EXCLUDE (start_ts, end_ts) FROM report ORDER BY start_ts, host;
