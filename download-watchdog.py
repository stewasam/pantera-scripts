#!/usr/bin/env python3
"""Download watchdog: spots downloads that are stuck or failed to import and fixes
the cases that follow a known pattern. Anything it does not recognise is reported.

Known patterns
  1. Lidarr import refused, but every track of a monitored album is present
     (extra bonus files, or a discography pack with albums that are not wanted)
       -> import the matching tracks, leave the rest.
  2. Lidarr import refused because the rip is one long file per disc
       -> remove the download, block that release, search again.
  3. Any download (Radarr, Sonarr, Lidarr) with no progress for STALL_HOURS
       -> remove it, block that release, search again.
  4. A Radarr or Sonarr download that holds a program, an archive, or no video at all
     (malware dressed as an episode; qBittorrent skips such files, so only the shell is left)
       -> remove it at once, block that release, search again.
  5. A Radarr or Sonarr download whose files are all already in the library ("already imported")
       -> clear it from the queue; the torrent keeps seeding.
  Any other blocked or failed import is reported (blocked ones used to go unreported).

Modes: report-only by default (says what it WOULD do); pass --apply to act.
Each finding is sent to Telegram once (tracked in watchdog-state.json).
"""
import json
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

STACK = Path(__file__).resolve().parent.parent
STATE_FILE = Path(__file__).resolve().parent / "watchdog-state.json"
APPLY = "--apply" in sys.argv
STALL_HOURS = 6
AUDIO = (".flac", ".mp3", ".m4a", ".ape", ".wv", ".ogg", ".opus", ".wav")
APPS = {
    "radarr": ("http://localhost:7878/api/v3/", "movie"),
    "sonarr": ("http://localhost:8989/api/v3/", "series"),
    "lidarr": ("http://localhost:8686/api/v1/", "album"),
}


def api(app, method, path, body=None):
    base = APPS[app][0]
    key = re.search(r"<ApiKey>([^<]+)", (STACK / f"config/{app}/config.xml").read_text()).group(1)
    req = urllib.request.Request(base + path, method=method,
                                 headers={"X-Api-Key": key, "Content-Type": "application/json"},
                                 data=json.dumps(body).encode() if body is not None else None)
    with urllib.request.urlopen(req, timeout=300) as resp:
        text = resp.read()
        return json.loads(text) if text else {}


def telegram(text):
    env = dict(line.split("=", 1) for line in (STACK / "config/notify.env").read_text().splitlines() if "=" in line)
    data = urllib.parse.urlencode({"chat_id": env["TELEGRAM_CHAT_ID"], "text": text[:3900]}).encode()
    url = f"https://api.telegram.org/bot{env['TELEGRAM_BOT_TOKEN']}/sendMessage"
    urllib.request.urlopen(urllib.request.Request(url, data=data), timeout=30)


def escalate(ident, text):
    """Drop a task file where the Wall-E agent (agent/agent.py) picks it up."""
    folder = Path(__file__).resolve().parent / "escalations"
    folder.mkdir(exist_ok=True)
    name = re.sub(r"[^A-Za-z0-9]+", "-", ident)[:80]
    (folder / f"{name}.json").write_text(json.dumps({"text": text + " Please investigate and fix it if you can."}))


UNSAFE = ("executable file", "archive file", "No files found are eligible")


def unsafe(record):
    """The reason a finished download looks like malware or holds nothing usable, or None."""
    if record.get("trackedDownloadState") not in ("importBlocked", "importFailed", "importPending"):
        return None
    said = "; ".join(m for s in record.get("statusMessages", []) for m in s.get("messages", []))
    return said if any(sign in said for sign in UNSAFE) else None


def already_imported(record):
    """True when every reason the import is held up is that the file is already in the library."""
    if record.get("trackedDownloadState") not in ("importBlocked", "importPending"):
        return False
    said = [m for s in record.get("statusMessages", []) for m in s.get("messages", [])]
    return bool(said) and all(m.startswith("Episode file already imported") or "was not found in the grabbed release" in m
                              for m in said)


def remove_and_research(app, record):
    """Drop a download, block that release, and let the app look for another."""
    api(app, "DELETE", f"queue/{record['id']}?removeFromClient=true&blocklist=true&skipRedownload=false")


# --- Lidarr failed imports ---------------------------------------------------

