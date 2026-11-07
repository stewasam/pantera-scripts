#!/usr/bin/env python3
"""Downloads to get first: titles listed in scripts/priority.txt (one per line).

Every 15 minutes (cron) the Radarr and Sonarr queues are checked; a download whose release name matches a listed
title is moved to the top of qBittorrent's queue and force-started (queue limits don't hold it back), and Telegram
says so once. Remove the line from priority.txt when it's done.

  download-priority.py            act
  download-priority.py --dry-run  say what would happen
"""
import json, re, subprocess, sys, urllib.parse, urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
STACK = HERE.parent
LIST, STATE = HERE / "priority.txt", HERE / "priority-state.json"
APPS = {"radarr": 7878, "sonarr": 8989}
DRY = "--dry-run" in sys.argv


def titles(path):
    try:
        return [l.strip() for l in open(path) if l.strip() and not l.strip().startswith("#")]
    except OSError:
        return []


def words(s):
    return re.findall(r"[a-z0-9]+", s.lower())


def matches(title, release):
    """Every word of the title appears, in order and side by side, in the release name."""
    t, r = words(title), words(release)
    return any(r[i:i + len(t)] == t for i in range(len(r) - len(t) + 1)) if t else False


def plan(wanted, queue, torrents, done):
    out = []
    for rec in queue:
        h = (rec.get("downloadId") or "").lower()
        if h and h in torrents and h not in done and any(matches(w, rec.get("title", "")) for w in wanted):
            out.append((h, rec["title"]))
    return out


def arr(app, path):
    key = re.search(r"<ApiKey>([^<]+)", (STACK / f"config/{app}/config.xml").read_text()).group(1)
    req = urllib.request.Request(f"http://localhost:{APPS[app]}/api/v3/{path}", headers={"X-Api-Key": key})
    return json.load(urllib.request.urlopen(req, timeout=60))


def qbit(path, post=None):
    cmd = ["docker", "exec", "gluetun", "wget", "-qO-", "-T", "20"] + (["--post-data", post] if post else [])
    out = subprocess.run(cmd + [f"http://127.0.0.1:8080/api/v2/{path}"], capture_output=True, text=True).stdout
    return json.loads(out) if out.strip().startswith(("[", "{")) else None


def telegram(text):
    n = dict(l.strip().split("=", 1) for l in open(STACK / "config/notify.env") if "=" in l and not l.startswith("#"))
    data = urllib.parse.urlencode({"chat_id": n["TELEGRAM_CHAT_ID"].strip("\"'"), "text": text}).encode()
    urllib.request.urlopen(f"https://api.telegram.org/bot{n['TELEGRAM_BOT_TOKEN'].strip(chr(34) + chr(39))}/sendMessage", data=data, timeout=30)


def main():
    wanted = titles(LIST)
    if not wanted:
        return
    state = json.loads(STATE.read_text()) if STATE.exists() else {"done": []}
    queue = [r for app in APPS for r in arr(app, "queue?pageSize=500")["records"]]
    torrents = {t["hash"].lower(): t for t in (qbit("torrents/info") or [])}
    todo = plan(wanted, queue, torrents, set(state["done"]))
    for h, name in todo:
        print(("would put first: " if DRY else "putting first: ") + name)
        if not DRY:
            qbit("torrents/topPrio", f"hashes={h}")
            qbit("torrents/setForceStart", f"hashes={h}&value=true")
            state["done"].append(h)
            telegram(f"Downloads: \"{name[:80]}\" is a priority, so it jumped the queue and is downloading now.")
    if todo and not DRY:
        STATE.write_text(json.dumps(state))


if __name__ == "__main__":
    main()
