#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = ["duckdb==1.5.6"]
# ///
"""Serve a dashboard of the netmon data at http://localhost:8766
(set NETMON_PORT to use another port)

Every 30s it rsyncs the Mac mini's data into ./data, so the charts stay
current while it runs.
"""

import json
import os
import re
import subprocess
import threading
import time
from datetime import date, datetime, timedelta
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import duckdb

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
REMOTE_HOST = "qfwfq"
REMOTE = f"{REMOTE_HOST}.local:netmon/data/"
PORT = int(os.environ.get("NETMON_PORT", 8766))
MAX_POINTS = 1500
BUCKETS = [1, 2, 5, 10, 15, 30, 60, 120, 300, 600, 900, 1800, 3600, 7200]


def sync_loop():
    while True:
        # Mirror only the Mac mini's files, deleting ones it has removed (CSVs
        # it has converted to Parquet, or expired data), and skip *.tmp files
        # from a conversion in progress
        r = subprocess.run(
            ["rsync", "-a", "--delete", "--timeout=10",
             f"--include={REMOTE_HOST}-*.csv", f"--include={REMOTE_HOST}-*.parquet",
             "--exclude=*", REMOTE, f"{DATA}/"],
            capture_output=True,
            text=True,
        )
        if r.returncode:
            print(f"rsync failed: {r.stderr.strip()}", flush=True)
        time.sleep(30)


def bucket_for(span):
    """Smallest bucket size (seconds) that keeps a chart under MAX_POINTS"""
    return next((b for b in BUCKETS if span / b <= MAX_POINTS), BUCKETS[-1])


def host_files(start, end):
    """Map host -> data files that may have rows between start and end"""
    first = datetime.fromtimestamp(start).date() - timedelta(days=1)
    last = datetime.fromtimestamp(end).date() + timedelta(days=1)
    hosts = {}
    for f in sorted(DATA.glob("*-*")):
        m = re.fullmatch(r"(.+)-(\d{4}-\d{2}-\d{2})\.(csv|parquet)", f.name)
        if m and first <= date.fromisoformat(m[2]) <= last:
            hosts.setdefault(m[1], []).append(str(f))
    return hosts


def sql_str(s):
    return "'" + s.replace("'", "''") + "'"


def read_files(files):
    """SQL reading CSV and Parquet files with the same column types.
    Matches to_parquet.sh: every column but ts is a DOUBLE."""
    file_list = lambda fs: "[" + ", ".join(map(sql_str, fs)) + "]"
    pq = [f for f in files if f.endswith(".parquet")]
    csv = [f for f in files if f.endswith(".csv")]
    parts = []
    if pq:
        parts.append(f"SELECT * FROM read_parquet({file_list(pq)}, union_by_name = true)")
    if csv:
        parts.append(f"""
            SELECT ts::TIMESTAMPTZ AS ts, COLUMNS(* EXCLUDE (ts))::DOUBLE
            FROM read_csv({file_list(csv)}, all_varchar = true, union_by_name = true)
        """)
    return " UNION ALL BY NAME ".join(parts)


def host_series(con, files, start, end, b):
    """Average latency, loss % and Wi-Fi stats per bucket of b seconds"""
    con.execute(f"CREATE OR REPLACE TEMP VIEW raw AS {read_files(files)}")
    cols = [r[0] for r in con.execute("DESCRIBE raw").fetchall()]
    targets = [c.removesuffix("_ms") for c in cols if c.endswith("_ms")]
    extras = [c for c in ("rssi", "tx_rate") if c in cols]

    aggs = [f'avg("{t}_ms")' for t in targets]
    aggs += [f'100 * avg(("{t}_ms" IS NULL)::int)' for t in targets]
    aggs += [f'avg("{c}")' for c in extras]
    first = int(start) // b * b
    # Join against every bucket in the range so missing data shows as a gap
    rows = con.execute(f"""
        WITH buckets AS (
          SELECT floor(epoch(ts) / {b})::bigint * {b} AS t, {", ".join(aggs)}
          FROM raw
          WHERE ts >= to_timestamp({start}) AND ts < to_timestamp({end})
          GROUP BY 1
        )
        SELECT * FROM (SELECT range AS t FROM range({first}, {int(end)}, {b}))
        LEFT JOIN buckets USING (t)
        ORDER BY t
    """).fetchall()

    columns = list(zip(*rows)) or [[] for _ in range(1 + 2 * len(targets) + len(extras))]
    rnd = lambda xs: [None if x is None else round(x, 2) for x in xs]
    n = len(targets)
    return {
        "t": list(columns[0]),
        "targets": targets,
        "latency": {t: rnd(columns[1 + i]) for i, t in enumerate(targets)},
        "loss": {t: rnd(columns[1 + n + i]) for i, t in enumerate(targets)},
        "wifi": {c: rnd(columns[1 + 2 * n + i]) for i, c in enumerate(extras)},
    }


def get_data(start, end):
    b = bucket_for(end - start)
    con = duckdb.connect()
    hosts = {
        host: host_series(con, files, start, end, b)
        for host, files in host_files(start, end).items()
    }
    return {"bucket": b, "hosts": hosts}


def get_outages():
    # outages.sql reads both *.csv and *.parquet, and DuckDB errors on a glob
    # that matches nothing
    if not (any(DATA.glob("*.csv")) and any(DATA.glob("*.parquet"))):
        return []
    con = duckdb.connect()
    con.execute((ROOT / "outages.sql").read_text())
    cur = con.execute("""
        SELECT host, epoch(start_ts) AS start, epoch(end_ts) AS "end",
               bad_secs, first_failed, all_failed, diagnosis, other_hosts
        FROM report ORDER BY start_ts DESC
    """)
    names = [d[0] for d in cur.description]
    return [dict(zip(names, row)) for row in cur.fetchall()]


class Handler(SimpleHTTPRequestHandler):
    def do_GET(self):
        url = urlparse(self.path)
        try:
            if url.path == "/api/data":
                q = parse_qs(url.query)
                return self.send_json(get_data(float(q["from"][0]), float(q["to"][0])))
            if url.path == "/api/outages":
                return self.send_json(get_outages())
        except Exception as e:
            self.send_error(500, str(e))
            raise
        super().do_GET()

    def send_json(self, obj):
        body = json.dumps(obj).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        pass


if __name__ == "__main__":
    threading.Thread(target=sync_loop, daemon=True).start()
    handler = partial(Handler, directory=str(ROOT / "static"))
    print(f"http://localhost:{PORT}")
    ThreadingHTTPServer(("127.0.0.1", PORT), handler).serve_forever()
