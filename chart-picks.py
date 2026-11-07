#!/usr/bin/env python3
"""Weekly streaming-chart picks, with a 14-day expiry for the ones nobody watches.

Picks: the most popular recent English-language films and shows on Disney+,
Max, Prime Video and Apple TV+ (TMDB popularity, read through Seerr - a proxy
for each service's own top 10, not the official chart). Picks are added to
Radarr/Sonarr with the tag "top10"; shows start with season 1 only.

Daily upkeep:
  - Started watching (either user) or favourited: the "top10" tag comes off,
    so the title is kept like any other. Further seasons are handled by
    jellyfin-sync.py.
  - Still tagged, untouched 14 days after it arrived: removed, with an
    exclusion so it is never picked again.
  - Jellyfin collection "Top 10" mirrors the current picks; "Leaving Soon"
    shows picks in their last 3 days (only once removal is switched on).

Removal is REPORT-ONLY until the file scripts/chart-picks.apply exists: the
script only says what it would remove. Music is never touched.

  chart-picks.py              daily upkeep
  chart-picks.py --pick       add this week's picks, then upkeep
  chart-picks.py --dry-run    change nothing, print what would happen
"""
import datetime as dt
import json, os, re, sys, urllib.parse, urllib.request, urllib.error

STACK = "/home/sam/docker/media-stack"
STATE = f"{STACK}/scripts/chart-picks-state.json"
APPLY_FLAG = f"{STACK}/scripts/chart-picks.apply"
SERVICES = {"Disney+": 337, "Max": 1899, "Prime Video": 9, "Apple TV+": 350}
REGION = "US"
MOVIES_PER_SERVICE, SHOWS_PER_SERVICE = 5, 3
MOVIE_WINDOW_DAYS, SHOW_WINDOW_DAYS = 365, 730
EXPIRE_DAYS, LEAVING_SOON_DAYS = 14, 3
SKIP_TV_GENRES = {10762, 10763, 10764, 10766, 10767}  # kids, news, reality, soap, talk
TAG, FAV_TAG = "top10", "favourite"
DRY = "--dry-run" in sys.argv
APPLY = os.path.exists(APPLY_FLAG)
TODAY = dt.date.today()


def env(path):
    return dict(l.strip().split("=", 1) for l in open(path) if "=" in l and not l.startswith("#"))


def xml_key(app):
    return re.search(r"<ApiKey>([^<]+)", open(f"{STACK}/config/{app}/config.xml").read()).group(1)


APPS = {"radarr": ("http://localhost:7878/api/v3", {"X-Api-Key": xml_key("radarr")}),
        "sonarr": ("http://localhost:8989/api/v3", {"X-Api-Key": xml_key("sonarr")}),
        "jellyfin": ("http://localhost:8096", {"X-Emby-Token": env(f"{STACK}/config/automation.env")["JELLYFIN_API_KEY"]}),
        "seerr": ("http://localhost:5055/api/v1",
                  {"X-Api-Key": json.load(open(f"{STACK}/config/jellyseerr/settings.json"))["main"]["apiKey"]})}


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
    return -1 if DRY else api(app, "/tag", "POST", {"label": label})["id"]


def profile(app, name):
    return next(p["id"] for p in api(app, "/qualityprofile") if p["name"] == name)


def discover(kind, provider, since):
    date_param = "primaryReleaseDateGte" if kind == "movies" else "firstAirDateGte"
    out = []
    for page in (1, 2, 3):
        q = urllib.parse.urlencode({"watchProviders": provider, "watchRegion": REGION, "sortBy": "popularity.desc",
                                    date_param: since.isoformat(), "page": page})
        out += api("seerr", f"/discover/{kind}?{q}")["results"]
    return out


