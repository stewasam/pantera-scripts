#!/usr/bin/env python3
"""Leaving Soon: movies and whole TV/anime shows nobody has played go on a "Leaving Soon" list for a week, then leave.

Spec: ~/docs/superpowers/specs/2026-10-22-leaving-soon-design.md
  - on the list: added 60+ days ago and never played by anyone; or, while the drive is over 78% full, the oldest
    unwatched ones added 30+ days ago until leaving them would bring it back to 78%. A show is one item: it goes
    whole, never a season at a time (11-08, Sam: single seasons leaving looks wrong). rolling-seasons.py keeps new shows
    at season 1 until someone is halfway through, so an unwatched show stays small without trimming.
  - kept: favorited by anyone (jellyfin-sync's "favourite" tag, or a ♥ on the movie/show/season), played by anyone
    (for a show: any episode),
    tagged keep / keep-all, still downloading, or requested in Seerr in the last 60 days
  - leaves 7 days after listing: Radarr/Sonarr delete it (no re-download), then its torrents go too so space frees
Practice mode until scripts/leaving-soon.live exists: lists, collects and messages, deletes nothing.

  leaving-soon.py            the hourly run (lists; deletes on the 04:00 run)
  leaving-soon.py --status   what's on the list
"""
import json, os, re, shutil, subprocess, sys, time, urllib.parse, urllib.request
from datetime import datetime, timezone

STACK = "/home/sam/docker/media-stack"
STATE = f"{STACK}/scripts/leaving-soon-state.json"
LIVE = f"{STACK}/scripts/leaving-soon.live"
DRIVE = "/mnt/storage"
DAY = 86400
GRACE, DISK_GRACE, WAIT, REQUESTED = 60 * DAY, 30 * DAY, 7 * DAY, 60 * DAY
DISK_LIMIT = 0.78   # under the daily report's 80% warning, so space frees before it fires (11-08; was 85%)
DISK_WHY = f"drive over {round(DISK_LIMIT * 100)}%"
DISK_WEEK_BUDGET = 1e12          # the drive rule lists at most 1 TB a week (it can't run away if deleting stopped freeing space)
MAX_DUE, MAX_DUE_BYTES, MIN_MATCHED = 30, 2e12, 0.9  # a run that looks wrong stops before deleting anything
RECYCLE = ["/media/.recycle"]    # Radarr's and Sonarr's recycle bins: a link there doesn't count as "still used"
KEEP_TAGS = {"favourite", "keep", "keep-all"}
COLLECTION = "Leaving Soon"


def ts(iso):
    return datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()


# --- the rules (pure; see test_leaving_soon.py) ---
def plan(items, listed, total, used, now):
    """items: every movie/show with key, added, size, played, favorite, protected. listed: state of the list.
    Returns add (new on the list, with leaves/why), drop (saved or gone), due (to delete now)."""
    by = {x["key"]: x for x in items}
    ok = lambda x: not x["played"] and not x["favorite"] and not x["protected"]
    # stays listed: still unwatched and unloved, and not new again (a new episode or an upgrade counts as new)
    stays = lambda k, v: by[k]["added"] <= now - DISK_GRACE
    keep = {k: v for k, v in listed.items() if k in by and ok(by[k]) and stays(k, v)}
    sure = {k: v for k, v in keep.items() if not v["why"].startswith("drive over") or by[k]["added"] <= now - GRACE}
    if used - DISK_LIMIT * total - sum(by[k]["size"] for k in sure) <= 0:
        keep = sure  # the drive is fine again: what only the drive rule listed comes off
    drop = [dict(key=k, **v) for k, v in listed.items() if k not in keep]
    due = [by[k] for k, v in keep.items() if v["leaves"] <= now]
    add = [dict(x, leaves=now + WAIT, why="60 days unwatched") for x in items
           if x["key"] not in keep and ok(x) and x["added"] <= now - GRACE]
    leaving = sum(by[k]["size"] for k in keep) + sum(x["size"] for x in add)
    over = used - leaving - DISK_LIMIT * total
    budget = DISK_WEEK_BUDGET - sum(v.get("size", by[k]["size"]) for k, v in keep.items()
                                    if v["why"].startswith("drive over") and v["listed"] > now - WAIT)
    if over > 0:  # the drive is too full: the oldest unwatched (30+ days) go on the list too
        taken = {x["key"] for x in add}
        for x in sorted((x for x in items if x["key"] not in keep and x["key"] not in taken and ok(x)
                         and x["added"] <= now - DISK_GRACE), key=lambda x: x["added"]):
            if over <= 0:
                break
            if x["size"] > budget:
                continue
            add.append(dict(x, leaves=now + WAIT, why=DISK_WHY))
            over -= x["size"]; budget -= x["size"]
    return {"add": add, "drop": drop, "due": due}


