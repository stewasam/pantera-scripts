#!/usr/bin/env python3
"""Names JoJo's Bizarre Adventure's seasons in Jellyfin after the story parts (TheTVDB's Season 2 is Part 3,
Stardust Crusaders), and locks the names. Run again when a new part has been added:
    python3 scripts/jojo-season-names.py   (from ~/docker/media-stack)
"""
import json, urllib.request
env = dict(l.split("=", 1) for l in open("config/automation.env").read().splitlines() if "=" in l and not l.startswith("#"))
H = {"X-Emby-Token": env["JELLYFIN_API_KEY"].strip(), "Content-Type": "application/json"}
def call(method, p, body=None):
    req = urllib.request.Request("http://localhost:8096" + p, method=method, headers=H, data=json.dumps(body).encode() if body is not None else None)
    t = urllib.request.urlopen(req, timeout=30).read()
    return json.loads(t) if t else {}
SERIES = "18b92a851694e98db82c2dfb87957c71"
uid = call("GET", "/Users")[0]["Id"]
NAMES = {1: "Part 1 & 2: Phantom Blood & Battle Tendency", 2: "Part 3: Stardust Crusaders", 3: "Part 4: Diamond Is Unbreakable",
         4: "Part 5: Golden Wind", 5: "Part 6: Stone Ocean", 6: "Part 7: Steel Ball Run"}
for season in call("GET", f"/Shows/{SERIES}/Seasons?userId={uid}")["Items"]:
    n = season.get("IndexNumber")
    if n not in NAMES:
        continue
    item = call("GET", f"/Users/{uid}/Items/{season['Id']}")
    item["Name"] = NAMES[n]
    item["LockedFields"] = sorted(set(item.get("LockedFields") or []) | {"Name"})
    call("POST", f"/Items/{season['Id']}", item)
for season in call("GET", f"/Shows/{SERIES}/Seasons?userId={uid}")["Items"]:
    print(season.get("IndexNumber"), season["Name"])
