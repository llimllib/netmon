#!/bin/zsh
# Deploy netmon from this checkout, on the laptop. Safe to run repeatedly;
# it restarts everything it installs.
#
#   ./deploy.sh            # qfwfq and this laptop
#   ./deploy.sh qfwfq      # just one (qfwfq or adama)
#
# qfwfq gets the committed tree, so commit first. See deploy/<host>.sh for
# what each machine runs.
set -euo pipefail
cd ${0:A:h}

GRAFANA=http://qfwfq.local:3000
KEYCHAIN_SERVICE=netmon-grafana

deploy_qfwfq() {
  if [[ -n $(git status --porcelain) ]]; then
    echo "uncommitted changes; commit them before deploying to qfwfq" >&2
    exit 1
  fi
  echo "== qfwfq: deploying $(git rev-parse --short HEAD)"
  git archive HEAD | ssh qfwfq.local 'mkdir -p ~/netmon && tar -x -C ~/netmon'
  ssh qfwfq.local "echo $(git rev-parse HEAD) > ~/netmon/DEPLOYED && zsh ~/netmon/deploy/qfwfq.sh"
  set_grafana_password
}

# Grafana starts with admin/admin. The first deploy replaces that with a
# random password kept in this Mac's keychain:
#   security find-generic-password -s netmon-grafana -w
set_grafana_password() {
  security find-generic-password -s $KEYCHAIN_SERVICE &>/dev/null && return
  echo "grafana: waiting for it to start"
  for _ in {1..30}; do
    curl -fs $GRAFANA/api/health &>/dev/null && break
    sleep 2
  done
  local pw=$(openssl rand -hex 16)
  # Saved before changing it, so it can't be lost
  security add-generic-password -s $KEYCHAIN_SERVICE -a admin -w $pw
  if ! curl -fsS -u admin:admin -X PUT -H 'Content-Type: application/json' \
      -d "{\"oldPassword\": \"admin\", \"newPassword\": \"$pw\", \"confirmNew\": \"$pw\"}" \
      $GRAFANA/api/user/password >/dev/null; then
    security delete-generic-password -s $KEYCHAIN_SERVICE >/dev/null
    echo "couldn't change Grafana's admin password from admin/admin" >&2
    exit 1
  fi
  echo "grafana: set admin password (security find-generic-password -s $KEYCHAIN_SERVICE -w)"
}

deploy_adama() {
  echo "== adama"
  zsh deploy/adama.sh
}

(( $# )) || set -- qfwfq adama
for host in $@; do
  case $host in
    qfwfq) deploy_qfwfq ;;
    adama) deploy_adama ;;
    *) echo "unknown host: $host" >&2; exit 1 ;;
  esac
done
