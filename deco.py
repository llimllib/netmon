#!/usr/bin/env -S uv run --script
# /// script
# requires-python = "==3.13.*"
# dependencies = ["cryptography", "duckdb==1.5.6"]
# [tool.uv]
# # duckdb 1.5.6 has no wheels for newer Pythons, and takes minutes to build
# no-build-package = ["duckdb"]
# ///
"""Record what the Deco mesh reports, for Grafana

Every 15s, appends to these tables in ~/netmon/data/<table>/<date>.csv:

  traffic     one row per online client: its rates, and which Deco it's on
  deco_nodes  one row per Deco: its parent, backhaul signal and internet status
  deco_wan    one row: the main Deco's WAN link, DHCP state and address

and copies new lines from the main Deco's system log to deco_log, a few
pages per poll. The Deco keeps only an hour or two of log.

When the date changes, earlier days are converted to Parquet and data older
than KEEP_DAYS is deleted. See docs/plans/ for how the Deco's API works.

Optional environment:
  DECO_HOST      the main Deco (default 192.168.68.1)
  DECO_PASSWORD  its password; by default it's read from the keychain item
                 netmon-deco
  DATA_DIR       where the data is (default ~/netmon/data)
  KEEP_DAYS      delete data older than this many days (default 30)

`deco.py --init` creates the tables' directories and exits.
"""

import base64
import csv
import hashlib
import json
import os
import re
import secrets
import subprocess
import sys
import tempfile
import time
from datetime import date
from pathlib import Path
from urllib.parse import quote

import duckdb
from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.asymmetric import padding as rsa_padding
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

DECO_HOST = os.environ.get("DECO_HOST", "192.168.68.1")
DATA = Path(os.environ.get("DATA_DIR", Path.home() / "netmon/data"))
KEEP_DAYS = int(os.environ.get("KEEP_DAYS", 30))
# views.sql assumes this when estimating bytes from rates
INTERVAL = 15
DAY_FILE = re.compile(r"\d{4}-\d{2}-\d{2}\.(csv|parquet)$")
TS_FORMAT = "%Y-%m-%dT%H:%M:%S%z"
# Wrong password, too many attempts, locked out. Retrying soon would only
# extend the lockout.
LOGIN_REFUSED = {-5002, -5003, -5007}


class LoginRefused(Exception):
    pass


def rsa_encrypt(key, data: bytes) -> str:
    """Encrypt with an [n, e] hex key from the Deco, in chunks as long as
    the key allows, concatenated as hex"""
    n, e = key
    pub = rsa.RSAPublicNumbers(int(e, 16), int(n, 16)).public_key()
    step = pub.key_size // 8 - 11
    return "".join(pub.encrypt(data[i:i + step], rsa_padding.PKCS1v15()).hex()
                   for i in range(0, len(data), step))


