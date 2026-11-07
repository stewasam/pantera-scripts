#!/usr/bin/env python3
"""Two jobs driven by what Sam and Maria do in Jellyfin.

Favourites: a film or show that either user marks as a favourite is tagged
"favourite" (keep forever) and moved to the 4K profile, then searched for an
upgrade. Anime stays on the anime profile (dual audio matters more than 4K) and
is only tagged. Removing the favourite removes the tag; the quality stays.

Watch progress: shows start with season 1 only. Once either user has finished
an episode, more is fetched:
  - 100 episodes or fewer in total: the whole series
  - longer shows: the season after the furthest one watched
  - when that next season is itself very long (over 30 episodes, typical for
    long anime): the next 25 episodes after the furthest one watched

Season 1 first: a show that has just been added (by a request, for example)
and that nobody has started is cut back to season 1. Later seasons are
unmonitored and their queued downloads are cancelled; episodes already in the
library are kept. The watch-progress rule above then fetches the rest. Shows
tagged "favourite" or "keep-all" are left alone, and so is a request that did
not include season 1. Each show is only looked at once, when it first appears.

Apart from cancelling those queued downloads, the jobs only add or upgrade.

  jellyfin-sync.py            apply
  jellyfin-sync.py --dry-run  show what would change
"""
import json, os, re, sys, urllib.parse, urllib.request

STACK = "/home/sam/docker/media-stack"
DRY = "--dry-run" in sys.argv
WHOLE_SERIES_MAX = 100
LONG_SEASON = 30
AHEAD = 25
FAV_TAG = "favourite"
KEEP_ALL_TAG = "keep-all"
STATE = f"{STACK}/scripts/jellyfin-sync-state.json"
MOVIE_4K, MOVIE_HD = "UHD Bluray + WEB", "HD Bluray + WEB"
SHOW_4K, SHOW_HD = "WEB-2160p", "WEB-1080p"


def env(path):
    return dict(l.strip().split("=", 1) for l in open(path) if "=" in l and not l.startswith("#"))


def xml_key(app):
    return re.search(r"<ApiKey>([^<]+)", open(f"{STACK}/config/{app}/config.xml").read()).group(1)


AUTO = env(f"{STACK}/config/automation.env")
APPS = {"radarr": ("http://localhost:7878/api/v3", {"X-Api-Key": xml_key("radarr")}),
        "sonarr": ("http://localhost:8989/api/v3", {"X-Api-Key": xml_key("sonarr")}),
        "jellyfin": ("http://localhost:8096", {"X-Emby-Token": AUTO["JELLYFIN_API_KEY"]})}


def api(app, path, method="GET", body=None):
    base, headers = APPS[app]
    r = urllib.request.Request(base + path, method=method,
                               data=json.dumps(body).encode() if body is not None else None,
                               headers={**headers, "Content-Type": "application/json"})
    with urllib.request.urlopen(r, timeout=120) as f:
        t = f.read()
        return json.loads(t) if t else None


def telegram(text):
    n = env(f"{STACK}/config/notify.env")
    tok = n["TELEGRAM_BOT_TOKEN"].strip("\"'")
    data = urllib.parse.urlencode({"chat_id": n["TELEGRAM_CHAT_ID"].strip("\"'"), "text": text[:3900]}).encode()
    try:
        urllib.request.urlopen(f"https://api.telegram.org/bot{tok}/sendMessage", data=data, timeout=30)
    except Exception as e:
        print("telegram failed:", e)


def tag_id(app, label):
    for t in api(app, "/tag"):
        if t["label"] == label:
            return t["id"]
    return None if DRY else api(app, "/tag", "POST", {"label": label})["id"]


def profiles(app):
    return {p["name"]: p["id"] for p in api(app, "/qualityprofile")}


def jf_items(user_id, **params):
    q = urllib.parse.urlencode({"Recursive": "true", "Fields": "ProviderIds", **params})
    return api("jellyfin", f"/Users/{user_id}/Items?{q}")["Items"]


def favourites(users):
    movies, shows = {}, {}
    for u in users:
        for it in jf_items(u["Id"], Filters="IsFavorite", IncludeItemTypes="Movie,Series"):
            ids = it.get("ProviderIds", {})
            if it["Type"] == "Movie" and ids.get("Tmdb"):
                movies.setdefault(int(ids["Tmdb"]), set()).add(u["Name"])
            elif it["Type"] == "Series" and ids.get("Tvdb"):
                shows.setdefault(int(ids["Tvdb"]), set()).add(u["Name"])
    return movies, shows