def finish(items):
    """Protect what can't be checked: anything not matched in Jellyfin (no warning there, watch state unknown)."""
    for x in items:
        if not x["protected"] and not x.get("jf"):
            x["protected"] = "not matched in Jellyfin"
    return items


class LooksWrong(Exception):
    pass


def guard(items, due):
    if not items:
        raise LooksWrong("Radarr/Sonarr returned nothing")
    matched = sum(1 for x in items if x.get("jf")) / len(items)
    if matched < MIN_MATCHED:
        raise LooksWrong(f"only {matched:.0%} of the library matched in Jellyfin")
    if len(due) > MAX_DUE or sum(x["size"] for x in due) > MAX_DUE_BYTES:
        raise LooksWrong(f"{len(due)} items ({sum(x['size'] for x in due) / 1e12:.1f} TB) due at once")


def recycle_links(dirs):
    """How many links each file (by inode) has inside the recycle bins."""
    out = {}
    for top in dirs:
        for d, _, fs in os.walk(top):
            for f in fs:
                st = os.lstat(os.path.join(d, f))
                out[(st.st_dev, st.st_ino)] = out.get((st.st_dev, st.st_ino), 0) + 1
    return out


def still_used(files, recycle):
    """Some file of a torrent is still in the library (a link outside the torrent and the recycle bins)."""
    for f in files:
        st = os.stat(f)
        if st.st_nlink - recycle.get((st.st_dev, st.st_ino), 0) > 1:
            return True
    return False


def show_of(series, files):
    """The whole show as one item (None with no files): the newest episode decides when it was added, so a new
    season or episode restarts its clock."""
    if not files:
        return None
    return {"key": f"show:{series['id']}", "kind": "show", "series_id": series["id"], "tvdb": series.get("tvdbId"),
            "title": series["title"], "added": max(ts(f["dateAdded"]) for f in files), "size": sum(f.get("size", 0) for f in files),
            "file_ids": [f["id"] for f in files], "tags": series.get("tags", [])}


# --- pantera ---
def env(path):
    return dict(l.strip().split("=", 1) for l in open(path) if "=" in l and not l.startswith("#"))


def xml_key(app):
    return re.search(r"<ApiKey>([^<]+)", open(f"{STACK}/config/{app}/config.xml").read()).group(1)


def api(base, headers, path, method="GET", body=None):
    req = urllib.request.Request(base + path, method=method, headers={**headers, "Content-Type": "application/json"},
                                 data=json.dumps(body).encode() if body is not None else None)
    with urllib.request.urlopen(req, timeout=120) as r:
        text = r.read()
        return json.loads(text) if text else {}


