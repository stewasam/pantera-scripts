#!/usr/bin/env python3
"""Media guard: keep programs and scripts off the media drive.

qBittorrent already refuses files *named* like programs (*.exe, *.scr, ...). This job
catches the rest: programs disguised as videos or music (judged by the file's first
bytes, not its name) and risky files that came in another way (Soulseek, by hand).

Each run looks at files that are new or changed since the last run (the first run, or
--full, looks at everything). For each bad file it:
  - moves every copy (hard link) of it into /mnt/storage/quarantine,
  - removes the download it came with: through Sonarr/Radarr with the release
    blocked (so a clean copy is searched for), or straight from qBittorrent,
  - reports it on Telegram.
Quarantined files are deleted for good after QUARANTINE_DAYS.
Archives and disc images are only reported (they can be legitimate).

  media-guard.py             scan new files and act
  media-guard.py --full      scan everything
  media-guard.py --dry-run   report only, change nothing, send nothing
"""
import json, os, re, shutil, subprocess, sys, time, urllib.parse, urllib.request
from pathlib import Path

STACK = Path("/home/sam/docker/media-stack")
STATE_FILE = Path(__file__).resolve().parent / "media-guard-state.json"
ROOT = "/mnt/storage/media"
QUARANTINE = "/mnt/storage/quarantine"     # same drive: moving is instant
QUARANTINE_DAYS = 14
DRY, FULL = "--dry-run" in sys.argv, "--full" in sys.argv

SHORTCUT_EXT = {".url", ".lnk", ".scf", ".website"}   # ad links release groups add: junk, not a threat here
PROGRAM_EXT = {".exe", ".scr", ".bat", ".cmd", ".com", ".pif", ".msi", ".msix", ".lnk", ".url", ".vbs", ".vbe", ".js",
               ".jse", ".wsf", ".wsh", ".ps1", ".psm1", ".jar", ".apk", ".dll", ".sys", ".hta", ".cpl", ".scf", ".reg",
               ".sh", ".dmg", ".pkg"}
REPORT_EXT = {".zip", ".rar", ".7z", ".cab", ".arj", ".ace", ".iso", ".img"}
MAGIC = {b"MZ": "Windows program", b"\x7fELF": "Linux program", b"\xca\xfe\xba\xbe": "Mac/Java program",
         b"\xcf\xfa\xed\xfe": "Mac program", b"#!": "script", b"PK\x03\x04": "zip archive", b"Rar!": "rar archive",
         b"7z\xbc\xaf": "7z archive"}
PROGRAMS = {"Windows program", "Linux program", "Mac/Java program", "Mac program", "script"}
MEDIA_EXT = {".mkv", ".mp4", ".avi", ".m4v", ".m2ts", ".ts", ".mka", ".vob", ".flac", ".mp3", ".m4a", ".ogg",
             ".opus", ".wav", ".ape", ".wv", ".srt", ".ass", ".ssa", ".sup", ".jpg", ".jpeg", ".png", ".webp", ".gif"}


def env(path):
    return dict(l.strip().split("=", 1) for l in open(path) if "=" in l and not l.startswith("#"))


def telegram(text):
    n = env(STACK / "config/notify.env")
    data = urllib.parse.urlencode({"chat_id": n["TELEGRAM_CHAT_ID"].strip("\"'"), "text": text[:3900]}).encode()
    tok = n["TELEGRAM_BOT_TOKEN"].strip("\"'")
    urllib.request.urlopen(f"https://api.telegram.org/bot{tok}/sendMessage", data=data, timeout=30)


def arr(app, port, path, method="GET"):
    key = re.search(r"<ApiKey>([^<]+)", (STACK / f"config/{app}/config.xml").read_text()).group(1)
    req = urllib.request.Request(f"http://localhost:{port}/api/v3{path}", method=method, headers={"X-Api-Key": key})
    with urllib.request.urlopen(req, timeout=120) as r:
        t = r.read()
        return json.loads(t) if t else None


def qbit(path, post=None):
    cmd = ["docker", "exec", "gluetun", "wget", "-qO-", "-T", "20"] + (["--post-data", post] if post else [])
    out = subprocess.run(cmd + [f"http://127.0.0.1:8080/api/v2/{path}"], capture_output=True, text=True).stdout
    return json.loads(out) if out.strip().startswith(("[", "{")) else None


def verdict(path):
    """('remove' | 'report', what it is) for a bad file, else None."""
    name = os.path.basename(path).lower()
    ext = os.path.splitext(name)[1]
    try:
        with open(path, "rb") as f:
            head = f.read(8)
    except OSError:
        return None
    kind = next((k for m, k in MAGIC.items() if head.startswith(m)), None)
    if ext in PROGRAM_EXT or kind in PROGRAMS and ext not in {".txt", ".nfo", ".log"}:
        return "remove", kind or f"{ext} file"
    if kind and ext in MEDIA_EXT:
        return "remove", f"{kind} disguised as {ext}"
    if ext in REPORT_EXT or (kind and kind != "script"):   # notes starting "#!" are just text
        return "report", kind or f"{ext} file"
    return None


