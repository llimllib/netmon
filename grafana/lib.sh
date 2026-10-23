# Talking to Grafana on qfwfq from the laptop. Used by pull.sh and deploy.sh.

GRAFANA=http://qfwfq.local:3000
DASHBOARDS=${0:A:h:h}/grafana/dashboards
# deploy.sh puts the admin password here the first time it deploys Grafana
KEYCHAIN_SERVICE=netmon-grafana

grafana_api() {
  curl -fsS -u admin:$(security find-generic-password -s $KEYCHAIN_SERVICE -w) \
    $GRAFANA$1
}

# A dashboard as Grafana has it, minus the fields Grafana keeps to itself
grafana_dashboard() {
  grafana_api /api/dashboards/uid/$1 | jq '.dashboard | del(.id, .version)'
}