class Pantera:
    def __init__(self):
        self.radarr = lambda p, **k: api("http://localhost:7878/api/v3", {"X-Api-Key": xml_key("radarr")}, p, **k)
        self.sonarr = lambda p, **k: api("http://localhost:8989/api/v3", {"X-Api-Key": xml_key("sonarr")}, p, **k)
        self.jf = lambda p, **k: api("http://localhost:8096", {"X-Emby-Token": env(f"{STACK}/config/automation.env")["JELLYFIN_API_KEY"]}, p, **k)
        seerr = json.load(open(f"{STACK}/config/jellyseerr/settings.json"))["main"]["apiKey"]
        self.seerr = lambda p, **k: api("http://localhost:5055/api/v1", {"X-Api-Key": seerr}, p, **k)

    def telegram(self, text):
        e = env(f"{STACK}/config/notify.env")
        data = urllib.parse.urlencode({"chat_id": e["TELEGRAM_CHAT_ID"], "text": text[:3900]}).encode()
        urllib.request.urlopen(urllib.request.Request(f"https://api.telegram.org/bot{e['TELEGRAM_BOT_TOKEN']}/sendMessage", data=data), timeout=30)

    def qbt(self, path, form=None):
        cmd = ["docker", "exec", "qbittorrent", "curl", "-s", "-f"]
        for k, v in (form or {}).items():
            cmd += ["--data-urlencode", f"{k}={v}"]
        out = subprocess.run(cmd + [f"http://localhost:8080/api/v2/{path}"], capture_output=True, text=True, timeout=60, check=True).stdout
        return json.loads(out) if out.strip().startswith(("[", "{")) else out

    # --- what's in the library, and who watched or loved what ---
    def watch_state(self):
        """Played and favorite, by any Jellyfin user: movies by TMDB id, seasons by (TVDB id, season), shows by TVDB id."""
        played_movies, fav_movies, played_seasons, fav_seasons, fav_shows, jf_ids = set(), set(), set(), set(), set(), {}
        series_tvdb = {}
        for u in self.jf("/Users"):
            q = urllib.parse.urlencode({"Recursive": "true", "Fields": "ProviderIds", "EnableUserData": "true",
                                        "IncludeItemTypes": "Movie,Series,Season,Episode"})
            res = self.jf(f"/Users/{u['Id']}/Items?{q}")
            items = res["Items"]
            if res.get("TotalRecordCount", len(items)) != len(items):
                raise LooksWrong(f"Jellyfin returned {len(items)} of {res['TotalRecordCount']} items")
            for it in items:
                if it["Type"] == "Series" and it.get("ProviderIds", {}).get("Tvdb"):
                    series_tvdb[it["Id"]] = int(it["ProviderIds"]["Tvdb"])
                    jf_ids[("show", int(it["ProviderIds"]["Tvdb"]))] = it["Id"]
            for it in items:
                ud = it.get("UserData") or {}
                played = ud.get("Played") or ud.get("PlayCount", 0) > 0 or ud.get("PlaybackPositionTicks", 0) > 0
                fav = bool(ud.get("IsFavorite"))
                ids = it.get("ProviderIds") or {}
                if it["Type"] == "Movie" and ids.get("Tmdb"):
                    t = int(ids["Tmdb"]); jf_ids[("movie", t)] = it["Id"]
                    if played: played_movies.add(t)
                    if fav: fav_movies.add(t)
                elif it["Type"] == "Series" and fav and ids.get("Tvdb"):
                    fav_shows.add(int(ids["Tvdb"]))
                elif it["Type"] in ("Season", "Episode") and it.get("SeriesId") in series_tvdb:
                    tv = series_tvdb[it["SeriesId"]]
                    n = it.get("IndexNumber") if it["Type"] == "Season" else it.get("ParentIndexNumber")
                    if n is None:
                        continue
                    if it["Type"] == "Season":
                        jf_ids[("season", tv, n)] = it["Id"]
                    if played: played_seasons.add((tv, n))
                    if fav: fav_seasons.add((tv, n))
        return played_movies, fav_movies, played_seasons, fav_seasons, fav_shows, jf_ids

    def items(self, now):
        played_m, fav_m, played_s, fav_s, fav_shows, jf_ids = self.watch_state()
        rtags = {t["id"]: t["label"] for t in self.radarr("/tag")}
        stags = {t["id"]: t["label"] for t in self.sonarr("/tag")}
        queued_movies = {r.get("movieId") for r in self.radarr("/queue?pageSize=1000")["records"]}
        queued_series = {r.get("seriesId") for r in self.sonarr("/queue?pageSize=1000")["records"]}
        asked = {"movie": set(), "tv": set(), "tv_tmdb": set()}
        for r in self.seerr("/request?take=1000&skip=0&sort=added")["results"]:
            if now - ts(r["createdAt"]) < REQUESTED:
                m = r.get("media") or {}
                if r.get("type") == "movie":
                    asked["movie"].add(m.get("tmdbId"))
                else:  # anime requests often have no TVDB id: TMDB too
                    asked["tv"].add(m.get("tvdbId")); asked["tv_tmdb"].add(m.get("tmdbId"))
        out = []
        for m in self.radarr("/movie"):
            if not m.get("hasFile"):
                continue
            tags = {rtags.get(t) for t in m.get("tags", [])}
            prot = ("tagged " + ", ".join(sorted(tags & KEEP_TAGS))) if tags & KEEP_TAGS else \
                "still downloading" if m["id"] in queued_movies else "requested in Seerr" if m.get("tmdbId") in asked["movie"] else None
            out.append({"key": f"movie:{m['id']}", "kind": "movie", "id": m["id"], "title": f"{m['title']} ({m.get('year')})",
                        "added": ts(m["movieFile"]["dateAdded"]), "size": m.get("sizeOnDisk", 0), "tmdb": m.get("tmdbId"),
                        "jf": jf_ids.get(("movie", m.get("tmdbId"))), "played": m.get("tmdbId") in played_m,
                        "favorite": m.get("tmdbId") in fav_m or "favourite" in tags, "protected": prot})
        for s in self.sonarr("/series"):
            tags = {stags.get(t) for t in s.get("tags", [])}
            x = show_of(s, self.sonarr(f"/episodefile?seriesId={s['id']}"))
            if not x:
                continue
            tv = s.get("tvdbId")
            prot = ("tagged " + ", ".join(sorted(tags & KEEP_TAGS))) if tags & KEEP_TAGS else \
                "still downloading" if s["id"] in queued_series else \
                "requested in Seerr" if (tv and tv in asked["tv"]) or (s.get("tmdbId") and s["tmdbId"] in asked["tv_tmdb"]) else None
            out.append(dict(x, jf=jf_ids.get(("show", tv)), played=any(t == tv for t, _ in played_s),
                            favorite=tv in fav_shows or any(t == tv for t, _ in fav_s) or "favourite" in tags, protected=prot))
        return finish(out)

    # --- the Jellyfin collection ---
    def collection(self, jf_ids):
        admin = next(u for u in self.jf("/Users") if u.get("Policy", {}).get("IsAdministrator"))
        q = urllib.parse.urlencode({"Recursive": "true", "IncludeItemTypes": "BoxSet", "SearchTerm": COLLECTION})
        found = [b for b in self.jf(f"/Users/{admin['Id']}/Items?{q}")["Items"] if b["Name"] == COLLECTION]
        want = {i for i in jf_ids if i}
        if not found:
            if want:
                self.jf("/Collections?" + urllib.parse.urlencode({"Name": COLLECTION, "IsLocked": "true", "Ids": ",".join(sorted(want))}), method="POST")
            return
        cid = found[0]["Id"]
        have = {i["Id"] for i in self.jf(f"/Users/{admin['Id']}/Items?ParentId={cid}")["Items"]}
        if want - have:
            self.jf(f"/Collections/{cid}/Items?Ids={','.join(sorted(want - have))}", method="POST")
        if have - want:
            self.jf(f"/Collections/{cid}/Items?Ids={','.join(sorted(have - want))}", method="DELETE")

    # --- leaving ---
    def torrent_hashes(self, x):
        rows = self.radarr(f"/history/movie?movieId={x['id']}") if x["kind"] == "movie" else \
            self.sonarr(f"/history/series?seriesId={x['series_id']}")
        return {r["downloadId"].lower() for r in rows if r.get("downloadId")}

    def delete(self, x):
        hashes = self.torrent_hashes(x)  # before the history goes with the movie
        if x["kind"] == "movie":
            self.radarr(f"/movie/{x['id']}?deleteFiles=true&addImportExclusion=true", method="DELETE")
        else:  # the whole show, kept off import lists so it doesn't come straight back
            self.sonarr(f"/series/{x['series_id']}?deleteFiles=true&addImportListExclusion=true", method="DELETE")
        freed = 0
        try:  # it has left the library either way; a torrent that can't be checked stays (the seeding limit removes it later)
            recycle = recycle_links(RECYCLE)
            for h in hashes:  # the torrent goes only when nothing of it is in the library any more
                info = self.qbt(f"torrents/info?hashes={h}")
                if not info:
                    continue
                p = info[0]["content_path"]
                if not os.path.exists(p):
                    raise FileNotFoundError(p)
                files = [p] if os.path.isfile(p) else [os.path.join(d, f) for d, _, fs in os.walk(p) for f in fs]
                if files and not still_used(files, recycle):
                    self.qbt("torrents/delete", {"hashes": h, "deleteFiles": "true"})
                    freed += info[0]["size"]
        except Exception as e:
            print(f"{x['title']}: torrents not removed: {e!r}", flush=True)
        return freed