class Deco:
    """A session with the main Deco. It allows one at a time, so logging in
    here logs out anything else, such as its web UI, and vice versa."""

    def __init__(self, host, password):
        self.base = f"http://{host}/cgi-bin/luci/;stok="
        self.password = password
        self.stok = None
        # Counts logins, so callers can tell their session's state is gone
        self.session = 0
        self.cookies = Path(tempfile.mkdtemp(prefix="netmon-deco")) / "cookies"

    def _curl(self, url, body, content_type):
        # Under launchd, macOS's Local Network privacy blocks Python from
        # reaching the Deco until it's allowed in System Settings, and
        # again after uv updates Python. Apple's curl is exempt.
        r = subprocess.run(
            ["/usr/bin/curl", "-sS", "--fail", "-m", "5",
             "-b", self.cookies, "-c", self.cookies,
             "-H", f"Content-Type: {content_type}", "--data-binary", "@-", url],
            input=body, capture_output=True, text=True,
        )
        if r.returncode:
            raise RuntimeError(r.stderr.strip())
        return json.loads(r.stdout)

    def _aes(self):
        return Cipher(algorithms.AES(self.key.encode()), modes.CBC(self.iv.encode()))

    def _encrypt(self, data: bytes) -> str:
        p = padding.PKCS7(128).padder()
        e = self._aes().encryptor()
        return base64.b64encode(
            e.update(p.update(data) + p.finalize()) + e.finalize()).decode()

    def _decrypt(self, data: str) -> bytes:
        d = self._aes().decryptor()
        u = padding.PKCS7(128).unpadder()
        return u.update(d.update(base64.b64decode(data)) + d.finalize()) + u.finalize()

    def _post_plain(self, path):
        return self._curl(self.base + path, '{"operation": "read"}',
                          "application/json")["result"]

    def _post(self, path, body, login=False):
        data = self._encrypt(json.dumps(body).encode())
        sign = f"h={self.hash}&s={self.seq + len(data)}"
        if login:
            sign = f"k={self.key}&i={self.iv}&" + sign
        # Form-encoded, but labeled as JSON like the web UI's requests. The
        # Deco rejects the same body labeled as a form.
        res = self._curl(
            f"{self.base}{self.stok or ''}{path}",
            f"sign={rsa_encrypt(self.sign_key, sign.encode())}&data={quote(data, safe='')}",
            "application/json; charset=UTF-8",
        )
        return json.loads(self._decrypt(res["data"]))

    def login(self):
        self.cookies.unlink(missing_ok=True)
        self.stok = None
        password_key = self._post_plain("/login?form=keys")["password"]
        auth = self._post_plain("/login?form=auth")
        self.sign_key, self.seq = auth["key"], auth["seq"]
        self.key = "".join(secrets.choice("0123456789") for _ in range(16))
        self.iv = "".join(secrets.choice("0123456789") for _ in range(16))
        self.hash = hashlib.md5(("admin" + self.password).encode()).hexdigest()
        res = self._post("/login?form=login", {
            "operation": "login",
            "params": {"password": rsa_encrypt(password_key, self.password.encode())},
        }, login=True)
        code = res.get("error_code")
        if code in LOGIN_REFUSED:
            raise LoginRefused(f"login refused: {res}")
        if code:
            raise RuntimeError(f"login failed: {res}")
        self.stok = res["result"]["stok"]
        self.session += 1

    def read(self, path, params=None, operation="read"):
        """A request's result. A failed request means logging in again next
        time, since another login may have ended this session."""
        if not self.stok:
            self.login()
        body = {"operation": operation}
        if params is not None:
            body["params"] = params
        try:
            res = self._post(path, body)
            if res.get("error_code"):
                raise RuntimeError(f"{path} failed: {res}")
        except Exception:
            self.stok = None
            raise
        return res.get("result")


class Table:
    """A day's CSV per date in DATA/<name>/, and Parquet for earlier days.
    Columns not in types are text."""

    def __init__(self, name, columns, types=None):
        self.dir = DATA / name
        self.columns = columns
        self.types = {"ts": "TIMESTAMPTZ", **(types or {})}

    def init(self, today):
        """Create the files that keep views.sql's globs from matching
        nothing, which would fail to create its views. They're rewritten
        every time, so they follow column changes. Today's CSV gets any new
        columns too."""
        self.dir.mkdir(parents=True, exist_ok=True)
        empty_csv = self.dir / "_empty.csv"
        tmp = self.dir / "_empty.tmp"
        tmp.write_text(",".join(self.columns) + "\n")
        tmp.rename(empty_csv)
        duckdb.sql(f"COPY ({self.select(empty_csv)}) TO '{tmp}' (FORMAT parquet)")
        tmp.rename(self.dir / "_empty.parquet")
        path = self.dir / f"{today}.csv"
        if path.exists():
            with path.open(newline="") as f:
                reader = csv.DictReader(f)
                if reader.fieldnames != self.columns:
                    rows = list(reader)
                    tmp = path.with_suffix(".csv.tmp")
                    with tmp.open("w", newline="") as out:
                        w = csv.DictWriter(out, self.columns, extrasaction="ignore")
                        w.writeheader()
                        w.writerows(rows)
                    tmp.rename(path)

    def select(self, csv_path):
        """A query reading a CSV with the columns' real types"""
        cols = ", ".join(f"{c}::{self.types[c]} AS {c}" if c in self.types else c
                         for c in self.columns)
        all_text = ", ".join(f"'{c}': 'VARCHAR'" for c in self.columns)
        return f"""
            SELECT {cols}
            FROM read_csv('{csv_path}', header = true, columns = {{{all_text}}})
        """

    def append(self, day, rows):
        path = self.dir / f"{day}.csv"
        new = not path.exists()
        with path.open("a", newline="") as f:
            w = csv.writer(f)
            if new:
                w.writerow(self.columns)
            w.writerows(rows)

    def tidy(self, today):
        """Convert earlier days' CSVs to Parquet, and delete expired data"""
        for path in sorted(self.dir.glob("*.csv")):
            if not DAY_FILE.fullmatch(path.name) or path.name == f"{today}.csv":
                continue
            pq = path.with_suffix(".parquet")
            tmp = path.with_suffix(".parquet.tmp")
            try:
                duckdb.sql(f"COPY ({self.select(path)}) TO '{tmp}' "
                           "(FORMAT parquet, COMPRESSION zstd)")
                written = duckdb.sql(f"SELECT count(*) FROM read_parquet('{tmp}')").fetchone()[0]
                expected = duckdb.sql(f"SELECT count(*) FROM ({self.select(path)})").fetchone()[0]
                if written != expected:
                    raise RuntimeError(f"{written} rows written, {expected} expected")
            except Exception as e:
                tmp.unlink(missing_ok=True)
                log(f"failed to convert {self.dir.name}/{path.name}: {e}")
                continue
            # Keep the CSV's mtime, which expiry goes by
            st = path.stat()
            os.utime(tmp, (st.st_atime, st.st_mtime))
            tmp.rename(pq)
            path.unlink()
        cutoff = time.time() - KEEP_DAYS * 86400
        for path in self.dir.iterdir():
            if DAY_FILE.fullmatch(path.name) and path.stat().st_mtime < cutoff:
                path.unlink()


