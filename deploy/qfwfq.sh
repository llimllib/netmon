#!/bin/zsh
# Set up qfwfq: the wired monitor, the Deco traffic collector, and Grafana
# at http://qfwfq.local:3000.
# deploy.sh copies the repo to ~/netmon and runs this there.
set -euo pipefail
cd ~/netmon
source deploy/lib.sh

# The plugin bundles its own DuckDB; keep it on the version in mise.toml
PLUGIN=motherduck-duckdb-datasource
PLUGIN_VERSION=0.4.6
PLUGIN_SHA256=3464c8ee0a60eb49bdbd778caf103112dd710a234aa26e429faf813f449a8a57
PLUGIN_DIR=/opt/homebrew/var/lib/grafana/plugins/$PLUGIN

install_plugin() {
  if grep -q "\"version\": \"$PLUGIN_VERSION\"" $PLUGIN_DIR/plugin.json 2>/dev/null; then
    return
  fi
  echo "grafana: installing $PLUGIN $PLUGIN_VERSION"
  local tmp=$(mktemp -d)
  curl -fsSL -o $tmp/plugin.zip \
    https://github.com/motherduckdb/grafana-duckdb-datasource/releases/download/v$PLUGIN_VERSION/$PLUGIN-$PLUGIN_VERSION.zip
  echo "$PLUGIN_SHA256  $tmp/plugin.zip" | shasum -a 256 -c --quiet
  # The zip has a ~80MB binary per platform; only this one is needed
  unzip -q $tmp/plugin.zip -d $tmp -x '*_linux_*' '*_windows_*' '*_darwin_amd64'
  rm -rf $PLUGIN_DIR
  mkdir -p ${PLUGIN_DIR:h}
  mv $tmp/$PLUGIN $PLUGIN_DIR
  rm -rf $tmp
}

install_tools
install_agent com.llimllib.netmon.qfwfq.plist com.llimllib.netmon
# Installs traffic.py's dependencies, and creates the files views.sql needs
# before Grafana loads it
mise exec -- uv run --quiet traffic.py --init
install_agent com.llimllib.netmon.traffic.plist com.llimllib.netmon.traffic

if ! brew list --formula grafana &>/dev/null; then
  echo "grafana: installing"
  brew install --quiet grafana
fi
install_plugin
cp grafana/grafana.ini /opt/homebrew/etc/grafana/grafana.ini
echo "grafana: restarting"
brew services restart grafana