def gb(b):
    return f"{b / 1e9:.0f} GB" if b >= 1e9 else f"{b / 1e6:.0f} MB"


def when(t):
    return datetime.fromtimestamp(t).strftime("%b %-d")


def run(p, now, deleting_hour):
    practice = not os.path.exists(LIVE)
    state = json.load(open(STATE)) if os.path.exists(STATE) else {"listed": {}, "reported": []}
    items = p.items(now)
    du = shutil.disk_usage(DRIVE)
    pl = plan(items, state["listed"], du.total, du.used, now)
    try:
        guard(items, pl["due"] if deleting_hour and not practice else [])
    except LooksWrong as e:  # nothing changes this run; say so once a day
        print(f"{time.strftime('%F %T')} stopped: {e}", flush=True)
        if deleting_hour:
            p.telegram(f"Leaving Soon didn't run today: {e}. Nothing was deleted.")
        return
    by = {x["key"]: x for x in items}
    for d in pl["drop"]:
        state["listed"].pop(d["key"], None)
    for x in pl["add"]:
        state["listed"][x["key"]] = {"listed": now, "leaves": x["leaves"], "why": x["why"], "title": x["title"], "size": x["size"], "jf": x.get("jf")}
    tag = "Practice (nothing is deleted yet): " if practice else ""
    if pl["add"]:
        lines = "\n".join(f"• {x['title']} ({gb(x['size'])})" for x in sorted(pl["add"], key=lambda x: x["title"]))
        p.telegram(f"{tag}Leaving Soon: {len(pl['add'])} item(s), {gb(sum(x['size'] for x in pl['add']))}, leave on "
                   f"{when(min(x['leaves'] for x in pl['add']))}. ♥ one in Jellyfin to keep it.\n{lines}")
    gone, freed = [], 0
    if pl["due"] and deleting_hour:
        if practice:
            new = [x for x in pl["due"] if x["key"] not in state["reported"]]
            if new:
                p.telegram(f"{tag}today these would have left ({gb(sum(x['size'] for x in new))}):\n" + "\n".join(f"• {x['title']}" for x in new))
                state["reported"] += [x["key"] for x in new]
        else:
            for x in pl["due"]:
                try:
                    freed += p.delete(x)
                    gone.append(x); state["listed"].pop(x["key"], None)
                except Exception as e:  # one failure must not stop the rest; it stays listed and is tried tomorrow
                    print(f"{x['title']}: delete failed: {e!r}", flush=True)
            if gone:
                p.telegram(f"Left the library ({gb(sum(x['size'] for x in gone))}; the space comes back when Radarr/Sonarr empty "
                           f"their recycle bin; {gb(freed)} of torrents removed):\n" + "\n".join(f"• {x['title']}" for x in gone))
    try:
        p.collection([by[k]["jf"] for k in state["listed"] if k in by])
    except Exception as e:
        print(f"collection update failed: {e!r}", flush=True)
    state["reported"] = [k for k in state["reported"] if k in state["listed"]]
    tmp = STATE + ".tmp"
    json.dump(state, open(tmp, "w"), indent=1); os.replace(tmp, STATE)
    print(f"{time.strftime('%F %T')} {'practice' if practice else 'live'}: {len(items)} items, drive {du.used / du.total:.1%}, "
          f"+{len(pl['add'])} listed, -{len(pl['drop'])} saved, {len(pl['due'])} due, {len(gone)} left", flush=True)


