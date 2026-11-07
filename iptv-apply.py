#!/usr/bin/env python3
"""After iptv-clean.py: Threadfin re-reads the list, new channels are switched on, Jellyfin reloads its guide.

Run by iptv-refresh.sh. Threadfin is stopped twice for a few seconds (its mapping file can only be edited while it is
off). Channels keep their numbers; new ones get the next free numbers; every channel gets Threadfin's 1-hour dummy guide.
"""
import json
import re
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
CONF = os.path.join(HERE, "..", "config", "threadfin")
XEPG = os.path.join(CONF, "xepg.json")
OUT = os.path.join(CONF, "data", "threadfin.m3u")   # rewritten when Threadfin is ready
SETTINGS = os.path.join(CONF, "settings.json")
GUIDE = os.path.join(CONF, "playlists", "guide.xml")  # iptv-guide.py's real guide
GUIDE_ID = "GUIDEPANTERAIPTV0001"                      # its id as a Threadfin XMLTV source (Threadfin's ids are 20 chars)
ATTR = re.compile(r'<channel id="([^"]*)"')
FIRST = 100


def run(*cmd):
    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL)


def wait_ready(since, seconds=600):
    """Until Threadfin has written its channel list again after [since] (epoch s): its last step when starting."""
    end = time.time() + seconds
    while time.time() < end:
        try:
            if os.path.getmtime(OUT) > since:
                return
        except OSError:
            pass
        time.sleep(3)
    sys.exit("Threadfin didn't finish starting in 10 minutes")


def restart():
    since = time.time()
    run("docker", "start", "threadfin")
    wait_ready(since)


def guide_ids() -> set:
    """The channel keys that have a real guide (iptv-guide.py writes one <channel id=...> line each)."""
    try:
        with open(GUIDE, encoding="utf-8") as f:
            return {m.group(1).replace("&amp;", "&").replace("&quot;", '"') for line in f if line.startswith("<channel") for m in [ATTR.search(line)] if m}
    except OSError:
        return set()


def add_guide_source(settings: dict) -> bool:
    """Registers guide.xml as Threadfin's XMLTV source (once). True if it changed the settings."""
    x = settings.setdefault("files", {}).setdefault("xmltv", {})
    if GUIDE_ID in x:
        return False
    x[GUIDE_ID] = {"compatibility": {}, "counter.download": 0, "counter.error": 0, "description": "iptv-guide.py: Pluto/Samsung/Roku by id, epg.pw and Plex by name",
                   "file.source": "/home/threadfin/conf/playlists/guide.xml", "file.threadfin": GUIDE_ID + ".xml", "http_proxy.ip": "",
                   "http_proxy.port": "", "id.provider": GUIDE_ID, "last.update": "", "name": "Real guide", "provider.availability": 100, "type": "xmltv"}
    return True


def map_guides(xepg: dict, have: set) -> int:
    """Each active channel with a real guide is mapped to it; the rest keep the 1-hour dummy. Returns how many have a guide."""
    n = 0
    for e in xepg.values():
        if not e.get("x-active"):
            continue
        k = e.get("tvg-id") or "name:" + (e.get("name") or "")
        if k in have:
            e.update({"x-xmltv-file": GUIDE_ID + ".xml", "x-mapping": k}); n += 1
        else:
            e.update({"x-xmltv-file": "Threadfin Dummy", "x-mapping": "60_Minutes"})
    return n


def activate(xepg: dict) -> int:
    """Switches on every inactive channel with the dummy guide; numbers stay, new ones get the next free. Returns how many."""
    used = {e.get("x-channelID") for e in xepg.values() if e.get("x-active")}
    nxt = max([FIRST - 1] + [int(n) for n in used if str(n).isdigit()]) + 1
    added = 0
    for e in xepg.values():
        if e.get("x-active"):
            continue
        e.update({"x-active": True, "x-xmltv-file": "Threadfin Dummy", "x-mapping": "60_Minutes", "x-channelID": str(nxt)})
        nxt += 1
        added += 1
    return added


def jellyfin_guide():
    """Jellyfin's "Refresh Guide" task, through the music-page container (it holds the Jellyfin key)."""
    code = ("import os,json,urllib.request as u\n"
            "b=os.environ['JELLYFIN_URL'].rstrip('/');k={'X-Emby-Token':os.environ['JELLYFIN_API_KEY']}\n"
            "t=[x for x in json.load(u.urlopen(u.Request(b+'/ScheduledTasks',headers=k))) if x.get('Key')=='RefreshGuide'][0]\n"
            "u.urlopen(u.Request(b+'/ScheduledTasks/Running/'+t['Id'],method='POST',headers=k))")
    subprocess.run(["docker", "exec", "music-page", "python", "-c", code], check=False)


def main():
    run("docker", "stop", "threadfin")
    settings = json.load(open(SETTINGS))
    if add_guide_source(settings):
        json.dump(settings, open(SETTINGS, "w"), indent=2)
    restart()                                 # re-reads us-clean.m3u and guide.xml; new channels come in switched off
    run("docker", "stop", "threadfin")
    xepg = json.load(open(XEPG))
    added = activate(xepg)
    guided = map_guides(xepg, guide_ids())
    tmp = XEPG + ".tmp"
    json.dump(xepg, open(tmp, "w"))
    os.replace(tmp, XEPG)
    restart()
    jellyfin_guide()
    print(f"{len(xepg)} channels in Threadfin, {added} newly switched on, {guided} with a real guide", flush=True)


def selftest():
    x = {"a": {"x-active": True, "x-channelID": "100"}, "b": {"x-active": False, "x-channelID": "1000"},
         "c": {"x-active": True, "x-channelID": "105"}, "d": {"x-active": False}}
    assert activate(x) == 2
    assert [x[k]["x-channelID"] for k in "abcd"] == ["100", "106", "105", "107"]
    assert x["b"]["x-mapping"] == "60_Minutes" and x["d"]["x-active"]
    assert activate({"e": {"x-active": False}}) == 1
    y = {"a": {"x-active": True, "tvg-id": "CBS.us"}, "b": {"x-active": True, "tvg-id": "", "name": "Odd"}, "c": {"x-active": True, "tvg-id": "Z.de"}}
    assert map_guides(y, {"CBS.us", "name:Odd"}) == 2
    assert y["a"]["x-mapping"] == "CBS.us" and y["b"]["x-xmltv-file"] == GUIDE_ID + ".xml" and y["c"]["x-mapping"] == "60_Minutes"
    s = {"files": {"xmltv": {}}}
    assert add_guide_source(s) and not add_guide_source(s) and s["files"]["xmltv"][GUIDE_ID]["file.threadfin"] == GUIDE_ID + ".xml"
    print("selftest ok")


if __name__ == "__main__":
    selftest() if "--selftest" in sys.argv else main()
