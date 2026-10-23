#!/bin/zsh
# Copy the dashboards as saved in Grafana's UI into grafana/dashboards/, to
# commit and deploy. Run on the laptop, which has the admin password.
set -euo pipefail
source ${0:A:h}/lib.sh

for f in $DASHBOARDS/*.json; do
  uid=$(jq -r .uid $f)
  grafana_dashboard $uid > $f.tmp
  mv $f.tmp $f
  echo "pulled $uid into ${f#$PWD/}"
done
git -C ${0:A:h:h} status --short $DASHBOARDS
