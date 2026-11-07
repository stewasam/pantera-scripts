#!/usr/bin/env python3
"""Feed the approved starter library into Radarr and Sonarr a batch at a time.

Reads scripts/starter-library.json, keeps progress in
scripts/starter-library-state.json. Each run adds the next batch only when the
download queues have room and the media drive is under 80% full, then sends one
Telegram summary. While the fill is in progress the per-download Telegram
messages from Radarr/Sonarr are switched off (several hundred downloads); they
are restored automatically when the list is finished. Seerr's own "available"
messages for requests are not touched.

  starter-library.py            add the next batch
  starter-library.py --dry-run  show what the next batch would be
  starter-library.py --status   progress only
"""
import json, os, re, shutil, sys, time, urllib.parse, urllib.request, urllib.error

STACK = "/home/sam/docker/media-stack"
DATA = f"{STACK}/scripts/starter-library.json"
STATE = f"{STACK}/scripts/starter-library-state.json"
MOVIE_BATCH, SERIES_BATCH = 25, 4
MOVIE_QUEUE_MAX, SERIES_QUEUE_MAX = 12, 40   # Sonarr queue counts episodes
DISK_LIMIT = 0.80
DRY = "--dry-run" in sys.argv


def key(app):
    return re.search(r"<ApiKey>([^<]+)", open(f"{STACK}/config/{app}/config.xml").read()).group(1)


APPS = {"radarr": ("http://localhost:7878/api/v3", key("radarr")),
        "sonarr": ("http://localhost:8989/api/v3", key("sonarr"))}


def api(app, path, method="GET", body=None):
    base, k = APPS[app]
    r = urllib.request.Request(base + path, method=method,
                               data=json.dumps(body).encode() if body is not None else None,
                               headers={"X-Api-Key": k, "Content-Type": "application/json"})
    with urllib.request.urlopen(r, timeout=120) as f:
        t = f.read()
        return json.loads(t) if t else None


def telegram(text):
    env = open(f"{STACK}/config/notify.env").read()
    tok = re.search(r"TELEGRAM_BOT_TOKEN=(\S+)", env).group(1).strip("\"'")
    chat = re.search(r"TELEGRAM_CHAT_ID=(\S+)", env).group(1).strip("\"'")
    data = urllib.parse.urlencode({"chat_id": chat, "text": text}).encode()
    try:
        urllib.request.urlopen(f"https://api.telegram.org/bot{tok}/sendMessage", data=data, timeout=30)
    except Exception as e:  # a failed message must not stop the fill
        print("telegram failed:", e)


def profile(app, name):
    return next(p["id"] for p in api(app, "/qualityprofile") if p["name"] == name)


def tag(app, label="starter"):
    for t in api(app, "/tag"):
        if t["label"] == label:
            return t["id"]
    return api(app, "/tag", "POST", {"label": label})["id"]


def norm(s):
    return re.sub(r"[^a-z0-9]", "", s.lower())


def pick(results, title, year):
    """Exact year first, then one year either side; within that, prefer a title match."""
    for tol in (0, 1):
        c = [r for r in results if r.get("year") and abs(r["year"] - year) <= tol]
        if c:
            exact = [r for r in c if norm(r["title"]) == norm(title)]
            return (exact or c)[0]
    return None


# Per-download messages that would otherwise fire for every item in the fill.
QUIET_FLAGS = {"radarr": ["onDownload", "onUpgrade"],
               "sonarr": ["onDownload", "onImportComplete", "onUpgrade"]}


def quiet(state, on):
    saved = state.setdefault("notifications", {})
    for app, flags in QUIET_FLAGS.items():
        for n in api(app, "/notification"):
            if n.get("implementation") != "Telegram":
                continue
            k = f"{app}:{n['id']}"
            if on and k not in saved:
                saved[k] = {f: n.get(f) for f in flags}
                for f in flags:
                    n[f] = False
            elif not on and k in saved:
                for f, v in saved.pop(k).items():
                    n[f] = v
            else:
                continue
            api(app, f"/notification/{n['id']}", "PUT", n)


