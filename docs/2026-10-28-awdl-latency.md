# Laptop latency spikes from a stuck AWDL

On 2026-10-28 at 20:10:56, the laptop's latency changed from a steady
baseline to spikes of 50-300ms every ~10s, on every target at once,
including the Office Deco it's connected to. qfwfq saw nothing (its
`deco_ms` stayed under 5ms), and the Deco's data (backhaul, WAN, log)
was normal. It lasted almost an hour, until `killall rapportd`.

## What happened

AWDL (Apple Wireless Direct Link) is the peer-to-peer Wi-Fi behind AirDrop,
Universal Control, Sidecar, AirPlay and Continuity Camera. While it's on,
the Wi-Fi radio leaves the network's channel on a schedule to talk to
nearby Apple devices. Pings in flight while it's away wait for it to come
back.

From the system log (`log show`):

- 20:10:54: Universal Control found a nearby Mac over Bluetooth (RSSI
  about -50) and marked it "Device Available". `rapportd` started
  advertising `_companion-link` over AWDL, and `wifip2pd` enabled AWDL:

  ```
  Updated AWDL channel sequence to [149 149 149 0 0 0 0 0 6 149 149 0 0 0 0 0]
  AWDL schedule changed: {} -> {6, 149++}
  enabled AWDL after 0 retries
  ```

  The laptop's network is on 6 GHz channel 37, so this has the radio
  visiting channels 6 and 149 instead.

- 20:56: the device left ("Device Lost"). Closing that Mac's lid didn't
  help. AWDL stayed on, because `rapportd` never withdrew its
  advertisement:

  ```
  AWDLDiscoveryTimeout: Periodic state - timer=2760s, effectiveIdle=2161s, TX=2161s, RX=2161s,
    browses=[], advertises=[_companion-link], clients={_companion-link[rapportd, unknown]}
  AWDLDiscoveryTimeout: Service leaked - Skipping AWDL disable (not supported on this platform)
    - Reason: Advertise: _companion-link ...
  ```

  AWDL had sent and received nothing for 36 minutes, and kept hopping
  anyway.

- `killall rapportd` (launchd restarts it) dropped the advertisement, and
  latency went back to normal right away.

## Recognizing it

- Spikes every ~10s, on every target at once, the first hop (`office`)
  included. Network trouble would hit the targets beyond the failing hop,
  not the first hop with the same rhythm.
- qfwfq is unaffected.
- `ifconfig awdl0` doesn't help: it says `status: active` when AWDL is idle
  too. The log does:

  ```
  log show --last 10m --predicate 'process == "wifip2pd" AND eventMessage CONTAINS "AWDL"'
  ```

## Fixes

- Now: `killall rapportd`.
- Fewer triggers: turn off Universal Control (System Settings → Displays →
  Advanced) and Handoff. Universal Control started it this time. Other
  parts of macOS can still turn AWDL on.
- For good: run a root daemon that runs `ifconfig awdl0 down` whenever it
  comes back up, as gaming tools do. That breaks AirDrop, Universal
  Control, Sidecar, AirPlay to the Mac and Continuity Camera. Not done yet;
  waiting to see whether it happens again.
