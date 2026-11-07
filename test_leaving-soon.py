"""Self-check for leaving-soon's rules. Run: python3 test_leaving-soon.py (recovered 2026-11-07 from the 10-22 session)."""
import importlib.util
spec = importlib.util.spec_from_file_location("ls", "/home/sam/docker/media-stack/scripts/leaving-soon.py")
ls = importlib.util.module_from_spec(spec); spec.loader.exec_module(ls)

DAY = 86400
NOW = 1_800_000_000
TB = 1e12


def item(key, days_ago, size=10e9, **kw):
    return dict({"key": key, "title": key, "kind": "movie", "added": NOW - days_ago * DAY, "size": size,
                 "played": False, "favorite": False, "protected": None}, **kw)


def plan(items, listed=None, used=0.5, total=11 * TB):
    return ls.plan(items, listed or {}, total, used * total, NOW)


def keys(xs):
    return sorted(x["key"] for x in xs)


def test_unwatched_for_60_days_goes_on_the_list_for_a_week():
    p = plan([item("old", 61), item("new", 59)])
    assert keys(p["add"]) == ["old"] and p["add"][0]["leaves"] == NOW + 7 * DAY and p["add"][0]["why"] == "60 days unwatched"


def test_played_favorited_or_protected_never_listed():
    p = plan([item("seen", 90, played=True), item("loved", 90, favorite=True), item("asked", 90, protected="requested in Seerr")])
    assert p["add"] == []


def test_listed_item_leaves_after_seven_days_unless_played_or_favorited_meanwhile():
    listed = {k: {"listed": NOW - 8 * DAY, "leaves": NOW - DAY, "why": "60 days unwatched"} for k in ("a", "b", "c", "d")}
    p = plan([item("a", 90), item("b", 90, played=True), item("c", 90, favorite=True), item("d", 90)],
             dict(listed, d=dict(listed["d"], leaves=NOW + DAY)))
    assert keys(p["due"]) == ["a"]  # d still has a day
    assert keys(p["drop"]) == ["b", "c"]  # saved: off the list


def test_gone_from_the_library_drops_off_the_list():
    p = plan([], {"x": {"listed": NOW, "leaves": NOW + DAY, "why": "60 days unwatched"}})
    assert [d["key"] for d in p["drop"]] == ["x"]


def test_over_85_percent_lists_oldest_unwatched_30_days_or_more_until_back_to_85():
    items = [item("a40", 40, size=0.2 * TB), item("b50", 50, size=0.2 * TB), item("c35", 35, size=0.2 * TB), item("d20", 20, size=1 * TB)]
    p = plan(items, used=0.88)  # 0.03 * 11 TB = 0.33 TB to free: the two oldest
    assert keys(p["add"]) == ["a40", "b50"] and {x["why"] for x in p["add"]} == {"drive over 85%"}


def test_disk_rule_counts_whats_already_listed():
    listed = {"big": {"listed": NOW, "leaves": NOW + DAY, "why": "60 days unwatched"}}
    p = plan([item("big", 70, size=0.5 * TB), item("a", 40, size=0.2 * TB)], listed, used=0.88)
    assert p["add"] == []  # leaving "big" already brings it under 85%


def test_under_85_percent_no_early_listing():
    assert plan([item("a", 40)], used=0.84)["add"] == []


def test_already_listed_not_added_again():
    listed = {"old": {"listed": NOW - DAY, "leaves": NOW + 6 * DAY, "why": "60 days unwatched"}}
    assert plan([item("old", 90)], listed)["add"] == []


def test_seasons_from_episode_files_and_watch_state():
    files = [{"seasonNumber": 1, "dateAdded": "2026-07-01T00:00:00Z", "size": 5, "id": 11},
             {"seasonNumber": 1, "dateAdded": "2026-09-01T00:00:00Z", "size": 6, "id": 12},
             {"seasonNumber": 2, "dateAdded": "2026-08-01T00:00:00Z", "size": 7, "id": 21}]
    s = ls.seasons_of({"id": 3, "title": "Show", "tvdbId": 99, "tags": []}, files)
    assert [(x["key"], x["size"], x["file_ids"]) for x in s] == [("season:3:1", 11, [11, 12]), ("season:3:2", 7, [21])]
    assert s[0]["added"] == ls.ts("2026-09-01T00:00:00Z")  # the newest episode decides


# --- rolling seasons: seasons past where you are ---
def season(key, n, tv=1, days_ago=5, **kw):
    return item(key, days_ago, kind="season", season=n, tvdb=tv, **kw)


def test_furthest_is_season_1_when_nobody_started():
    assert ls.furthest(1, set()) == 1
    assert ls.furthest(1, {(1, 3), (1, 1), (2, 9), (1, 0)}) == 3


def test_season_past_where_you_are_is_listed_at_once():
    p = plan([season("s3", 3, beyond=True), season("s2", 2, beyond=False)])
    assert [(x["key"], x["why"]) for x in p["add"]] == [("s3", ls.BEYOND)]


def test_beyond_still_respects_favorite_played_protected():
    p = plan([season("a", 4, beyond=True, favorite=True), season("b", 5, beyond=True, played=True),
              season("c", 6, beyond=True, protected="tagged keep")])
    assert p["add"] == []


def test_beyond_comes_off_the_list_once_you_catch_up():
    listed = {"s3": {"listed": NOW - DAY, "leaves": NOW + 6 * DAY, "why": ls.BEYOND, "title": "s3", "size": 1}}
    p = plan([season("s3", 3, beyond=False)], listed)
    assert [d["key"] for d in p["drop"]] == ["s3"] and p["due"] == []


def test_beyond_leaves_after_the_week():
    listed = {"s3": {"listed": NOW - 8 * DAY, "leaves": NOW - DAY, "why": ls.BEYOND, "title": "s3", "size": 1}}
    p = plan([season("s3", 3, beyond=True)], listed)
    assert [x["key"] for x in p["due"]] == ["s3"]


if __name__ == "__main__":
    tests = [f for n, f in sorted(globals().items()) if n.startswith("test_")]
    for t in tests:
        t()
    print(f"{len(tests)} checks passed")