TRAFFIC = Table("traffic", ["ts", "mac", "name", "ip", "conn", "client_type", "deco",
                            "down_kbps", "up_kbps"],
                {"down_kbps": "DOUBLE", "up_kbps": "DOUBLE"})
NODES = Table("deco_nodes", ["ts", "mac", "name", "ip", "role", "parent", "backhaul",
                             "signal_2g", "signal_5g", "signal_6g", "inet_status",
                             "inet_error"],
              {"signal_2g": "INTEGER", "signal_5g": "INTEGER", "signal_6g": "INTEGER"})
WAN = Table("deco_wan", ["ts", "link", "dial", "inet", "error_code", "ip", "gateway",
                         "dns1", "dns2"],
            {"error_code": "INTEGER"})
LOG = Table("deco_log", ["ts", "facility", "severity", "process", "pid", "message"],
            {"pid": "INTEGER"})
TABLES = [TRAFFIC, NODES, WAN, LOG]


def poll(deco, ts):
    """One row set per table, from one round of requests"""
    nodes = deco.read("/admin/device?form=device_list")["device_list"]
    by_mac = {n["mac"]: n for n in nodes}
    traffic, node_rows = [], []
    for n in nodes:
        # Each Deco's own client list says which clients are on it; together
        # they're the whole network's
        for c in deco.read("/admin/client?form=client_list", {"device_mac": n["mac"]})["client_list"]:
            if c.get("online"):
                traffic.append([
                    ts, c["mac"],
                    base64.b64decode(c.get("name", "")).decode(errors="replace"),
                    c.get("ip", ""), c.get("connection_type", ""), c.get("client_type", ""),
                    n.get("nickname", ""), c.get("down_speed", ""), c.get("up_speed", ""),
                ])
        signal = n.get("signal_level") or {}
        node_rows.append([
            ts, n["mac"], n.get("nickname", ""), n.get("device_ip", ""), n.get("role", ""),
            # The upstream Deco's MAC; the main one has none
            by_mac.get(n.get("previous"), {}).get("nickname", ""),
            "+".join(n.get("connection_type") or []),
            signal.get("band2_4", ""), signal.get("band5", ""), signal.get("band6", ""),
            n.get("inet_status", ""), n.get("inet_error_msg", ""),
        ])
    internet = deco.read("/admin/network?form=internet")
    ipv4 = internet.get("ipv4", {})
    wan = deco.read("/admin/network?form=wan_ipv4")["wan"]["ip_info"]
    wan_row = [ts, internet.get("link_status", ""), ipv4.get("dial_status", ""),
               ipv4.get("inet_status", ""), ipv4.get("error_code", ""),
               wan.get("ip", ""), wan.get("gateway", ""), wan.get("dns1", ""), wan.get("dns2", "")]
    return traffic, node_rows, [wan_row]


LOG_PAGE = "/admin/log_export?form=feedback_log"
LOG_LINE = re.compile(r"(\w{3} \w{3} +\d+ \d\d:\d\d:\d\d \d{4}) (\w+)\.(\w+) "
                      r"([^:\[]+?)(?:\[(\d+)\])?: ?(.*)", re.S)


def parse_log_line(line, prev_epoch):
    """(epoch, facility, severity, process, pid, message). A line that doesn't
    parse keeps the previous line's time, and is all message."""
    m = LOG_LINE.fullmatch(line)
    if not m:
        return prev_epoch, "", "", "", "", line
    # The Deco's clock is in local time, like this Mac's
    epoch = int(time.mktime(time.strptime(m[1], "%a %b %d %H:%M:%S %Y")))
    return epoch, m[2], m[3], m[4], m[5] or "", m[6]


