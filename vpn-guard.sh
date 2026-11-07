#!/bin/sh
# Runs every 5 minutes (cron). qbittorrent and slskd live inside gluetun's network (the VPN):
# - VPN unhealthy for 15 minutes: restart gluetun (at most once an hour); after 3 tries that didn't help, tell Sam on
#   Telegram once and wait for it to come back. The two apps are then restarted onto the new network (below).
# - gluetun restarted or recreated, or the apps came up before it (a reboot): restart them so they get its network.
# - VPN fine but slskd logged out of Soulseek for 10 minutes: restart slskd (at most every 30 minutes).
# It never starts the apps without the tunnel.
cd /home/sam/docker/media-stack || exit 1

# touch scripts/vpn-guard.pause to keep them stopped on purpose
[ -e scripts/vpn-guard.pause ] && exit 0

S=scripts/.vpn-guard   # counters between runs
mkdir -p "$S"
get() { cat "$S/$1" 2>/dev/null || echo 0; }
put() { echo "$2" > "$S/$1"; }
now=$(date +%s)
tell() {   # a Telegram message to Sam (the same bot as daily-health)
    ( set -a; . config/notify.env; set +a
      curl -s -m 20 "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage" \
          --data-urlencode "chat_id=${TELEGRAM_CHAT_ID}" --data-urlencode "text=$1" >/dev/null )
}
notify() {   # a notice in Shred's bell for Sam and Maria: notify <needs|info> <text>; never fails the guard
    data=$(python3 -c 'import json,sys; print(json.dumps({"priority": sys.argv[1], "text": sys.argv[2], "people": ["sam", "maria"]}))' "$1" "$2") || return 0
    curl -s -m 5 -X POST localhost:8097/api/notify -H "X-Notify-Token: $(cat config/music-page/notify-token 2>/dev/null)" \
        -H 'Content-Type: application/json' --data "$data" >/dev/null 2>&1 || true
}

if [ "$(docker inspect -f '{{.State.Health.Status}}' gluetun 2>/dev/null)" != "healthy" ]; then
    down=$(( $(get down) + 1 )); put down "$down"
    [ "$down" -eq 1 ] && put since "$now"
    if [ "$down" -ge 3 ] && [ $(( now - $(get last) )) -ge 3600 ]; then
        tries=$(get tries)
        if [ "$tries" -lt 3 ]; then
            put tries $(( tries + 1 )); put last "$now"
            echo "$(date -Is) VPN unhealthy for $(( (now - $(get since)) / 60 )) min, restarting gluetun (try $(( tries + 1 )) of 3)"
            docker compose restart gluetun
        elif [ "$(get told)" = 0 ]; then
            put told 1
            echo "$(date -Is) VPN still down after 3 restarts, telling Sam"
            tell "pantera: the VPN (gluetun) has been down for $(( (now - $(get since)) / 60 )) minutes and 3 restarts didn't fix it. Soulseek downloads and torrents are stopped until it's back (they never run without it). The ProtonVPN key (config/gluetun-secrets, pantera3) may need replacing or extending on account.protonvpn.com."
            notify needs "the VPN (gluetun) has been down for $(( (now - $(get since)) / 60 )) minutes and 3 restarts didn't fix it. Soulseek downloads and torrents are stopped until it's back (they never run without it). The ProtonVPN key (config/gluetun-secrets, pantera3) may need replacing or extending on account.protonvpn.com."
        fi
    fi
    exit 0
fi

if [ "$(get tries)" != 0 ]; then
    echo "$(date -Is) VPN healthy again"
    [ "$(get told)" = 1 ] && tell "pantera: the VPN is back; downloads and torrents are running again."
    [ "$(get told)" = 1 ] && notify info "the VPN is back; downloads and torrents are running again."
fi
put down 0; put tries 0; put told 0; put last 0

gid=$(docker inspect -f '{{.Id}}' gluetun)
gstart=$(docker inspect -f '{{.State.StartedAt}}' gluetun)

for c in qbittorrent slskd; do
    running=$(docker inspect -f '{{.State.Running}}' "$c" 2>/dev/null)
    mode=$(docker inspect -f '{{.HostConfig.NetworkMode}}' "$c" 2>/dev/null)
    cstart=$(docker inspect -f '{{.State.StartedAt}}' "$c" 2>/dev/null)
    if [ "$running" != "true" ] || [ "$mode" != "container:$gid" ]; then
        echo "$(date -Is) $c not attached to current gluetun (running=$running), bringing it up"
        docker compose up -d "$c"
    elif [ "$(printf '%s\n%s\n' "$cstart" "$gstart" | sort | head -1)" = "$cstart" ] && [ "$cstart" != "$gstart" ]; then
        # started before gluetun's last start: still holding the old, dead network
        echo "$(date -Is) $c started before gluetun last did, restarting it"
        docker restart "$c"
    fi
done

# slskd logged in to Soulseek? (asked through Shred's music-page, which has its API key; no answer: not checked)
in=$(docker exec music-page python -c "import sources; print((sources._slskd('/server') or {}).get('isLoggedIn'))" 2>/dev/null)
if [ "$in" = "False" ]; then
    n=$(( $(get slsk) + 1 )); put slsk "$n"
    if [ "$n" -ge 2 ] && [ $(( now - $(get slsklast) )) -ge 1800 ]; then
        put slsk 0; put slsklast "$now"
        echo "$(date -Is) slskd logged out of Soulseek for 10 min with the VPN up, restarting slskd"
        docker restart slskd
    fi
else
    put slsk 0
fi
