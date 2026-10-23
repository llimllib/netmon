#!/bin/zsh
# Ping a set of targets once a second and append one CSV row per second.
#
# usage: monitor.sh NAME=IP [NAME=IP ...]
#   e.g. monitor.sh deco=192.168.68.1 isp_gw=142.254.209.201 cf=1.1.1.1 google=8.8.8.8
#
# Writes ~/netmon/data/<host>-<YYYY-MM-DD>.csv with columns
#   ts, <name>_ms..., dns_ms
# An empty field means no reply within the timeout. Once a day, earlier
# days' CSVs are converted to Parquet by to_parquet.sh.
#
# Optional environment:
#   HOME_GW   skip rows while the default gateway is some other address
#             (i.e. the laptop is on another network)
#   WIFISTAT  path to the wifistat binary; adds rssi,noise,channel,band,
#             width,tx_rate columns
#   KEEP_DAYS delete data older than this many days (default 30)

zmodload zsh/datetime

DNS_SERVER=${DNS_SERVER:-192.168.68.1}
DATA_DIR=${DATA_DIR:-$HOME/netmon/data}
KEEP_DAYS=${KEEP_DAYS:-30}
HOST=$(hostname -s)

names=() ips=()
for arg in "$@"; do
  names+=(${arg%%=*})
  ips+=(${arg#*=})
done
if (( ${#ips} == 0 )); then
  echo "usage: $0 NAME=IP [NAME=IP ...]" >&2
  exit 1
fi

mkdir -p $DATA_DIR
cols=(ts ${^names}_ms dns_ms)
[[ -n $WIFISTAT ]] && cols+=(rssi noise channel band width tx_rate)
header=${(j:,:)cols}

pace() {
  # Sleep until the next whole second, so every second gets exactly one row
  local remaining=$(( ${start%.*} + 1 - EPOCHREALTIME ))
  (( remaining > 0 )) && sleep $remaining
}

while true; do
  start=$EPOCHREALTIME

  # No default route counts as an outage, but a different gateway means
  # we're on someone else's network
  if [[ -n $HOME_GW ]]; then
    gw=$(route -n get default 2>/dev/null | awk '/gateway:/ {print $2}')
    if [[ -n $gw && $gw != $HOME_GW ]]; then
      pace
      continue
    fi
  fi

  strftime -s ts '%Y-%m-%dT%H:%M:%S%z' ${start%.*}
  strftime -s day '%Y-%m-%d' ${start%.*}
  file=$DATA_DIR/$HOST-$day.csv
  if [[ ! -s $file ]]; then
    print -r -- $header > $file
    # Once per new file (i.e. daily), drop old data and convert yesterday's
    rm -f $DATA_DIR/*.(csv|parquet)(N.m+$KEEP_DAYS)
    DATA_DIR=$DATA_DIR ${0:A:h}/to_parquet.sh &!
  fi

  # Run DNS check in the background while fping runs
  dns_out=$(mktemp -t netmon)
  dig @$DNS_SERVER +tries=1 +time=1 "$RANDOM$RANDOM.netmon.example.com" > $dns_out 2>&1 &
  dig_pid=$!
  if [[ -n $WIFISTAT ]]; then
    wifi_out=$(mktemp -t netmon)
    $WIFISTAT > $wifi_out 2>/dev/null &
    wifi_pid=$!
  fi

  # fping -C1 -q prints "ip : <ms>" or "ip : -" for each target on stderr
  typeset -A rtt=()
  fping -C1 -t 900 -q $ips 2>&1 | while read -r ip _ ms; do
    [[ $ms == - ]] && ms=
    rtt[$ip]=$ms
  done

  wait $dig_pid
  # Any answer (including NXDOMAIN) counts. SERVFAIL or a timeout does not.
  dns_ms=
  if grep -qE 'status: (NOERROR|NXDOMAIN)' $dns_out; then
    dns_ms=$(sed -nE 's/^;; Query time: ([0-9]+) msec/\1/p' $dns_out)
  fi
  rm -f $dns_out

  row=$ts
  for ip in $ips; do row+=",${rtt[$ip]}"; done
  row+=",$dns_ms"

  if [[ -n $WIFISTAT ]]; then
    wait $wifi_pid
    wifi=$(<$wifi_out)
    rm -f $wifi_out
    row+=",${wifi:-,,,,,}"
  fi

  print -r -- $row >> $file
  pace
done
