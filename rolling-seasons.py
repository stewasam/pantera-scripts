#!/usr/bin/env python3
"""Rolling seasons: TV and anime arrive one season at a time.

For every show in Sonarr, the season you're on is the furthest one anyone has started in Jellyfin (season 1 if
nobody has). Seasons up to it are left as they are; once half of it has been watched, the next season is monitored
and searched; seasons further ahead are unmonitored so they don't download. Unwatched seasons already on disk past
the next one stay (leaving-soon.py removes a show only whole, when nobody has played any of it).

Skipped: unmonitored shows, shows tagged keep-all, and shows whose watched season numbers don't exist in Sonarr
(anime is often numbered differently).
Practice mode until scripts/rolling-seasons.live exists: prints what it would change, changes nothing.

  rolling-seasons.py          the hourly run
"""
import importlib.util, os, sys, time, urllib.parse

HERE = os.path.dirname(os.path.abspath(__file__))
LIVE = os.path.join(HERE, "rolling-seasons.live")
spec = importlib.util.spec_from_file_location("ls", os.path.join(HERE, "leaving-soon.py"))
ls = importlib.util.module_from_spec(spec); spec.loader.exec_module(ls)


def want(seasons, played, eps):
    """seasons: season numbers (>0) in Sonarr. played: {season: episodes anyone started}. eps: {season: episodes}.
    Returns (fetch, stop): the next season to monitor and search (or None), and the seasons that must not download.
    None instead of the pair if the numbering doesn't match (skip the show)."""
    if any(n not in seasons for n in played if n > 0 and played[n]):
        return None
    cur = max([n for n in played if n > 0 and played[n]] or [min(seasons)])
    fetch = cur + 1 if eps.get(cur) and played.get(cur, 0) * 2 >= eps[cur] and cur + 1 in seasons else None
    return fetch, {n for n in seasons if n > (fetch or cur)}


def played_episodes(p):
    """{(tvdb, season): episodes started or finished by anyone}."""
    out = {}
    q = urllib.parse.urlencode({"Recursive": "true", "Fields": "ProviderIds", "IncludeItemTypes": "Series,Episode"})
    for u in p.jf("/Users"):
        items = p.jf(f"/Users/{u['Id']}/Items?{q}")["Items"]
        tvdb = {it["Id"]: int(it["ProviderIds"]["Tvdb"]) for it in items
                if it["Type"] == "Series" and (it.get("ProviderIds") or {}).get("Tvdb")}
        for it in items:
            ud = it.get("UserData") or {}
            if it["Type"] == "Episode" and it.get("SeriesId") in tvdb and it.get("ParentIndexNumber") is not None and \
                    (ud.get("Played") or ud.get("PlayCount", 0) > 0 or ud.get("PlaybackPositionTicks", 0) > 0):
                out.setdefault((tvdb[it["SeriesId"]], it["ParentIndexNumber"]), set()).add(it["Id"])
    return {k: len(v) for k, v in out.items()}


def main():
    practice = not os.path.exists(LIVE)
    p = ls.Pantera()
    played = played_episodes(p)
    if not played:  # Jellyfin answered but nobody has watched anything: more likely an outage than the truth
        print(f"{time.strftime('%F %T')} stopped: no watched episodes found in Jellyfin; nothing changed", flush=True)
        return
    stags = {t["id"]: t["label"] for t in p.sonarr("/tag")}
    fetched, changed = [], 0
    for s in p.sonarr("/series"):
        if not s.get("monitored") or "keep-all" in {stags.get(t) for t in s.get("tags", [])}:
            continue
        seasons = {x["seasonNumber"]: x for x in s["seasons"] if x["seasonNumber"] > 0}
        if not seasons:
            continue
        mine = {n: c for (tv, n), c in played.items() if tv == s.get("tvdbId")}
        eps = {n: x.get("statistics", {}).get("totalEpisodeCount", 0) for n, x in seasons.items()}
        plan = want(set(seasons), mine, eps)
        if plan is None:
            continue
        fetch, stop = plan
        on = [fetch] if fetch and not seasons[fetch]["monitored"] else []
        off = sorted(n for n in stop if seasons[n]["monitored"])
        if not on and not off:
            continue
        changed += 1
        print(f"{'(practice) ' if practice else ''}{s['title']}: monitor {on or '-'}, stop {off or '-'}", flush=True)
        if practice:
            continue
        for x in s["seasons"]:
            if x["seasonNumber"] in on:
                x["monitored"] = True
            elif x["seasonNumber"] in off:
                x["monitored"] = False
        p.sonarr(f"/series/{s['id']}", method="PUT", body=s)
        for n in on:
            p.sonarr("/command", method="POST", body={"name": "SeasonSearch", "seriesId": s["id"], "seasonNumber": n})
            fetched.append(f"{s['title']} season {n}")
    if fetched:
        p.telegram("Rolling seasons: you're halfway, so the next season is on its way:\n" +
                   "\n".join(f"• {f}" for f in fetched))
    print(f"{time.strftime('%F %T')} {changed} show(s) {'would change' if practice else 'changed'}; "
          f"{'practice mode' if practice else 'LIVE'}", flush=True)


if __name__ == "__main__":
    main()
