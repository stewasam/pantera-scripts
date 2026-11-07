#!/usr/bin/env python3
"""One Telegram message each morning saying whether the server is healthy.

Checks disk space, both drives' health, containers, the VPN, backup age and the
download queues. The first line is either "All good" or what needs attention.

  daily-health.py            send the message
  daily-health.py --print    print it instead of sending
"""
import datetime as dt
import json, os, re, shutil, subprocess, sys, urllib.parse, urllib.request

STACK = "/home/sam/docker/media-stack"
HOME_STACK = "/home/sam/docker/home-stack"
DRIVE_STATE = f"{STACK}/scripts/drive-state.json"
problems, lines = [], []


def env(path):
    return dict(l.strip().split("=", 1) for l in open(path) if "=" in l and not l.startswith("#"))


def sh(cmd, timeout=180):
    try:
        return subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout).stdout.strip()
    except subprocess.TimeoutExpired:
        return ""


def arr(app, port, path):
    key = re.search(r"<ApiKey>([^<]+)", open(f"{STACK}/config/{app}/config.xml").read()).group(1)
    r = urllib.request.Request(f"http://localhost:{port}/api/v3{path}", headers={"X-Api-Key": key})
    return json.loads(urllib.request.urlopen(r, timeout=60).read())


def check(name, fn):
    try:
        fn()
    except Exception as e:  # one broken check must not hide the others
        problems.append(f"{name} check failed ({type(e).__name__})")


def disks():
    if not os.path.ismount("/media"):
        problems.append("the 12TB drive is NOT mounted at /media")
    parts = []
    for label, path, limit in (("System disk", "/", 85), ("Media drive", "/media", 80)):
        u = shutil.disk_usage(path)
        pct = round(u.used / u.total * 100)
        parts.append(f"{label} {pct}% used, {u.free / 1e12:.1f} TB free" if u.free > 1e12
                     else f"{label} {pct}% used, {u.free / 1e9:.0f} GB free")
        if pct >= limit:
            problems.append(f"{label.lower()} is {pct}% full")
    lines.append("Space: " + "; ".join(parts))


def drives():
    parts = []
    for name, f in (("12TB", "/run/smart-status/sda.json"), ("NVMe", "/run/smart-status/nvme0.json")):
        if not os.path.exists(f) or os.path.getmtime(f) < dt.datetime.now().timestamp() - 7200:
            problems.append(f"no recent health reading for the {name} drive")
            continue
        d = json.load(open(f))
        ok = d.get("smart_status", {}).get("passed")
        temp = d.get("temperature", {}).get("current")
        bad = 0
        for a in d.get("ata_smart_attributes", {}).get("table", []):
            if a["id"] in (5, 197, 198):
                bad += a["raw"]["value"]
        bad += d.get("nvme_smart_health_information_log", {}).get("media_errors", 0)
        unc = next((a["raw"]["value"] for a in d.get("ata_smart_attributes", {}).get("table", []) if a["id"] == 187), None)
        if unc is not None:
            seen = json.load(open(DRIVE_STATE)) if os.path.exists(DRIVE_STATE) else {}
            if unc > seen.get(name, unc):
                problems.append(f"{name} drive had {unc - seen[name]} new unreadable-sector error(s) (total {unc})")
            seen[name] = unc
            if "--print" not in sys.argv:
                json.dump(seen, open(DRIVE_STATE, "w"))
        parts.append(f"{name} healthy, {temp}C" if ok and not bad else f"{name} PROBLEM")
        if not ok or bad:
            problems.append(f"{name} drive reports trouble (health passed={ok}, bad sectors/errors={bad})")
        elif temp and temp >= 50:
            problems.append(f"{name} drive is hot ({temp}C)")
    # read errors the drive itself may not count (a loose cable shows up here first)
    io = sh("journalctl -k --since -24h --no-pager | grep -c 'I/O error, dev sda'").strip()
    if io.isdigit() and int(io):
        problems.append(f"12TB drive had {io} read error(s) in the kernel log in the last 24 h (check its cable; run a long SMART test)")
    lines.append("Drives: " + "; ".join(parts))


def containers():
    rows = [json.loads(l) for l in sh("docker ps -a --format '{{json .}}'").splitlines()]
    down = [r["Names"] for r in rows if r["State"] != "running"]
    sick = [r["Names"] for r in rows if "unhealthy" in r["Status"]]
    lines.append(f"Apps: {len(rows) - len(down)} of {len(rows)} running")
    if down or sick:
        problems.append("apps not healthy: " + ", ".join(sorted(set(down + sick))))