def main():
    data = json.load(open(DATA))
    state = json.load(open(STATE)) if os.path.exists(STATE) else {"done": {}, "unmatched": {}}
    done, unmatched = state["done"], state["unmatched"]
    k = lambda kind, x: f"{kind}:{x['title']} ({x['year']})"
    todo_m = [x for x in data["movies"] if k("movie", x) not in done and k("movie", x) not in unmatched]
    todo_s = [x for x in data["series"] if k("series", x) not in done and k("series", x) not in unmatched]
    total = len(data["movies"]) + len(data["series"])
    print(f"progress: {len(done)}/{total} added, {len(unmatched)} unmatched, "
          f"{len(todo_m)} films and {len(todo_s)} shows to go")
    if "--status" in sys.argv:
        return

    if not todo_m and not todo_s:
        if not state.get("finished") and not DRY:
            quiet(state, False)
            state["finished"] = time.strftime("%Y-%m-%d %H:%M")
            json.dump(state, open(STATE, "w"), indent=1)
            msg = f"Starter library: all {len(done)} titles are added. Download messages are back on."
            if unmatched:
                msg += "\nCould not match: " + ", ".join(x.split(":", 1)[1] for x in unmatched)
            telegram(msg)
        return

    du = shutil.disk_usage("/media")
    if du.used / du.total > DISK_LIMIT:
        print("media drive over limit, not adding")
        if not state.get("disk_warned") and not DRY:
            state["disk_warned"] = True
            json.dump(state, open(STATE, "w"), indent=1)
            telegram("Starter library paused: the media drive is over 80% full.")
        return

    if not DRY:
        quiet(state, True)
    added, missed = [], []

    mq = api("radarr", "/queue?pageSize=1")["totalRecords"]
    if todo_m and mq < MOVIE_QUEUE_MAX:
        have = {m["tmdbId"] for m in api("radarr", "/movie")}
        prof = {False: profile("radarr", "HD Bluray + WEB"), True: profile("radarr", "[Anime] Remux-1080p")}
        t = tag("radarr")
        for x in todo_m[:MOVIE_BATCH]:
            res = api("radarr", "/movie/lookup?term=" + urllib.parse.quote(f"{x['title']} {x['year']}"))
            m = pick(res, x["title"], x["year"]) or pick(
                api("radarr", "/movie/lookup?term=" + urllib.parse.quote(x["title"])), x["title"], x["year"])
            if not m:
                unmatched[k("movie", x)] = "no lookup match"
                missed.append(f"{x['title']} ({x['year']})")
                continue
            label = f"{m['title']} ({m['year']})"
            if DRY:
                print("  would add film:", label, "<-", x["title"], "|", x["group"])
                continue
            if m["tmdbId"] not in have:
                m.update({"qualityProfileId": prof[x["anime"]],
                          "rootFolderPath": "/media/anime/movies" if x["anime"] else "/media/movies",
                          "monitored": True, "minimumAvailability": "released", "tags": [t],
                          "addOptions": {"searchForMovie": True}})
                api("radarr", "/movie", "POST", m)
                have.add(m["tmdbId"])
                added.append(label)
            done[k("movie", x)] = {"matched": label, "tmdbId": m["tmdbId"], "when": time.strftime("%Y-%m-%d %H:%M")}
            json.dump(state, open(STATE, "w"), indent=1)
    else:
        print(f"radarr queue {mq}: no films added this run")

    sq = api("sonarr", "/queue?pageSize=1")["totalRecords"]
    if todo_s and sq < SERIES_QUEUE_MAX:
        have = {s["tvdbId"] for s in api("sonarr", "/series")}
        prof = {False: profile("sonarr", "WEB-1080p"), True: profile("sonarr", "[Anime] Remux-1080p")}
        t = tag("sonarr")
        for x in todo_s[:SERIES_BATCH]:
            s = pick(api("sonarr", "/series/lookup?term=" + urllib.parse.quote(x["search"])), x["search"], x["year"])
            if not s:
                unmatched[k("series", x)] = "no lookup match"
                missed.append(x["title"])
                continue
            label = f"{s['title']} ({s['year']})"
            if DRY:
                print("  would add show:", label, "<-", x["title"], "|", x["group"])
                continue
            if s["tvdbId"] not in have:
                s.update({"qualityProfileId": prof[x["anime"]],
                          "rootFolderPath": "/media/anime/tv" if x["anime"] else "/media/tv",
                          "seasonFolder": True, "monitored": True, "tags": [t],
                          "seriesType": "anime" if x["anime"] else "standard",
                          "addOptions": {"monitor": "firstSeason", "searchForMissingEpisodes": True}})
                api("sonarr", "/series", "POST", s)
                have.add(s["tvdbId"])
                added.append(label + " S1")
            done[k("series", x)] = {"matched": label, "tvdbId": s["tvdbId"], "when": time.strftime("%Y-%m-%d %H:%M")}
            json.dump(state, open(STATE, "w"), indent=1)
    else:
        print(f"sonarr queue {sq}: no shows added this run")

    if DRY:
        return
    json.dump(state, open(STATE, "w"), indent=1)
    print("added:", added, "| missed:", missed)
    if added or missed:
        msg = f"Starter library: added {len(added)} this round ({len(done)}/{total} so far)."
        if added:
            msg += "\n" + "\n".join("- " + a for a in added)
        if missed:
            msg += "\nCould not match: " + ", ".join(missed)
        telegram(msg[:3900])


if __name__ == "__main__":
    main()