def sync_favourites(users, notes):
    fav_movies, fav_shows = favourites(users)
    for app, favs, id_field, p4k, phd, search in (
            ("radarr", fav_movies, "tmdbId", MOVIE_4K, MOVIE_HD, lambda i: {"name": "MoviesSearch", "movieIds": [i]}),
            ("sonarr", fav_shows, "tvdbId", SHOW_4K, SHOW_HD, lambda i: {"name": "SeriesSearch", "seriesId": i})):
        prof = profiles(app)
        tid = tag_id(app, FAV_TAG)
        for item in api(app, "/movie" if app == "radarr" else "/series"):
            who = favs.get(item[id_field])
            tagged = tid in item["tags"]
            if who and not tagged:
                upgrade = item["qualityProfileId"] == prof[phd]
                what = "upgrading to 4K" if upgrade else "kept forever"
                print(f"favourite: {item['title']} ({', '.join(sorted(who))}) -> {what}")
                if DRY:
                    continue
                item["tags"].append(tid)
                if upgrade:
                    item["qualityProfileId"] = prof[p4k]
                api(app, f"/{'movie' if app == 'radarr' else 'series'}/{item['id']}", "PUT", item)
                if upgrade:
                    api(app, "/command", "POST", search(item["id"]))
                notes.append(f"Favourite: {item['title']} ({', '.join(sorted(who))}) - {what}")
            elif tagged and not who:
                print(f"no longer a favourite: {item['title']} -> tag removed")
                if DRY:
                    continue
                item["tags"].remove(tid)
                api(app, f"/{'movie' if app == 'radarr' else 'series'}/{item['id']}", "PUT", item)


def watched_by_series(users):
    """tvdbId -> set of (season, episode) that at least one user has finished."""
    out = {}
    for u in users:
        tvdb = {s["Id"]: int(s["ProviderIds"]["Tvdb"]) for s in jf_items(u["Id"], IncludeItemTypes="Series")
                if s.get("ProviderIds", {}).get("Tvdb")}
        for e in jf_items(u["Id"], Filters="IsPlayed", IncludeItemTypes="Episode"):
            t = tvdb.get(e.get("SeriesId"))
            if t and e.get("ParentIndexNumber") and e.get("IndexNumber"):
                out.setdefault(t, set()).add((e["ParentIndexNumber"], e["IndexNumber"]))
    return out


def deleted_on_purpose(series_id):
    """Episodes whose file was deleted on purpose (in Jellyfin, by Leaving Soon, in Sonarr), not replaced by an upgrade:
    never fetched again."""
    rows = api("sonarr", f"/history/series?seriesId={series_id}&eventType=5")  # 5 = episodeFileDeleted
    return {h["episodeId"] for h in rows if h.get("eventType") == "episodeFileDeleted"
            and (h.get("data") or {}).get("reason") != "Upgrade"}


