# Shared by deploy/<host>.sh

export PATH=/opt/homebrew/bin:$PATH

# Install a LaunchAgent plist as <label>.plist and (re)start it
install_agent() {
  local src=$1 label=$2
  local dst=~/Library/LaunchAgents/$label.plist
  echo "agent: $label"
  cp $src $dst
  launchctl bootout gui/$UID/$label 2>/dev/null || true
  # bootout returns before the job is gone, and bootstrap fails until it is
  for _ in {1..10}; do
    launchctl bootstrap gui/$UID $dst 2>/dev/null && return
    sleep 1
  done
  launchctl bootstrap gui/$UID $dst
}

# Install the DuckDB version pinned in mise.toml
install_tools() {
  echo "mise: installing tools"
  mise trust -q mise.toml
  mise install -q
}
