#!/bin/sh
# One step: rebuild the channel list from iptv-settings.txt, build the real guide, load both into Threadfin,
# switch new channels on, map guides, and have Jellyfin reload. About 8-10 minutes; Threadfin restarts (streams blip).
set -e
cd "$(dirname "$0")"
python3 iptv-clean.py
python3 iptv-guide.py || echo "guide not rebuilt: keeping the last one"
python3 iptv-apply.py
