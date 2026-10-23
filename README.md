# netmon

Find out where the brief internet outages come from.

Every second, each monitor pings each hop on the path and does a DNS lookup,
then appends one CSV row to `~/netmon/data/<host>-<date>.csv`. An empty field
means no reply. Once a day, `to_parquet.sh` converts earlier days' CSVs to
`<host>-<date>.parquet` (about 1/6 the size) and deletes them. Data older
than 30 days is deleted (set `KEEP_DAYS` to change that). Comparing the wired Mac mini with a Wi-Fi client shows which
segment failed:

```
modem (Technicolor CVA4001, Spectrum) → main Deco ─┬─ TP-Link switch ─ Mac mini, NAS
                                                   └─ Hallway Deco ⇢ Office Deco ⇢ laptop
```

| Mac mini fails?             | Laptop fails? | Likely cause                  |
|-----------------------------|---------------|-------------------------------|
| yes, at `isp_gw` or beyond  | yes           | modem / Spectrum              |
| yes, at `deco`              | yes           | main Deco                     |
| yes, at `deco`              | no            | TP-Link switch or its cables  |
| no                          | yes           | mesh backhaul / Wi-Fi         |

## Monitors

Each machine needs `fping` and [mise](https://mise.jdx.dev), which supplies
the DuckDB version pinned in `mise.toml`. Run `mise trust && mise install` in
`~/netmon` first. `to_parquet.sh` expects mise at `/opt/homebrew/bin/mise`;
set `MISE` in the plist if it's elsewhere.

Each machine has a LaunchAgent at
`~/Library/LaunchAgents/com.llimllib.netmon.plist`; the copies here are
`com.llimllib.netmon.<host>.plist`. To install one:

```
cp com.llimllib.netmon.<host>.plist ~/Library/LaunchAgents/com.llimllib.netmon.plist
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.llimllib.netmon.plist
```

- **qfwfq** (Mac mini, wired to the main Deco): run under
  `caffeinate -i -s` so the Mac doesn't sleep (`-i` alone isn't enough: once
  the Mac has slept, it cycles through brief dark wakes that `-i` doesn't
  prevent). If the Mac sleeps anyway, the log shows a gap followed by several
  seconds where every target fails, and the laptop sees nothing. Check
  `pmset -g log | grep Sleep` before trusting a Mac-mini-only outage. Stderr goes to `~/netmon/monitor.log`.

  ```
  launchctl kickstart -k gui/$(id -u)/com.llimllib.netmon   # restart
  launchctl bootout gui/$(id -u)/com.llimllib.netmon        # stop
  ```

- **adama** (laptop, Wi-Fi via the Office Deco): runs without `caffeinate`,
  so sleep leaves gaps in the
  log. It also pings the Office (192.168.71.249) and Hallway (192.168.71.250)
  nodes. It adds Wi-Fi columns from `wifistat` (built with
  `swiftc -O wifistat.swift -o wifistat`). `HOME_GW` makes it skip rows
  while the laptop is on another network.

  Ping times show the mesh path. Normally Office < Hallway < main. If
  `hallway_ms` becomes larger than `deco_ms`, Office is probably connecting
  straight to the main Deco.

To remove a monitor for good, `bootout` it and delete the plist from
`~/Library/LaunchAgents`.

`isp_gw` (142.254.209.201) is the second traceroute hop. If Spectrum changes
it, `isp_gw` will fail constantly. Update the plists if that happens.

## Dashboard

```
~/netmon/dashboard.py     # then open http://localhost:8766
```

It shows latency, packet loss and the laptop's Wi-Fi stats for each monitor,
plus a list of outages. Drag across a chart to zoom in, and click an outage to
jump to it. While it runs, it rsyncs the Mac mini's data every 30s. Charting
uses uPlot, stored in `static/` so the page works offline.

## Finding outages

On the laptop (`--delete` removes the Mac mini's CSVs once it has converted
them, so those days aren't counted twice; the filters keep it from touching
the laptop's own files):

```
rsync -a --delete --include='qfwfq-*.csv' --include='qfwfq-*.parquet' --exclude='*' \
  qfwfq.local:netmon/data/ ~/netmon/data/
cd ~/netmon && duckdb -c ".read outages.sql"
```

`outages.sql` reads both the Parquet files and today's CSVs, and needs at
least one of each.

To see the laptop's Wi-Fi around an outage:

```sql
SELECT ts, office_ms, hallway_ms, deco_ms, rssi, channel, band, tx_rate
FROM read_parquet('~/netmon/data/adama-*.parquet')
WHERE ts BETWEEN '2026-10-12 19:00:00' AND '2026-10-12 19:05:00';
```
