#!/usr/bin/env python3
"""Daily library audit: is everything we downloaded actually watchable in Jellyfin?

The download watchdog only sees Sonarr/Radarr's *queue*. A download leaves the queue
once the app thinks it is done, even when only part of it was imported (a "Complete"
pack where 2 of 291 episodes matched), and downloads added by hand never enter it.
This job looks at the files themselves instead:

  1. Every video in downloads/ that is not hard-linked into the library is shown to
     Sonarr and Radarr (their manual-import scan, read-only). Episodes or films the
     library is missing and the app accepts without objection are imported
     (hard-linked, so torrents keep seeding and no space is used). Samples, extras
     and lower-quality copies are left alone. Anything no app recognises is reported.
  2. Every series and film Sonarr/Radarr have on disk is compared with what Jellyfin
     shows; gaps are reported (after one library refresh).

Telegram gets a message only when something was imported or needs a look; each
finding is reported once (state in library-audit-state.json).

  library-audit.py            run and act
  library-audit.py --dry-run  say what it would do, change nothing, send nothing
"""
import json, os, re, sys, time, urllib.parse, urllib.request
from pathlib import Path

STACK = Path("/home/sam/docker/media-stack")
STATE_FILE = Path(__file__).resolve().parent / "library-audit-state.json"
DRY = "--dry-run" in sys.argv
HOST, APP_ROOT = "/mnt/storage/media", "/media"   # same drive, as the host and the apps see it
DOWNLOADS = f"{HOST}/downloads"
VIDEO = (".mkv", ".mp4", ".avi", ".m4v", ".m2ts", ".ts")
MIN_VIDEO = 150_000_000   # smaller videos are samples, openings, trailers


def env(path):
    return dict(l.strip().split("=", 1) for l in open(path) if "=" in l and not l.startswith("#"))


def key(app):
    return re.search(r"<ApiKey>([^<]+)", (STACK / f"config/{app}/config.xml").read_text()).group(1)


APPS = {"sonarr": ("http://localhost:8989/api/v3", {"X-Api-Key": key("sonarr")}),
        "radarr": ("http://localhost:7878/api/v3", {"X-Api-Key": key("radarr")}),
        "jellyfin": ("http://localhost:8096", {"X-Emby-Token": env(STACK / "config/automation.env")["JELLYFIN_API_KEY"]})}


