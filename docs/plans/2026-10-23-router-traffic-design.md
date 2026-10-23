# Router traffic in Grafana

Show how much traffic the network carries, in total and per device, next to
the outage data, so you can tell whether the link was saturated when an
outage started and which device was using it.

## Where the numbers come from

Neither router-ish box offers a standard interface:

- The **modem** (Technicolor CVA4001, 192.168.100.1) doesn't answer HTTP or
  SNMP from the LAN.
- The **main Deco** (XE75 Pro, firmware 1.5.0 Build 20260603) doesn't
  answer SNMP, but serves the same local API its web UI at
  http://192.168.68.1/webpages/index.html uses.

So the data comes from the Deco's API. Its client list gives each client's
current download and upload rate. There are no byte counters, so volume is
estimated from sampled rates.

## The Deco API

Reverse-engineered from the web UI's JavaScript (`js/libs/tpEncrypt.js`,
`js/su/frame.js` and `modules/login/localLogin/`). Every request is a POST
to `http://192.168.68.1/cgi-bin/luci/;stok=<stok><path>`, with an empty
`stok` before login.

1. `/login?form=keys`, body `{"operation": "read"}` in plain JSON, returns
   `result.password`: a 1024-bit RSA key `[n, e]` in hex, for the password.
2. `/login?form=auth`, likewise, returns `result.key`, a 512-bit RSA key
   for signatures, and `result.seq`, a number.
3. Make a 16-digit AES key and IV (random decimal digits, used as their
   ASCII bytes).
4. `/login?form=login` with
   `{"operation": "login", "params": {"password": <hex RSA(password)>}}`.

From step 4 on, request bodies are encrypted. The JSON is encrypted with
AES-128-CBC and PKCS#7 padding, and base64-encoded (`data`). The signature
is the RSA encryption (PKCS#1 v1.5) of
`k=<key>&i=<iv>&h=<md5("admin" + password)>&s=<seq + len(data)>`, in 53-byte
chunks, hex-encoded and concatenated (`sign`). Only the login request
includes `k=…&i=…&`. The body is form-encoded (`sign=…&data=…`) but sent
with **`Content-Type: application/json`**, as jQuery does. With a
form-encoded content type the Deco answers `no such callback`. Responses
are `{"data": <base64 AES>}`, decrypted with the same key.

Login returns `result.stok` and sets a `sysauth` cookie. The password is
the owner's TP-Link ID password. Logging in doesn't log out the Deco app.
After about 7 wrong passwords the Deco locks logins for a while.

The client list is `/admin/client?form=client_list` with
`{"operation": "read", "params": {"device_mac": "default"}}`. That covers
clients of every Deco in the mesh. Each client has:

- `name`, base64-encoded
- `mac` (`1C-B3-…`), `ip`
- `connection_type`: `wired`, `band2_4`, `band5` or `band6`
- `client_type`, e.g. "Media Player"
- `online`
- `down_speed` and `up_speed`, in **kbit/s**

The rate units were calibrated against a 1,023,714 B/s (8.19 Mbit/s) curl
download, which the Deco reported as 8,519–8,783. That's about 5% more,
since the Deco counts protocol overhead. Its rates lag by a few seconds.

## Design

**Collector.** `traffic.py`, a `uv` script, runs on qfwfq as the
LaunchAgent `com.llimllib.netmon.traffic`. It reads the password from
qfwfq's keychain (`netmon-deco`). It logs in once and reuses the session
until a request fails, then logs in again on the next poll. Every 15s, on
the quarter minute, it appends one row per online client to
`data/traffic/<date>.csv`:

    ts, mac, name, ip, conn, client_type, down_kbps, up_kbps

The files live in a subdirectory because `views.sql` combines every
`data/*.csv` and `data/*.parquet` into `pings`. When the date changes, the
collector converts earlier days to Parquet (keeping the text columns as
text, unlike `to_parquet.sh`) and deletes data older than `KEEP_DAYS` (30).

**Errors.** If the Deco can't be reached, that poll is skipped. Errors are
logged when they first happen and when they clear, not on every poll. If
the Deco rejects the password, or reports that logins are locked, the
collector waits an hour before trying again, so it can't lock you out.

**Views.** `views.sql` gets a `traffic` view over those files. It adds:

- `client`: a display name from `client_names`, or the Deco's name plus the
  MAC's last four digits, since several devices share names like "iPhone"
- `down_bytes` and `up_bytes`: an estimate of the bytes moved in each
  sample, `kbps × 1000 / 8 × 15`

DuckDB fails to create a view whose glob matches no files, and
`views.sql` is Grafana's init SQL, so that would break every panel. The
collector therefore keeps a zero-row `_empty.parquet` and a header-only
`_empty.csv` in `data/traffic/`.

**Dashboard.** A "Traffic" row at the bottom of the netmon dashboard:

1. **Internet throughput**: the sum of all clients' rates, download up and
   upload down.
2. **Top clients**: download rate per client, stacked, for the 8 busiest in
   the time range plus "other".
3. **Data used**: estimated bytes down and up per client in the time range.

Outage annotations appear on these panels like on the others.

## Limits

- Volumes are estimates. Bursts shorter than a poll are smoothed out, and
  the Deco's rates include protocol overhead.
- Totals only cover clients, so traffic from the Decos themselves is
  missing.
- The API is undocumented, so a firmware update could break it. The
  collector would then log errors and the traffic panels would go empty.
  Pings and outages don't depend on it.

## Testing

The Deco has no test mode, so testing is against the real one:

- Run `traffic.py` on qfwfq with a temporary `DATA_DIR`, then check rows
  arrive and the `traffic` view reads them.
- Convert a fake past day's CSV and compare row counts.
- Check that `views.sql` loads with only the `_empty` files present.
- After deploying, check `traffic.log` and the panels.
