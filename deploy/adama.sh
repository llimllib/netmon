#!/bin/zsh
# Set up adama (the laptop): the Wi-Fi monitor, pushing its data to qfwfq,
# and the local dashboard. deploy.sh runs this from the checkout, which is
# also where everything runs from.
set -euo pipefail
cd ${0:A:h:h}
source deploy/lib.sh

install_tools
# views.sql reads data/traffic/, which only qfwfq fills; without these
# files, it fails to load
mise exec -- uv run --quiet traffic.py --init
install_agent com.llimllib.netmon.adama.plist com.llimllib.netmon
install_agent com.llimllib.netmon.push.plist com.llimllib.netmon.push
install_agent com.llimllib.netmon.dashboard.plist com.llimllib.netmon.dashboard