def vpn():
    inside = sh("docker exec qbittorrent curl -s -m 10 https://api.ipify.org", 30)
    outside = sh("curl -s -m 10 https://api.ipify.org", 30)
    if not inside:
        problems.append("download apps have no internet (VPN tunnel down?)")
        lines.append("VPN: no connection")
    elif inside == outside:
        problems.append("download apps are NOT behind the VPN")
        lines.append("VPN: EXPOSED")
    else:
        lines.append("VPN: on")


def backups():
    pw = open(f"{HOME_STACK}/backrest-repo-password.txt").read().strip()
    parts = []
    for name, uri in (("local", "/repos/local"), ("Google Drive", "rclone:gdrive:server-backups")):
        out = sh(f"docker exec -e RESTIC_PASSWORD='{pw}' backrest restic -r {uri} snapshots --json --no-lock", 240)
        snaps = json.loads(out) if out.startswith("[") else []
        if not snaps:
            parts.append(f"{name}: none yet")
            problems.append(f"no completed backup in the {name} repository")
            continue
        # restic writes nanoseconds and a UTC offset; drop the fraction, keep the offset
        newest = max(dt.datetime.fromisoformat(re.sub(r"\.\d+", "", s["time"])) for s in snaps)
        hours = (dt.datetime.now(dt.timezone.utc) - newest).total_seconds() / 3600
        parts.append(f"{name}: {hours:.0f}h ago")
        if hours > 36:
            problems.append(f"newest {name} backup is {hours:.0f} hours old")
    lines.append("Backups: " + "; ".join(parts))


def downloads():
    parts = []
    for app, port, noun in (("radarr", 7878, "films"), ("sonarr", 8989, "episodes")):
        q = arr(app, port, "/queue?pageSize=500")
        stuck = [r for r in q["records"] if r.get("trackedDownloadStatus") == "warning" or r.get("status") == "warning"]
        parts.append(f"{q['totalRecords']} {noun} queued" + (f" ({len(stuck)} stuck)" if stuck else ""))
        if stuck:
            problems.append(f"{len(stuck)} {noun} stuck in {app}: " + ", ".join(sorted({r['title'][:40] for r in stuck})[:3]))
        for h in arr(app, port, "/health"):
            if h["type"] == "error":
                problems.append(f"{app}: {h['message'][:120]}")
    movies = arr("radarr", 7878, "/movie")
    parts.append(f"library {sum(m['hasFile'] for m in movies)} films")
    st = f"{STACK}/scripts/starter-library-state.json"
    if os.path.exists(st):
        s = json.load(open(st))
        if not s.get("finished"):
            parts.append(f"starter list {len(s['done'])}/320 added")
    lines.append("Downloads: " + "; ".join(parts))


def library():
    f = f"{STACK}/scripts/library-audit-state.json"
    if not os.path.exists(f):
        lines.append("Library audit: not run yet"); return
    st = json.load(open(f))
    hours = (dt.datetime.now().timestamp() - st.get("last_run", 0)) / 3600
    gaps = st.get("summary", {}).get("jellyfin_gaps", 0)
    lines.append(f"Library audit: {hours:.0f}h ago, " + (f"{gaps} item(s) not showing in Jellyfin" if gaps else "everything in Jellyfin"))
    if hours > 36:
        problems.append(f"library audit has not run for {hours:.0f} hours")
    if gaps:
        problems.append(f"{gaps} downloaded item(s) not showing in Jellyfin")


def system():
    n = max(0, len(sh("apt list --upgradable 2>/dev/null").splitlines()) - 1)
    bits = [f"{n} system updates waiting"] if n else []
    if os.path.exists("/var/run/reboot-required"):
        bits.append("restart needed to finish updates")
    up = float(open("/proc/uptime").read().split()[0]) / 86400
    bits.append(f"up {up:.0f} days" if up >= 1 else "restarted in the last day")
    lines.append("System: " + "; ".join(bits))


for name, fn in (("disk", disks), ("drive", drives), ("apps", containers), ("VPN", vpn),
                 ("backup", backups), ("download", downloads), ("library", library), ("system", system)):
    check(name, fn)

head = "All good on pantera." if not problems else "pantera needs attention:\n" + "\n".join("- " + p for p in problems)
text = head + "\n\n" + "\n".join(lines)
if "--print" in sys.argv:
    print(text)
else:
    n = env(f"{STACK}/config/notify.env")
    data = urllib.parse.urlencode({"chat_id": n["TELEGRAM_CHAT_ID"].strip("\"'"), "text": text[:3900]}).encode()
    tok = n["TELEGRAM_BOT_TOKEN"].strip("\"'")
    urllib.request.urlopen(f"https://api.telegram.org/bot{tok}/sendMessage", data=data, timeout=30)