# ---------------------------------------------------------------- weekly picks
def pick(state, notes):
    picks = state["picks"]
    have_m = {m["tmdbId"] for m in api("radarr", "/movie")}
    excl_m = {e["tmdbId"] for e in api("radarr", "/exclusions")}
    have_s = {s["tvdbId"] for s in api("sonarr", "/series")}
    excl_s = {e["tvdbId"] for e in api("sonarr", "/importlistexclusion")}
    mt, st = tag_id("radarr", TAG), tag_id("sonarr", TAG)
    mprof, sprof = profile("radarr", "HD Bluray + WEB"), profile("sonarr", "WEB-1080p")
    lines = []
    for service, pid in SERVICES.items():
        got = []
        for r in discover("movies", pid, TODAY - dt.timedelta(days=MOVIE_WINDOW_DAYS)):
            if len(got) >= MOVIES_PER_SERVICE:
                break
            rel = r.get("releaseDate") or "9999"
            if r.get("adult") or r.get("originalLanguage") != "en" or rel > TODAY.isoformat():
                continue
            if r["id"] in have_m or r["id"] in excl_m:
                continue
            got.append(r["title"])
            have_m.add(r["id"])
            if DRY:
                continue
            m = api("radarr", f"/movie/lookup/tmdb?tmdbId={r['id']}")
            m.update({"qualityProfileId": mprof, "rootFolderPath": "/media/movies", "monitored": True,
                      "minimumAvailability": "released", "tags": [mt], "addOptions": {"searchForMovie": True}})
            api("radarr", "/movie", "POST", m)
            picks[f"movie:{r['id']}"] = {"title": r["title"], "service": service, "picked": TODAY.isoformat()}
        shows = []
        for r in discover("tv", pid, TODAY - dt.timedelta(days=SHOW_WINDOW_DAYS)):
            if len(shows) >= SHOWS_PER_SERVICE:
                break
            if r.get("originalLanguage") != "en" or SKIP_TV_GENRES & set(r.get("genreIds") or []):
                continue
            if (r.get("firstAirDate") or "9999") > TODAY.isoformat():
                continue
            tvdb = (api("seerr", f"/tv/{r['id']}").get("externalIds") or {}).get("tvdbId")
            if not tvdb or tvdb in have_s or tvdb in excl_s:
                continue
            shows.append(r["name"])
            have_s.add(tvdb)
            if DRY:
                continue
            s = api("sonarr", "/series/lookup?term=" + urllib.parse.quote(f"tvdb:{tvdb}"))[0]
            s.update({"qualityProfileId": sprof, "rootFolderPath": "/media/tv", "seasonFolder": True,
                      "monitored": True, "tags": [st],
                      "addOptions": {"monitor": "firstSeason", "searchForMissingEpisodes": True}})
            api("sonarr", "/series", "POST", s)
            picks[f"series:{tvdb}"] = {"title": r["name"], "service": service, "picked": TODAY.isoformat()}
        print(f"{service}: films {got} | shows {shows}")
        if got or shows:
            lines.append(f"{service}: " + ", ".join(got + [s + " (S1)" for s in shows]))
    if lines:
        notes.append("This week's chart picks:\n" + "\n".join(lines))


# ---------------------------------------------------------------- daily upkeep
def jellyfin_view():
    """Per provider id: Jellyfin item id, and whether any user has started or favourited it."""
    items, started = {}, set()
    for u in api("jellyfin", "/Users"):
        def q(**p):
            return api("jellyfin", f"/Users/{u['Id']}/Items?" + urllib.parse.urlencode(
                {"Recursive": "true", "Fields": "ProviderIds", **p}))["Items"]
        series_key = {}
        for it in q(IncludeItemTypes="Movie,Series"):
            ids, ud = it.get("ProviderIds", {}), it.get("UserData", {})
            key = (f"movie:{ids['Tmdb']}" if it["Type"] == "Movie" and ids.get("Tmdb")
                   else f"series:{ids['Tvdb']}" if it["Type"] == "Series" and ids.get("Tvdb") else None)
            if not key:
                continue
            items[key] = it["Id"]
            if it["Type"] == "Series":
                series_key[it["Id"]] = key
            if ud.get("Played") or ud.get("PlaybackPositionTicks") or ud.get("PlayedPercentage") or ud.get("IsFavorite"):
                started.add(key)
        for flt in ("IsPlayed", "IsResumable"):
            for e in q(IncludeItemTypes="Episode", Filters=flt):
                if e.get("SeriesId") in series_key:
                    started.add(series_key[e["SeriesId"]])
    return items, started


