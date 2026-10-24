# Mesh, WAN and log data from the Deco

The [traffic collector](2026-10-23-router-traffic-design.md) already logs
in to the main Deco every 15s. The Deco reports more that can help explain
an outage:

- each satellite's backhaul (which Deco it connects through, and signal)
- which Deco each client is on
- whether the main Deco thinks the internet is up
- its system log

`traffic.py` became `deco.py` (LaunchAgent `com.llimllib.netmon.deco`), and
collects all of it.

## The API

The same encrypted requests as before (see the traffic design). The ones
used, and what they took on 2026-10-23:

| Path | Body | Gives | Time |
|------|------|-------|------|
| `/admin/device?form=device_list` | read | each Deco: `nickname`, `role` (master/slave), `previous` (upstream Deco's MAC), `connection_type` (backhaul bands), `signal_level` per band, `inet_status`, `inet_error_msg` | 0.7s |
| `/admin/client?form=client_list` | read, `{"device_mac": <a Deco's MAC>}` | that Deco's clients, with rates | 0.7s each |
| `/admin/network?form=internet` | read | `link_status` (plugged), and `ipv4`'s `dial_status` (connected) and `inet_status` (online) | 0.4s |
| `/admin/network?form=wan_ipv4` | read | WAN `ip`, `gateway`, `dns1`, `dns2` | 0.5s |
| `/admin/network?form=performance` | read | `cpu_usage`, `mem_usage` (fractions) | 4.6s |
| `/admin/log_export?form=feedback_log` | `build`, `{"level": 8}` | builds a snapshot of the log (level 8 is everything; 3 is errors and worse) | |
| same | read, `{"index": i, "limit": 100}` | page `i` of the snapshot, oldest first, and `totalNum` pages | 1.2s |

The per-Deco client lists, together, have exactly the clients of
`device_mac: "default"`, so they replace it.

**The Deco allows one admin session at a time.** A login ends the previous
session, whose next request fails to decrypt. Everything that talks to the
Deco has to be in one process, and opening its web UI interrupts the
collector (which logs in again on its next poll). The phone app isn't
affected.

**The log** is a ring of about 8,650 lines, which covered 1.6 hours. About
90 lines a minute, three-quarters from `nrd` (mesh client steering). The
rest includes Wi-Fi associations and disassociations by client MAC
(`client_mgmt`, `hostapd`), DHCP (`udhcpd`), and connections to the
satellites. Lines look like:

    Fri Oct 23 19:54:11 2026 daemon.err udhcpd[9628]: Sending ACK to 192.168.68.54

in local time. A snapshot stays the same until the next build, even with
other requests in between, but not past a new login.

## Design

Each poll, `deco.py` makes the device list, client list (one per Deco),
internet and WAN requests, about 3.5s, and appends to:

- `traffic`: as before, plus `deco`, the Deco the client is on
- `deco_nodes`: per Deco, `ts, mac, name, ip, role, parent, backhaul,
  signal_2g, signal_5g, signal_6g, inet_status, inet_error`
- `deco_wan`: `ts, link, dial, inet, error_code, ip, gateway, dns1, dns2`

The performance request is left out: it takes 4.6s, and CPU load wasn't
needed yet.

**Log.** Every 5 minutes it builds a snapshot, then reads 3 pages per poll,
newest first, until it reaches a page that starts before the newest line
already copied. Then it appends the new lines to `deco_log`:
`ts, facility, severity, process, pid, message`. Identical lines in the
same second are common, so lines can't be identified by content. Instead
it remembers the newest copied time and how many lines had it, and on
startup works that out from the files. Lines go in the current day's file
even if they're from the day before, whose CSV may already be Parquet.
Everything is kept, `nrd` included; it compresses well and can't be
recovered later. A first run copies the whole buffer, about 29 polls.

**Tables.** All four tables work like `traffic`: a CSV per day in
`data/<table>/`, earlier days converted to Parquet, `KEEP_DAYS` expiry, and
`_empty` files so `views.sql` loads before there's data. The `_empty` files
are rewritten on every start, so they follow column changes, and today's
CSV gets any new columns.

**Dashboard.** A "Mesh and WAN" row:

- the main Deco's internet connection (cable, DHCP, internet, WAN IP) as a
  state timeline, showing the worst state in each bucket
- satellites' backhaul signal, the weakest in each bucket
- which Deco each Wi-Fi client is on
- the log without `nrd`, newest first

## Testing

- A fake Deco checks the log reader: a first run, repeats straddling two
  reads, restarting, nothing new, and a gap longer than the buffer.
- A run on qfwfq against the real Deco with a temporary `DATA_DIR`, with a
  copy of the day's traffic CSV to check it gains the new column.
- After deploying: all four views, and the panels' queries through
  Grafana's API.

We haven't seen what the log or the internet status say during an outage
yet. Check them after the next one.
