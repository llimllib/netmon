#!/bin/zsh
# Copy this host's data to qfwfq, where Grafana reads it. Copies there of
# files that are gone here (CSVs converted to Parquet, expired data) are
# deleted. launchd runs this every 30s on the laptop.
#
# Optional environment:
#   DATA_DIR  where the data is (default ~/netmon/data)
#   SERVER    where to copy it (default qfwfq.local)

DATA_DIR=${DATA_DIR:-$HOME/netmon/data}
SERVER=${SERVER:-qfwfq.local}
HOST=$(hostname -s)

# Away from home the server is unreachable; don't log that every 30s
ping -c 1 -t 2 -q $SERVER &>/dev/null || exit 0

# The filters keep --delete away from other hosts' files, and skip the .tmp
# files to_parquet.sh writes
rsync -a --delete --timeout=10 \
  --include="$HOST-*.csv" --include="$HOST-*.parquet" --exclude='*' \
  $DATA_DIR/ $SERVER:netmon/data/