def parse(ts):
    return dt.datetime.fromisoformat(ts.replace("Z", "+00:00")).date()


def upkeep(state, notes):
    jf_items, started = jellyfin_view()
    current, leaving, removed, would = [], [], [], []
    for app, kind, id_field in (("radarr", "movie", "tmdbId"), ("sonarr", "series", "tvdbId")):
        tid, fav = tag_id(app, TAG), tag_id(app, FAV_TAG)
        for item in api(app, f"/{kind}"):
            if tid not in item["tags"]:
                continue
            key = f"{kind}:{item[id_field]}"
            if key in started or fav in item["tags"]:
                print("keeping (watched or favourite):", item["title"])
                if not DRY:
                    item["tags"].remove(tid)
                    api(app, f"/{kind}/{item['id']}", "PUT", item)
                    state["picks"].get(key, {})["kept"] = TODAY.isoformat()
                    notes.append(f"Chart pick kept: {item['title']} (someone started it)")
                continue
            if kind == "movie":
                arrived = parse(item["movieFile"]["dateAdded"]) if item.get("movieFile") else parse(item["added"])
            else:
                files = api("sonarr", f"/episodefile?seriesId={item['id']}")
                arrived = min((parse(f["dateAdded"]) for f in files), default=parse(item["added"]))
            age = (TODAY - arrived).days
            if key in jf_items:
                current.append(jf_items[key])
            if age >= EXPIRE_DAYS:
                if APPLY and not DRY:
                    flag = "addImportExclusion" if app == "radarr" else "addImportListExclusion"
                    api(app, f"/{kind}/{item['id']}?deleteFiles=true&{flag}=true", "DELETE")
                    state["picks"].get(key, {})["removed"] = TODAY.isoformat()
                    removed.append(item["title"])
                else:
                    would.append(f"{item['title']} ({age} days)")
            elif age >= EXPIRE_DAYS - LEAVING_SOON_DAYS and key in jf_items:
                leaving.append(jf_items[key])
    print(f"tagged picks in Jellyfin: {len(current)} | leaving soon: {len(leaving)} | removed: {removed} | would remove: {would}")
    if removed:
        notes.append("Chart picks removed (unwatched for 14 days):\n" + "\n".join("- " + r for r in removed))
    if would and would != state.get("last_would"):
        notes.append("Report only, nothing deleted. Chart picks that would be removed (unwatched 14+ days):\n"
                     + "\n".join("- " + w for w in would))
    if not DRY:
        state["last_would"] = would
        collection("Top 10", current)
        if APPLY:
            collection("Leaving Soon", leaving)


def collection(name, want):
    boxes = api("jellyfin", "/Items?" + urllib.parse.urlencode({"IncludeItemTypes": "BoxSet", "Recursive": "true"}))["Items"]
    box = next((b for b in boxes if b["Name"] == name), None)
    if not box:
        if want:
            api("jellyfin", "/Collections?" + urllib.parse.urlencode({"name": name, "ids": ",".join(want)}), "POST")
        return
    have = {i["Id"] for i in api("jellyfin", f"/Items?ParentId={box['Id']}")["Items"]}
    add, drop = set(want) - have, have - set(want)
    if add:
        api("jellyfin", f"/Collections/{box['Id']}/Items?ids=" + ",".join(add), "POST")
    if drop:
        api("jellyfin", f"/Collections/{box['Id']}/Items?ids=" + ",".join(drop), "DELETE")


def main():
    state = json.load(open(STATE)) if os.path.exists(STATE) else {"picks": {}}
    notes = []
    print(f"{TODAY} | removal: {'ON' if APPLY else 'report-only'}{' | dry run' if DRY else ''}")
    if "--pick" in sys.argv:
        pick(state, notes)
    upkeep(state, notes)
    if not DRY:
        json.dump(state, open(STATE, "w"), indent=1)
        if notes:
            telegram("\n\n".join(notes))


if __name__ == "__main__":
    main()