def lidarr_failed_import(record):
    """Return (description, action or None) for a Lidarr download that failed to import."""
    title = record["title"][:70]
    folder = record.get("outputPath")
    artist_id = record.get("artistId")
    if not folder or not artist_id:
        return f"Lidarr: '{title}' failed to import and has no usable path - needs a look", None
    query = urllib.parse.urlencode({"folder": folder, "artistId": artist_id,
                                    "filterExistingFiles": "true", "replaceExistingFiles": "false"})
    files = [f for f in api("lidarr", "GET", "manualimport?" + query) if f["path"].lower().endswith(AUDIO)]
    matched = [f for f in files if f.get("tracks") and f.get("album")]

    # Pattern 2: a handful of huge files and nothing matched = one file per disc.
    if files and not matched and len(files) <= 4:
        def fix():
            remove_and_research("lidarr", record)
        return f"Lidarr: '{title}' is one long file per disc, unusable - remove, block, search again", fix

    # Pattern 1: group matched files by album; keep monitored albums that are complete.
    by_album = {}
    for f in matched:
        by_album.setdefault(f["album"]["id"], []).append(f)
    complete = {}
    for album_id, group in by_album.items():
        album = api("lidarr", "GET", f"album/{album_id}")
        if not album["monitored"] or (album.get("statistics") or {}).get("trackFileCount", 0) > 0:
            continue
        # Compare against the edition the files matched, or the selected one if that
        # edition is not listed; keep one file per track when the pack has duplicates.
        release = (next((r for r in album["releases"] if r["id"] == group[0]["albumReleaseId"]), None)
                   or next((r for r in album["releases"] if r["monitored"]), None))
        one_per_track = {}
        for f in group:
            one_per_track.setdefault(tuple(sorted(t["id"] for t in f["tracks"])), f)
        track_ids = {tid for ids in one_per_track for tid in ids}
        if release and len(track_ids) >= release["trackCount"]:
            complete[album["title"]] = list(one_per_track.values())
    if complete:
        def fix():
            payload = [{"path": f["path"], "artistId": artist_id, "albumId": f["album"]["id"],
                        "albumReleaseId": f["albumReleaseId"], "trackIds": [t["id"] for t in f["tracks"]],
                        "quality": f["quality"], "indexerFlags": f.get("indexerFlags", 0),
                        "downloadId": f.get("downloadId") or "", "disableReleaseSwitching": False}
                       for group in complete.values() for f in group]
            api("lidarr", "POST", "command", {"name": "ManualImport", "files": payload,
                                              "importMode": "copy", "replaceExistingFiles": False})
            api("lidarr", "DELETE", f"queue/{record['id']}?removeFromClient=false&blocklist=false&skipRedownload=true")
        skipped = len(files) - sum(len(g) for g in complete.values())
        names = ", ".join(complete)
        return (f"Lidarr: '{title}' was refused, but {names} is complete inside it - "
                f"import those tracks, skip {skipped} other file(s)"), fix
    return f"Lidarr: '{title}' failed to import and matches no known pattern - needs a look", None


# --- stalled downloads -------------------------------------------------------

def stalled(app, record, state, now):
    """Track progress between runs; return (description, action) once stuck long enough."""
    key = f"progress:{app}:{record.get('downloadId') or record['id']}"
    left = record.get("sizeleft", 0)
    seen = state.get(key)
    if record.get("status") in ("completed",) or left == 0:
        state.pop(key, None)
        return None
    if not seen or seen["left"] != left:
        state[key] = {"left": left, "since": now}
        return None
    hours = (now - seen["since"]) / 3600
    if hours < STALL_HOURS:
        return None

    def fix():
        remove_and_research(app, record)
        state.pop(key, None)
    return (f"{app.capitalize()}: '{record['title'][:70]}' has made no progress for {hours:.0f} h - "
            f"remove, block, search again"), fix


def main():
    now = time.time()
    state = json.loads(STATE_FILE.read_text()) if STATE_FILE.exists() else {}
    notified = state.setdefault("notified", {})
    findings = []
    for app in APPS:
        try:
            records = api(app, "GET", "queue?pageSize=500")["records"]
        except (urllib.error.URLError, OSError) as err:
            findings.append((f"{app}:unreachable", f"{app.capitalize()} did not answer: {err}", None))
            continue
        for record in records:
            ident = f"{app}:{record.get('downloadId') or record['id']}"
            if app == "lidarr" and record.get("trackedDownloadState") == "importFailed":
                findings.append((ident + ":import", *lidarr_failed_import(record)))
            elif app != "lidarr" and unsafe(record):
                why = unsafe(record)[:100]
                findings.append((ident + ":unsafe",
                                 f"{app.capitalize()}: '{record['title'][:70]}' holds no proper video ({why}) - "
                                 f"remove, block, search again",
                                 lambda app=app, record=record: remove_and_research(app, record)))
            elif app != "lidarr" and already_imported(record):
                findings.append((ident + ":done",
                                 f"{app.capitalize()}: '{record['title'][:70]}' is already in the library - clear it from the queue",
                                 lambda app=app, record=record: api(app, "DELETE", f"queue/{record['id']}?removeFromClient=false&blocklist=false&skipRedownload=true")))
            elif record.get("trackedDownloadState") in ("importFailed", "importBlocked"):
                said = "; ".join(m for s in record.get("statusMessages", []) for m in s.get("messages", []))[:120]
                findings.append((ident + ":import",
                                 f"{app.capitalize()}: '{record['title'][:70]}' cannot be imported ({said}) - needs a look", None))
            else:
                result = stalled(app, record, state, now)
                if result:
                    findings.append((ident + ":stalled", *result))

    lines = []
    handled = set()
    for ident, text, fix in findings:
        # A pack of several episodes is one download listed once per episode: deal with it once.
        if ident in handled:
            continue
        handled.add(ident)
        if fix and APPLY:
            try:
                fix()
                lines.append("FIXED - " + text)
                notified.pop(ident, None)
            except Exception as err:  # report rather than crash the whole run
                lines.append(f"FIX FAILED ({err}) - " + text)
        elif now - notified.get(ident, 0) > 86400:  # remind at most once a day
            lines.append(("WOULD FIX - " if fix else "NEEDS A LOOK - ") + text)
            notified[ident] = now
            if not fix and not ident.endswith(":unreachable"):
                escalate(ident, text)  # hand the unrecognised case to the Wall-E agent
    for ident in [i for i, t in notified.items() if now - t > 7 * 86400]:
        del notified[ident]
    STATE_FILE.write_text(json.dumps(state, indent=1))

    if lines:
        header = "Download watchdog" + ("" if APPLY else " (report only, nothing changed)")
        message = header + "\n" + "\n".join("- " + line for line in lines)
        print(message)
        if "--no-telegram" not in sys.argv:
            telegram(message)
    else:
        print(f"ok: {len(findings)} finding(s), nothing new to report")
    return 0


if __name__ == "__main__":
    sys.exit(main())