class LogReader:
    """Copies new log lines to LOG. The Deco returns the log as pages of a
    snapshot, oldest first, and each page takes about a second, so a few
    pages are read per poll, newest first, until reaching lines already
    copied. The snapshot lasts until the next one is built, but not past a
    new login."""

    PAGES_PER_POLL = 3
    EVERY = 300

    def __init__(self):
        # The newest line copied: its time, and how many lines had that time
        # (repeats are common, so lines can't be told apart by content)
        self.last, self.at_last = latest_log_time()
        self.started = 0
        self.reset()

    def reset(self):
        self.next_page = None
        self.lines = []

    def step(self, deco, now, today):
        if self.next_page is None:
            if now - self.started < self.EVERY:
                return
            # Snapshot all levels; the rest can be filtered out later
            deco.read(LOG_PAGE, {"level": 8}, operation="build")
            self.session = deco.session
            self.started = now
            first = deco.read(LOG_PAGE, {"index": 0, "limit": 100})
            self.next_page = first["totalNum"] - 1
        elif deco.session != self.session:
            # A new login loses the snapshot
            self.reset()
            return
        for _ in range(self.PAGES_PER_POLL):
            page = deco.read(LOG_PAGE, {"index": self.next_page, "limit": 100})["logList"]
            lines = [e["content"] for e in page] if page else []
            self.lines[:0] = lines
            self.next_page -= 1
            oldest = parse_log_line(lines[0], None)[0] if lines else None
            if self.next_page < 0 or (self.last and oldest and oldest < self.last):
                self.write(today)
                return

    def write(self, today):
        rows, prev, seen_at_last = [], None, 0
        for line in self.lines:
            epoch, *fields = parse_log_line(line, prev)
            prev = epoch
            if epoch is None or (self.last and epoch < self.last):
                continue
            if self.last and epoch == self.last:
                seen_at_last += 1
                if seen_at_last <= self.at_last:
                    continue
            rows.append((epoch, fields))
        # Into today's file even if they're from yesterday, whose CSV may
        # already be Parquet
        LOG.append(today, [[time.strftime(TS_FORMAT, time.localtime(e)), *f] for e, f in rows])
        if rows:
            newest = rows[-1][0]
            self.at_last = (seen_at_last if newest == self.last else 0) + \
                sum(1 for e, _ in rows if e == newest)
            self.last = newest
        self.reset()


def latest_log_time():
    """The newest copied log line's time, and how many lines have it"""
    row = duckdb.sql(f"""
        WITH t AS (
          SELECT ts FROM read_parquet('{LOG.dir}/*.parquet')
          UNION ALL
          SELECT ts FROM ({LOG.select(f'{LOG.dir}/*.csv')})
        )
        SELECT epoch(max(ts))::BIGINT, count(*) FILTER (WHERE ts = (SELECT max(ts) FROM t))
        FROM t
    """).fetchone()
    return (row[0], row[1]) if row[0] is not None else (None, 0)


def password():
    if pw := os.environ.get("DECO_PASSWORD"):
        return pw
    return subprocess.run(
        ["security", "find-generic-password", "-s", "netmon-deco", "-w"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()


def log(msg):
    print(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}", flush=True)


def main():
    today = date.today().isoformat()
    for t in TABLES:
        t.init(today)
    if "--init" in sys.argv:
        return
    deco = Deco(DECO_HOST, password())
    reader = LogReader()
    day = None
    last_error = None
    while True:
        # Sleep until the next quarter minute, so samples line up across days
        time.sleep(INTERVAL - time.time() % INTERVAL)
        now = time.time()
        ts = time.strftime(TS_FORMAT, time.localtime(now))

        today = date.fromtimestamp(now).isoformat()
        if today != day:
            for t in TABLES:
                t.tidy(today)
            day = today

        try:
            for table, rows in zip([TRAFFIC, NODES, WAN], poll(deco, ts)):
                table.append(today, rows)
            reader.step(deco, now, today)
        except LoginRefused as e:
            log(f"{e}; waiting an hour before trying again")
            last_error = str(e)
            time.sleep(3600)
            continue
        except Exception as e:
            reader.reset()
            # Log each new kind of error once, not on every poll
            if str(e) != last_error:
                log(f"skipping polls until the Deco answers: {e}")
                last_error = str(e)
            continue
        if last_error:
            log("the Deco is answering again")
            last_error = None


if __name__ == "__main__":
    main()