def all_links(path):
    """Every path on the drive that is this same file (library hard links included)."""
    st = os.stat(path)
    if st.st_nlink == 1:
        return [path]
    out = subprocess.run(["find", ROOT, "-xdev", "-inum", str(st.st_ino)], capture_output=True, text=True).stdout
    return [p for p in out.splitlines() if p] or [path]


def owning_torrent(rel, torrents):
    """The torrent whose files include this path: the closest match. Torrents with no
    path yet (magnets still fetching their file list) match nothing."""
    hits = [t for t in torrents if t.get("content_path") and t["content_path"].rstrip("/") != t.get("save_path", "").rstrip("/")
            and (rel == t["content_path"] or rel.startswith(t["content_path"].rstrip("/") + "/"))]
    return max(hits, key=lambda t: len(t["content_path"]), default=None)


def remove_download(path, notes):
    """Remove the torrent this file came in, through the app that grabbed it when there is one."""
    rel = "/media" + path[len("/mnt/storage/media"):]
    torrents = qbit("torrents/info") or []
    t = owning_torrent(rel, torrents)
    if not t:
        return
    for app, port in (("sonarr", 8989), ("radarr", 7878)):
        try:
            for r in arr(app, port, "/queue?pageSize=1000")["records"]:
                if (r.get("downloadId") or "").lower() == t["hash"].lower():
                    if not DRY:
                        arr(app, port, f"/queue/{r['id']}?removeFromClient=true&blocklist=true&skipRedownload=false", "DELETE")
                    notes.append(f"removed '{t['name'][:60]}' via {app}, release blocked, searching for a clean copy")
                    return
        except Exception:
            pass
    if not DRY:
        qbit("torrents/delete", f"hashes={t['hash']}&deleteFiles=true")
    notes.append(f"removed torrent '{t['name'][:60]}' from qBittorrent")


def main():
    state = json.loads(STATE_FILE.read_text()) if STATE_FILE.exists() else {}
    since = 0 if FULL or "last_run" not in state else state["last_run"] - 3600
    started = time.time()
    removed, reported, notes = [], [], []
    seen_reports = set(state.get("reported", []))
    checked = 0
    for d, dirs, files in os.walk(ROOT):
        dirs[:] = [x for x in dirs if not x.endswith(".trickplay") and x != "incomplete"]
        for f in files:
            p = os.path.join(d, f)
            try:
                st = os.stat(p)
            except OSError:
                continue
            if max(st.st_mtime, st.st_ctime) < since:
                continue
            checked += 1
            v = verdict(p)
            if not v:
                continue
            action, what = v
            if action == "report":
                if p not in seen_reports:
                    reported.append(f"{what}: {p[len(ROOT) + 1:][:100]}")
                    seen_reports.add(p)
                continue
            if not what.endswith(tuple(SHORTCUT_EXT) + tuple(e + " file" for e in SHORTCUT_EXT)):
                remove_download(p, notes)
            for link in all_links(p):
                if os.path.exists(link) and not DRY:
                    dest = os.path.join(QUARANTINE, time.strftime("%Y-%m-%d"), link[len(ROOT) + 1:])
                    os.makedirs(os.path.dirname(dest), exist_ok=True)
                    shutil.move(link, dest)
            removed.append(f"{what}: {p[len(ROOT) + 1:][:100]}")
    # empty the quarantine of anything older than QUARANTINE_DAYS
    if os.path.isdir(QUARANTINE) and not DRY:
        for day in os.listdir(QUARANTINE):
            full = os.path.join(QUARANTINE, day)
            if os.path.getmtime(full) < time.time() - QUARANTINE_DAYS * 86400:
                shutil.rmtree(full, ignore_errors=True)
    if not DRY:
        state.update({"last_run": started, "reported": sorted(seen_reports)})
        STATE_FILE.write_text(json.dumps(state, indent=1))
    lines = []
    if removed:
        lines.append(f"Removed {len(removed)} unsafe file(s) (in quarantine for {QUARANTINE_DAYS} days):")
        lines += ["- " + x for x in removed[:20]] + ["- " + n for n in notes]
    if reported:
        lines.append("Worth a look (archives/disc images, left in place):")
        lines += ["- " + x for x in reported[:20]]
    text = "Media guard\n" + "\n".join(lines) if lines else f"ok: {checked} new file(s) checked, nothing unsafe"
    print(text)
    if lines and not DRY:
        telegram(text)


if __name__ == "__main__":
    main()