def sync_progress(users, notes):
    watched = watched_by_series(users)
    for s in api("sonarr", "/series"):
        seen = watched.get(s["tvdbId"])
        if not seen:
            continue
        eps = sorted((e for e in api("sonarr", f"/episode?seriesId={s['id']}") if e["seasonNumber"] > 0),
                     key=lambda e: (e["seasonNumber"], e["episodeNumber"]))
        if not eps or all(e["monitored"] for e in eps):
            continue
        order = [(e["seasonNumber"], e["episodeNumber"]) for e in eps]
        furthest = max((order.index(x) for x in seen if x in order), default=None)
        if furthest is None:
            continue
        cur_season = order[furthest][0]
        seasons = sorted({o[0] for o in order})
        later = [n for n in seasons if n > cur_season]
        if len(eps) <= WHOLE_SERIES_MAX:
            want, full_seasons, why = eps, seasons, "the whole series"
        else:
            nxt = later[0] if later else None
            nxt_size = sum(1 for o in order if o[0] == nxt) if nxt else 0
            cur_size = sum(1 for o in order if o[0] == cur_season)
            if max(nxt_size, cur_size) > LONG_SEASON:
                want, full_seasons = eps[:furthest + 1 + AHEAD], []
                why = f"the next {AHEAD} episodes"
            else:
                keep = [n for n in seasons if n <= (nxt or cur_season)]
                want, full_seasons = [e for e in eps if e["seasonNumber"] in keep], keep
                why = f"season {nxt}" if nxt else "the rest of the season"
        gone = deleted_on_purpose(s["id"])
        new = [e for e in want if not e["monitored"] and (e["hasFile"] or e["id"] not in gone)]
        if not new:
            continue
        print(f"progress: {s['title']} watched to S{order[furthest][0]:02d}E{order[furthest][1]:02d} "
              f"-> fetching {why} ({len(new)} episodes)")
        if DRY:
            continue
        api("sonarr", "/episode/monitor", "PUT", {"episodeIds": [e["id"] for e in new], "monitored": True})
        if full_seasons:
            fresh = api("sonarr", f"/series/{s['id']}")
            for sea in fresh["seasons"]:
                if sea["seasonNumber"] in full_seasons:
                    sea["monitored"] = True
            api("sonarr", f"/series/{s['id']}", "PUT", fresh)
        missing = [e for e in new if not e["hasFile"]]
        by_season = {}
        for e in missing:
            by_season.setdefault(e["seasonNumber"], []).append(e["id"])
        for n, ids in by_season.items():
            whole = len(ids) == sum(1 for o in order if o[0] == n)
            api("sonarr", "/command", "POST",
                {"name": "SeasonSearch", "seriesId": s["id"], "seasonNumber": n} if whole
                else {"name": "EpisodeSearch", "episodeIds": ids})
        notes.append(f"Now watching {s['title']}: fetching {why} ({len(new)} episodes)")


def season_one_first(users, notes):
    state = json.load(open(STATE)) if os.path.exists(STATE) else None
    series = api("sonarr", "/series")
    if state is None:   # first ever run: everything already here is left as it is
        state = {"seen": [x["id"] for x in series]}
        if not DRY:
            json.dump(state, open(STATE, "w"))
        return
    new = [x for x in series if x["id"] not in state["seen"]]
    if not new:
        return
    watched = watched_by_series(users)
    exempt = {tag_id("sonarr", FAV_TAG), tag_id("sonarr", KEEP_ALL_TAG)}
    for s in new:
        state["seen"].append(s["id"])
        if watched.get(s["tvdbId"]) or exempt & set(s["tags"]):
            continue
        mon = {x["seasonNumber"] for x in s["seasons"] if x["monitored"]}
        if 1 not in mon or not (mon - {1}):
            continue
        eps = api("sonarr", f"/episode?seriesId={s['id']}")
        drop = [e["id"] for e in eps if e["seasonNumber"] != 1 and e["monitored"]]
        queue = api("sonarr", f"/queue?pageSize=1000&seriesIds={s['id']}")["records"]
        queue = [r for r in queue if r.get("seriesId") == s["id"]]
        by_download = {}
        for r in queue:
            by_download.setdefault(r.get("downloadId") or r["id"], []).append(r)
        # cancel a download only if nothing in it belongs to season 1
        cancel = [r["id"] for recs in by_download.values() if all(x.get("seasonNumber") != 1 for x in recs) for r in recs]
        print(f"season 1 first: {s['title']} -> unmonitor {len(drop)} later episodes, cancel {len(cancel)} queued downloads")
        if DRY:
            continue
        if drop:
            api("sonarr", "/episode/monitor", "PUT", {"episodeIds": drop, "monitored": False})
        fresh = api("sonarr", f"/series/{s['id']}")
        for sea in fresh["seasons"]:
            sea["monitored"] = sea["seasonNumber"] == 1
        api("sonarr", f"/series/{s['id']}", "PUT", fresh)
        if cancel:
            api("sonarr", "/queue/bulk?removeFromClient=true&blocklist=false&skipRedownload=true", "DELETE", {"ids": cancel})
        notes.append(f"New show {s['title']}: season 1 only for now; the rest follows once someone starts watching")
    if not DRY:
        json.dump(state, open(STATE, "w"))


def main():
    users = api("jellyfin", "/Users")
    notes = []
    season_one_first(users, notes)
    sync_favourites(users, notes)
    sync_progress(users, notes)
    if notes and not DRY:
        telegram("\n".join(notes))
    print("done:", len(notes), "change(s)")


if __name__ == "__main__":
    main()
