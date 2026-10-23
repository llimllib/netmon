#!/bin/zsh
# Convert this host's finished daily CSVs to Parquet, then delete the CSVs.
# Today's CSV is still being written, so it's left alone. monitor.sh runs
# this once a day; it's also safe to run by hand.
#
# Every column but ts is stored as DOUBLE, so files agree on types even when
# a target never replied all day.
#
# Optional environment:
#   DATA_DIR  where the CSVs are (default ~/netmon/data)
#   MISE      path to mise, which supplies duckdb (default
#             /opt/homebrew/bin/mise; launchd's PATH doesn't include it)

DATA_DIR=${DATA_DIR:-$HOME/netmon/data}
MISE=${MISE:-/opt/homebrew/bin/mise}
ROOT=${0:A:h}
HOST=$(hostname -s)
today=$(date +%Y-%m-%d)

for csv in $DATA_DIR/$HOST-<->-<->-<->.csv(N); do
  [[ $csv == *-$today.csv ]] && continue
  pq=${csv%.csv}.parquet
  tmp=$pq.tmp
  if $MISE -C $ROOT exec -- duckdb -c "
      COPY (SELECT ts::TIMESTAMPTZ AS ts, COLUMNS(* EXCLUDE (ts))::DOUBLE
            FROM read_csv('$csv', all_varchar = true))
      TO '$tmp' (FORMAT parquet, COMPRESSION zstd);
      SELECT CASE WHEN (SELECT count(*) FROM read_parquet('$tmp'))
                     = (SELECT count(*) FROM read_csv('$csv', all_varchar = true))
             THEN 'ok' ELSE error('row count mismatch') END;
    " > /dev/null; then
    # Keep the CSV's mtime so KEEP_DAYS expiry still counts from the data's day
    touch -r $csv $tmp && mv $tmp $pq && rm $csv
  else
    rm -f $tmp
    echo "to_parquet: failed to convert $csv" >&2
  fi
done
