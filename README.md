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

## Deploying

Everything is installed by `deploy.sh`, run from this checkout on the
laptop. It's safe to rerun, and restarts what it installs:

```
./deploy.sh            # both machines
./deploy.sh qfwfq      # or just one (qfwfq, adama)
```

- **qfwfq** gets the committed tree (`git archive HEAD`, so commit first) in
  `~/netmon`, with the commit in `~/netmon/DEPLOYED`. `deploy/qfwfq.sh`
  installs its monitor, the [traffic](#traffic) collector and Grafana.
- **adama** runs from this checkout. `deploy/adama.sh` installs its monitor,
  `push.sh` (copies its data to qfwfq every 30s, for Grafana) and the local
  dashboard.

Each machine needs `fping` and [mise](https://mise.jdx.dev) at
`/opt/homebrew/bin/mise`; mise supplies the DuckDB version pinned in
`mise.toml`. Services are LaunchAgents, so they only run while you're logged
in. To manage one:

```
launchctl kickstart -k gui/$(id -u)/<label>   # restart
launchctl bootout gui/$(id -u)/<label>        # stop until the next deploy
```

| Label                           | Machine | What                    | Log                       |
|---------------------------------|---------|-------------------------|---------------------------|
| `com.llimllib.netmon`           | both    | the monitor             | `~/netmon/monitor.log`    |
| `com.llimllib.netmon.push`      | adama   | `push.sh`               | `~/netmon/push.log`       |
| `com.llimllib.netmon.dashboard` | adama   | `dashboard.py`          | `~/netmon/dashboard.log`  |
| `com.llimllib.netmon.traffic`   | qfwfq   | `traffic.py`            | `~/netmon/traffic.log`    |
| `sh.brew.grafana`               | qfwfq   | Grafana (`brew services`) | `/opt/homebrew/var/log/grafana/grafana.log` |

## Monitors

- **qfwfq** (Mac mini, wired to the main Deco): run under
  `caffeinate -i -s` so the Mac doesn't sleep (`-i` alone isn't enough: once
  the Mac has slept, it cycles through brief dark wakes that `-i` doesn't
  prevent). If the Mac sleeps anyway, the log shows a gap followed by several
  seconds where every target fails, and the laptop sees nothing. Check
  `pmset -g log | grep Sleep` before trusting a Mac-mini-only outage.

- **adama** (laptop, Wi-Fi via the Office Deco): runs without `caffeinate`,
  so sleep leaves gaps in the
  log. `AC_ONLY` makes it skip rows while on battery, so it only records while
  plugged in. It also pings the Office (192.168.71.250) and Hallway (192.168.71.249)
  nodes. The two look identical; the Deco's device list says Office
  (`EC-75-0C-C9-D6-65`) connects through Hallway (`EC-75-0C-C9-D6-6F`).
  Until 2026-10-23 the monitor had their addresses swapped; the recorded
  data was relabeled to match. It adds Wi-Fi columns from `wifistat` (built with
  `swiftc -O wifistat.swift -o wifistat`). `HOME_GW` makes it skip rows
  while the laptop is on another network.

  Ping times show the mesh path. The node the laptop is connected to
  answers fastest, so in the office it's normally Office < Hallway < main. If
  `hallway_ms` becomes larger than `deco_ms`, Office is probably connecting
  straight to the main Deco.

`isp_gw` (142.254.209.201) is the second traceroute hop. If Spectrum changes
it, `isp_gw` will fail constantly. Update the plists if that happens.

## Grafana

http://qfwfq.local:3000 has the netmon dashboard: outages, then latency and
loss for each monitor, the laptop's Wi-Fi, and [traffic](#traffic). Outages are also drawn on the
charts, and clicking an outage's start time zooms in on it. Viewing doesn't
need a login. The admin password is in the laptop's keychain:

```
security find-generic-password -s netmon-grafana -w
```

It's all set up from `grafana/` by `deploy/qfwfq.sh`:

- `grafana.ini` replaces Homebrew's config.
- The data source is the [DuckDB plugin][plugin], pinned in
  `deploy/qfwfq.sh`. Its DuckDB has to match `mise.toml`'s. It loads
  `views.sql`, so panels can query `pings`, `results`, `report` and so on.
- `dashboards/netmon.json` is the dashboard. To change it, sign in as admin,
  edit and save in the UI, then run `grafana/pull.sh` on the laptop and
  commit. `deploy.sh` refuses to deploy over saved edits that haven't been
  pulled. The plugin quotes variables itself: write `host = $host`, not
  `'$host'`. Hosts' display names are in `host_names` in `views.sql`.

[plugin]: https://github.com/motherduckdb/grafana-duckdb-datasource

## Traffic

`traffic.py` on qfwfq records how much each device on the network is
downloading and uploading. Every 15s it asks the main Deco for its client
list, which includes each client's current rates, and appends one row per
online client to `~/netmon/data/traffic/<date>.csv`. Earlier days become
Parquet, and data older than 30 days is deleted, as with the monitors'
data.

The Deco doesn't do SNMP, so this uses the undocumented, encrypted API
behind its web UI. How that works, and how it was worked out, is in
[the design](docs/plans/2026-10-23-router-traffic-design.md). If a
firmware update changes the API, `traffic.log` shows errors and the
traffic panels go empty. Nothing else depends on it.

It logs in with your TP-Link ID password, kept in qfwfq's keychain. To set
or change it, run this on qfwfq, then restart the collector:

```
security add-generic-password -U -s netmon-deco -a admin -w
```

Over SSH, run `security unlock-keychain` first. If the Deco rejects the
password, the collector waits an hour before trying again, because
repeated failures lock logins.

The dashboard's Traffic row shows total throughput, download per client,
and estimated data used per client. They are estimates, made by multiplying
each 15s sample's rate by 15s. Short bursts get smoothed out, and the
Deco's rates are about 5% higher than what applications see, because it
counts protocol overhead. The Decos' own traffic isn't included.

`views.sql` has the data as `traffic`, with rates in kbit/s, a `client`
display name, and `down_bytes`/`up_bytes` estimates. For example, the past
day's downloads:

```sql
SELECT client, sum(down_bytes) / 1e9 AS gb_down
FROM traffic
WHERE ts > now() - INTERVAL 1 DAY
GROUP BY client ORDER BY gb_down DESC;
```

Several devices share names like "iPhone", so devices without a name in
`client_names` (in `views.sql`) are shown with the end of their MAC. Add
them there to name them.

## Dashboard

The laptop also runs the older dashboard at http://localhost:8766. Restart it
after changing `dashboard.py`; changes to `static/` and `views.sql` are
picked up on the next page load or refresh.

It shows latency, packet loss and the laptop's Wi-Fi stats for each monitor,
plus a list of outages. Drag across a chart to zoom in, and click an outage to
jump to it. While it runs, it rsyncs the Mac mini's data every 30s. Charting
uses uPlot, stored in `static/` so the page works offline.

## Finding outages

On either machine (each has both machines' data):

```
cd ~/netmon && duckdb -c ".read outages.sql"
```

`views.sql` defines `pings` (every row, with a `host` column), `results`
(one row per host, second and target) and `report` (outages), for your own
queries: `duckdb -c ".read views.sql" -c "SELECT ..."`. It reads both the
Parquet files and today's CSVs, and needs at least one of each.

To see the laptop's Wi-Fi around an outage:

```sql
SELECT ts, office_ms, hallway_ms, deco_ms, rssi, channel, band, tx_rate
FROM pings
WHERE host = 'adama' AND ts BETWEEN '2026-10-12 19:00:00' AND '2026-10-12 19:05:00';
```
