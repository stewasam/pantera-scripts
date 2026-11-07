#!/usr/bin/env python3
"""Weekly Telegram digest of albums Lidarr has wanted for a while but cannot find.

Lists monitored albums with no files whose artist was added more than
MIN_DAYS ago and that are already released. Sends nothing when the list is empty.
Run with --dry-run to print the message instead of sending it.
"""
import json
import re
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

STACK = Path(__file__).resolve().parent.parent
LIDARR = "http://localhost:8686/api/v1/"
MIN_DAYS = 3
MAX_LINES = 40


def lidarr(path):
    key = re.search(r"<ApiKey>([^<]+)", (STACK / "config/lidarr/config.xml").read_text()).group(1)
    req = urllib.request.Request(LIDARR + path, headers={"X-Api-Key": key})
    with urllib.request.urlopen(req, timeout=120) as resp:
        return json.load(resp)


def parse_date(text):
    return datetime.fromisoformat(text.replace("Z", "+00:00")) if text else None


def stuck_albums():
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=MIN_DAYS)
    queued = {r.get("albumId") for r in lidarr("queue?pageSize=500")["records"]}
    rows = []
    for artist in lidarr("artist"):
        added = parse_date(artist.get("added"))
        if not artist["monitored"] or (added and added > cutoff):
            continue
        for album in lidarr(f"album?artistId={artist['id']}"):
            released = parse_date(album.get("releaseDate"))
            has_files = (album.get("statistics") or {}).get("trackFileCount", 0) > 0
            if album["monitored"] and not has_files and album["id"] not in queued and released and released < now:
                rows.append((artist["artistName"], album["title"], released.year))
    return sorted(rows)


def send(text):
    env = dict(line.split("=", 1) for line in (STACK / "config/notify.env").read_text().splitlines() if "=" in line)
    data = urllib.parse.urlencode({"chat_id": env["TELEGRAM_CHAT_ID"], "text": text}).encode()
    url = f"https://api.telegram.org/bot{env['TELEGRAM_BOT_TOKEN']}/sendMessage"
    with urllib.request.urlopen(urllib.request.Request(url, data=data), timeout=30) as resp:
        return json.load(resp)["ok"]


def main():
    rows = stuck_albums()
    if not rows:
        print("no stuck albums")
        return 0
    lines = [f"- {artist} - {title} ({year})" for artist, title, year in rows[:MAX_LINES]]
    if len(rows) > MAX_LINES:
        lines.append(f"...and {len(rows) - MAX_LINES} more")
    text = (f"Lidarr - {len(rows)} album(s) still missing after {MIN_DAYS}+ days\n"
            + "\n".join(lines) + "\nNo release was found on any source.")
    if "--dry-run" in sys.argv:
        print(text)
        return 0
    print("sent" if send(text) else "send failed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