def api(app, path, body=None, method=None, **q):
    base, headers = APPS[app]
    url = base + path + ("?" + urllib.parse.urlencode(q) if q else "")
    req = urllib.request.Request(url, method=method, data=json.dumps(body).encode() if body is not None else None,
                                 headers={**headers, "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=900) as r:
        t = r.read()
        return json.loads(t) if t else None


def telegram(text):
    n = env(STACK / "config/notify.env")
    data = urllib.parse.urlencode({"chat_id": n["TELEGRAM_CHAT_ID"].strip("\"'"), "text": text[:3900]}).encode()
    urllib.request.urlopen(f"https://api.telegram.org/bot{n['TELEGRAM_BOT_TOKEN'].strip(chr(34) + chr(39))}/sendMessage",
                           data=data, timeout=30)


def to_app(path):
    return APP_ROOT + path[len(HOST):]


def finished_torrents():
    """Names of torrents still downloading (their files are incomplete; skip them)."""
    import subprocess
    out = subprocess.run(["docker", "exec", "gluetun", "wget", "-qO-", "-T", "20",
                          "http://127.0.0.1:8080/api/v2/torrents/info"], capture_output=True, text=True).stdout
    return {t["name"] for t in json.loads(out or "[]") if t["progress"] < 1}


def wait(app, cmd):
    for _ in range(300):
        st = api(app, f"/command/{cmd['id']}")["status"]
        if st not in ("queued", "started"):
            return st
        time.sleep(3)
    return "timeout"


# --- 1. downloads that never reached the library ------------------------------------

def is_season_dir(d):
    """A season or pack folder: its unlinked videos may be real episodes, never just extras."""
    return bool(re.search(r"(^|[^a-z])(s\d{1,2}|season[ ._]?\d+)([^a-z]|$)", os.path.basename(d), re.I)) and \
        not re.search(r"extra|special|featurette|bonus|\bsps?\b|\bnc", os.path.basename(d), re.I)


EXTRAS = re.compile(r"extra|special|featurette|bonus|behind|interview|trailer|\bsps?\b|\bnc(op|ed)?\b", re.I)
LINKED_DIRS = set()   # folders that already have a video in the library


def orphan_folders(busy):
    """Folders holding real videos with no library link, keyed by the top-level download.
    A download whose own folder already has a video in the library is a film or season
    with its extras; its other videos are bonus material and are skipped."""
    found = {}
    for top in sorted(os.listdir(DOWNLOADS)):
        if top in busy or top == "slskd":
            continue
        path = os.path.join(DOWNLOADS, top)
        walk = list(os.walk(path)) if os.path.isdir(path) else [(DOWNLOADS, [], [top])]
        linked_dirs = {d for d, _, files in walk for f in files
                       if f.lower().endswith(VIDEO) and os.stat(os.path.join(d, f)).st_nlink > 1}
        for d, _, files in walk:
            if any(d.startswith(l + os.sep) or l.startswith(d + os.sep) for l in linked_dirs) and not is_season_dir(d):
                continue   # an extras folder beside (or around) the film/season itself
            if d in linked_dirs:
                LINKED_DIRS.add(d)
            for f in files:
                p = os.path.join(d, f)
                if f.lower().endswith(VIDEO) and "sample" not in f.lower():
                    st = os.stat(p)
                    if st.st_nlink == 1 and st.st_size >= MIN_VIDEO:
                        found.setdefault(top, {}).setdefault(d, []).append(f)
    return found


def scan(app, folder, files):
    """The app's verdict on each of these files: ('import', payload) | ('known', why) | ('unknown', why)."""
    res = {os.path.basename(r["path"]): r for r in api(app, "/manualimport", folder=to_app(folder), filterExistingFiles="false")}
    out, seen = {}, set()
    for f in files:
        r = res.get(f)
        if not r:
            out[f] = ("unknown", "not listed"); continue
        why = "; ".join(x["reason"] for x in r.get("rejections", []))
        target = r.get("series") or r.get("movie")
        if not target:
            out[f] = ("unknown", why or "no match"); continue
        if app == "sonarr":
            eps = r.get("episodes") or []
            ids = tuple(sorted(e["id"] for e in eps))
            if why or not eps or any(e.get("hasFile") for e in eps) or ids in seen:
                out[f] = ("known", why or "episode already has a file"); continue
            seen.add(ids)
            payload = {"seriesId": target["id"], "episodeIds": list(ids), "releaseType": r.get("releaseType", "singleEpisode")}
        else:
            if why or target.get("hasFile"):
                out[f] = ("known", why or "film already has a file"); continue
            payload = {"movieId": target["id"]}
        payload.update({"path": r["path"], "quality": r["quality"], "languages": r.get("languages", []),
                        "releaseGroup": r.get("releaseGroup"), "indexerFlags": r.get("indexerFlags", 0),
                        "downloadId": r.get("downloadId")})
        out[f] = ("import", payload, target.get("title"))
    return out


def audit_downloads(state, report):
    busy = finished_torrents()
    orphans = orphan_folders(busy)
    known = state.setdefault("known", {})   # file path -> why it is fine to leave it
    imported = {}
    unknown = {}
    for top, folders in orphans.items():
        for folder, files in folders.items():
            files = [f for f in files if os.path.join(folder, f) not in known]
            if not files:
                continue
            verdicts = {}
            for app in ("sonarr", "radarr"):
                try:
                    v = scan(app, folder, files)
                except Exception as e:
                    report.append(f"{app} could not scan '{top[:60]}' ({type(e).__name__})"); continue
                for f, x in v.items():
                    # prefer the app that wants to import it, then one that knows it
                    rank = {"import": 2, "known": 1, "unknown": 0}
                    if f not in verdicts or rank[x[0]] > rank[verdicts[f][1][0]]:
                        verdicts[f] = (app, x)
            batches = {}
            for f, (app, x) in verdicts.items():
                if x[0] == "import":
                    batches.setdefault(app, []).append(x)
                elif x[0] == "known":
                    known[os.path.join(folder, f)] = x[1][:80]
                elif folder in LINKED_DIRS or EXTRAS.search(os.path.basename(folder)):
                    known[os.path.join(folder, f)] = "extra beside a film/episode already in the library"
                else:
                    unknown.setdefault(top, []).append(f)
            for app, items in batches.items():
                title = items[0][2]
                if DRY:
                    print(f"would import {len(items)} into {title} ({app}) from {top[:60]}")
                else:
                    for i in range(0, len(items), 50):
                        st = wait(app, api(app, "/command", {"name": "ManualImport", "importMode": "copy",
                                                            "files": [x[1] for x in items[i:i + 50]]}))
                        if st != "completed":
                            report.append(f"import into {title} {st}")
                imported[title] = imported.get(title, 0) + len(items)
    if imported:
        report.append("Imported into the library: " + ", ".join(f"{t} ({n})" for t, n in sorted(imported.items())))
    reported = state.setdefault("reported", {})
    for top, files in unknown.items():
        if top not in reported:
            report.append(f"No app recognises '{top[:70]}' ({len(files)} video(s)) - needs adding to Sonarr/Radarr or deleting")
            reported[top] = time.time()
    return bool(imported)


# --- 2. on disk per Sonarr/Radarr, but not in Jellyfin ------------------------------

def jellyfin_gaps():
    gaps = []
    user = next(u for u in api("jellyfin", "/Users") if u["Policy"]["IsAdministrator"])["Id"]
    items = f"/Users/{user}/Items"   # the unscoped /Items leaves some libraries out
    jf_movies = {m.get("ProviderIds", {}).get("Tmdb") for m in
                 api("jellyfin", items, Recursive="true", IncludeItemTypes="Movie", Fields="ProviderIds")["Items"]}
    for m in api("radarr", "/movie"):
        if m.get("hasFile") and str(m["tmdbId"]) not in jf_movies:
            gaps.append(f"film {m['title']} ({m['year']})")
    jf_series, jf_folders = {}, {}
    for s in api("jellyfin", items, Recursive="true", IncludeItemTypes="Series", Fields="ProviderIds,Path")["Items"]:
        t = s.get("ProviderIds", {}).get("Tvdb")
        if t:
            jf_series[t] = s["Id"]
        if s.get("Path"):
            jf_folders[os.path.basename(s["Path"])] = s["Id"]   # some shows are matched without a TVDB id
    for s in api("sonarr", "/series"):
        have = (s.get("statistics") or {}).get("episodeFileCount", 0)
        if not have:
            continue
        sid = jf_series.get(str(s["tvdbId"])) or jf_folders.get(os.path.basename(s["path"]))
        if not sid:
            gaps.append(f"show {s['title']} (all {have} episodes)"); continue
        shown = api("jellyfin", items, Recursive="true", ParentId=sid, IncludeItemTypes="Episode",
                    LocationTypes="FileSystem", Limit="0")["TotalRecordCount"]
        if shown < have:
            gaps.append(f"show {s['title']} ({have - shown} of {have} episodes missing)")
    return gaps


def refresh_jellyfin():
    api("jellyfin", "/Library/Refresh", method="POST")
    time.sleep(30)
    for _ in range(180):
        t = [x for x in api("jellyfin", "/ScheduledTasks") if x["Key"] == "RefreshLibrary"]
        if not t or t[0]["State"] == "Idle":
            return
        time.sleep(10)


def audit_jellyfin(state, report, changed):
    if changed and not DRY:
        refresh_jellyfin()
    gaps = jellyfin_gaps()
    if gaps and not DRY:
        refresh_jellyfin()   # a new file may simply not be scanned yet
        gaps = jellyfin_gaps()
    old = state.get("jellyfin_gaps", [])
    new = [g for g in gaps if g not in old]
    if new:
        report.append("In Sonarr/Radarr but not showing in Jellyfin: " + "; ".join(new[:15])
                      + (f" and {len(new) - 15} more" if len(new) > 15 else ""))
    state["jellyfin_gaps"] = gaps
    return gaps


def main():
    state = json.loads(STATE_FILE.read_text()) if STATE_FILE.exists() else {}
    report = []
    changed = audit_downloads(state, report)
    gaps = audit_jellyfin(state, report, changed)
    state["last_run"] = time.time()
    state["summary"] = {"jellyfin_gaps": len(gaps)}
    if not DRY:
        STATE_FILE.write_text(json.dumps(state, indent=1))
    if report:
        text = "Library audit\n" + "\n".join("- " + r for r in report)
        print(text)
        if not DRY:
            telegram(text)
    else:
        print("ok: library matches downloads, Jellyfin shows everything")


if __name__ == "__main__":
    main()
