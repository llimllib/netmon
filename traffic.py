#!/usr/bin/env -S uv run --script
# /// script
# requires-python = "==3.13.*"
# dependencies = ["cryptography", "duckdb==1.5.6", "requests"]
# [tool.uv]
# # duckdb 1.5.6 has no wheels for newer Pythons, and takes minutes to build
# no-build-package = ["duckdb"]
# ///
"""Record each Deco client's traffic every 15s, for Grafana

Appends one row per online client to ~/netmon/data/traffic/<date>.csv:
ts, mac, name, ip, conn, client_type, down_kbps, up_kbps. When the date
changes, earlier days are converted to Parquet and data older than
KEEP_DAYS is deleted. See docs/plans/2026-10-23-router-traffic-design.md
for how the Deco's API works.

Optional environment:
  DECO_HOST      the main Deco (default 192.168.68.1)
  DECO_PASSWORD  its password; by default it's read from the keychain item
                 netmon-deco
  DATA_DIR       where the data is (default ~/netmon/data)
  KEEP_DAYS      delete data older than this many days (default 30)

`traffic.py --init` creates the data directory and exits.
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
import time
from datetime import date
from pathlib import Path
from urllib.parse import quote

import duckdb
import requests
from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.asymmetric import padding as rsa_padding
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

DECO_HOST = os.environ.get("DECO_HOST", "192.168.68.1")
DATA = Path(os.environ.get("DATA_DIR", Path.home() / "netmon/data")) / "traffic"
KEEP_DAYS = int(os.environ.get("KEEP_DAYS", 30))
# views.sql assumes this when estimating bytes from rates
INTERVAL = 15
COLUMNS = ["ts", "mac", "name", "ip", "conn", "client_type", "down_kbps", "up_kbps"]
DAY_FILE = re.compile(r"\d{4}-\d{2}-\d{2}\.(csv|parquet)$")
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
    def __init__(self, host, password):
        self.base = f"http://{host}/cgi-bin/luci/;stok="
        self.password = password
        self.stok = None

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
        r = self.session.post(self.base + path, json={"operation": "read"}, timeout=5)
        r.raise_for_status()
        return r.json()["result"]

    def _post(self, path, body, login=False):
        data = self._encrypt(json.dumps(body).encode())
        sign = f"h={self.hash}&s={self.seq + len(data)}"
        if login:
            sign = f"k={self.key}&i={self.iv}&" + sign
        # Form-encoded, but labeled as JSON like the web UI's requests. The
        # Deco rejects the same body labeled as a form.
        r = self.session.post(
            f"{self.base}{self.stok or ''}{path}",
            data=f"sign={rsa_encrypt(self.sign_key, sign.encode())}&data={quote(data, safe='')}",
            headers={"Content-Type": "application/json; charset=UTF-8"},
            timeout=5,
        )
        r.raise_for_status()
        return json.loads(self._decrypt(r.json()["data"]))

    def login(self):
        self.session = requests.Session()
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

    def clients(self):
        """The online clients of every Deco in the mesh. A failed request
        means logging in again next time."""
        if not self.stok:
            self.login()
        try:
            res = self._post("/admin/client?form=client_list",
                             {"operation": "read", "params": {"device_mac": "default"}})
            if res.get("error_code"):
                raise RuntimeError(f"client list failed: {res}")
        except Exception:
            self.stok = None
            raise
        return [c for c in res["result"]["client_list"] if c.get("online")]


def password():
    if pw := os.environ.get("DECO_PASSWORD"):
        return pw
    return subprocess.run(
        ["security", "find-generic-password", "-s", "netmon-deco", "-w"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()


def log(msg):
    print(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}", flush=True)


def init():
    """Create the data directory, and the files that keep views.sql's globs
    from matching nothing, which would fail to create the traffic view"""
    DATA.mkdir(parents=True, exist_ok=True)
    empty_csv = DATA / "_empty.csv"
    if not empty_csv.exists():
        empty_csv.write_text(",".join(COLUMNS) + "\n")
    empty_parquet = DATA / "_empty.parquet"
    if not empty_parquet.exists():
        duckdb.sql(f"COPY ({select(empty_csv)}) TO '{empty_parquet}' (FORMAT parquet)")


def select(csv_path):
    """A query reading a CSV with the columns' real types"""
    return f"""
        SELECT ts::TIMESTAMPTZ AS ts, mac, name, ip, conn, client_type,
               down_kbps::DOUBLE AS down_kbps, up_kbps::DOUBLE AS up_kbps
        FROM read_csv('{csv_path}', header = true,
                      columns = {{{", ".join(f"'{c}': 'VARCHAR'" for c in COLUMNS)}}})
    """


def tidy(today):
    """Convert earlier days' CSVs to Parquet, and delete expired data"""
    for path in sorted(DATA.glob("*.csv")):
        if not DAY_FILE.fullmatch(path.name) or path.name == f"{today}.csv":
            continue
        pq = path.with_suffix(".parquet")
        tmp = path.with_suffix(".parquet.tmp")
        try:
            duckdb.sql(f"COPY ({select(path)}) TO '{tmp}' (FORMAT parquet, COMPRESSION zstd)")
            written = duckdb.sql(f"SELECT count(*) FROM read_parquet('{tmp}')").fetchone()[0]
            expected = duckdb.sql(f"SELECT count(*) FROM ({select(path)})").fetchone()[0]
            if written != expected:
                raise RuntimeError(f"{written} rows written, {expected} expected")
        except Exception as e:
            tmp.unlink(missing_ok=True)
            log(f"failed to convert {path.name}: {e}")
            continue
        # Keep the CSV's mtime, which expiry goes by
        st = path.stat()
        os.utime(tmp, (st.st_atime, st.st_mtime))
        tmp.rename(pq)
        path.unlink()
    cutoff = time.time() - KEEP_DAYS * 86400
    for path in DATA.iterdir():
        if DAY_FILE.fullmatch(path.name) and path.stat().st_mtime < cutoff:
            path.unlink()


def row(ts, c):
    return [
        ts,
        c["mac"],
        base64.b64decode(c.get("name", "")).decode(errors="replace"),
        c.get("ip", ""),
        c.get("connection_type", ""),
        c.get("client_type", ""),
        c.get("down_speed", ""),
        c.get("up_speed", ""),
    ]


def main():
    init()
    if "--init" in sys.argv:
        return
    deco = Deco(DECO_HOST, password())
    day = None
    last_error = None
    while True:
        # Sleep until the next quarter minute, so samples line up across days
        time.sleep(INTERVAL - time.time() % INTERVAL)
        now = time.time()
        ts = time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(now))

        today = date.fromtimestamp(now).isoformat()
        if today != day:
            tidy(today)
            day = today

        try:
            clients = deco.clients()
        except LoginRefused as e:
            log(f"{e}; waiting an hour before trying again")
            last_error = str(e)
            time.sleep(3600)
            continue
        except Exception as e:
            # Log each new kind of error once, not on every poll
            if str(e) != last_error:
                log(f"skipping polls until the Deco answers: {e}")
                last_error = str(e)
            continue
        if last_error:
            log("the Deco is answering again")
            last_error = None

        path = DATA / f"{today}.csv"
        new = not path.exists()
        with path.open("a", newline="") as f:
            w = csv.writer(f)
            if new:
                w.writerow(COLUMNS)
            w.writerows(row(ts, c) for c in clients)


if __name__ == "__main__":
    main()