if __name__ == "__main__":
    if "--probe" in sys.argv:  # read-only: what the list would be now, and if the drive were 90% full
        now, pa = time.time(), Pantera()
        items, du = pa.items(now), shutil.disk_usage(DRIVE)
        st = json.load(open(STATE)) if os.path.exists(STATE) else {"listed": {}}
        kinds = {}
        for x in items:
            k = "played" if x["played"] else "favorite" if x["favorite"] else x["protected"] or ("old enough" if x["added"] <= now - GRACE else "too new")
            kinds[k] = kinds.get(k, 0) + 1
        print(f"{len(items)} items ({sum(x['kind'] == 'movie' for x in items)} movies, {sum(x['kind'] == 'show' for x in items)} shows); drive {du.used / du.total:.1%}")
        print("  ", dict(sorted(kinds.items(), key=lambda kv: -kv[1])))
        print(f"   in Jellyfin: {sum(1 for x in items if x['jf'])} of {len(items)} matched")
        for label, used in (("now", du.used), ("if the drive were 90% full", 0.90 * du.total)):
            pl = plan(items, st["listed"], du.total, used, now)
            print(f"{label}: {len(pl['add'])} would go on the list ({gb(sum(x['size'] for x in pl['add']))})")
            for x in pl["add"][:12]:
                print(f"     {gb(x['size']):>7}  added {when(x['added'])}  {x['why']:18} {x['title']}")
    elif "--status" in sys.argv:
        st = json.load(open(STATE)) if os.path.exists(STATE) else {"listed": {}}
        for k, v in sorted(st["listed"].items(), key=lambda kv: kv[1]["leaves"]):
            print(f"{when(v['leaves'])}  {gb(v['size']):>7}  {v['why']:18} {v['title']}")
        print(f"{len(st['listed'])} on the list; {'LIVE' if os.path.exists(LIVE) else 'practice mode'}")
    else:
        run(Pantera(), time.time(), deleting_hour=time.localtime().tm_hour == 4)
