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


def test_over_the_limit_lists_oldest_unwatched_30_days_or_more_until_back_under():
    items = [item("a40", 40, size=0.2 * TB), item("b50", 50, size=0.2 * TB), item("c35", 35, size=0.2 * TB), item("d20", 20, size=1 * TB)]
    p = plan(items, used=ls.DISK_LIMIT + 0.03)  # 0.03 * 11 TB = 0.33 TB to free: the two oldest
    assert keys(p["add"]) == ["a40", "b50"] and {x["why"] for x in p["add"]} == {ls.DISK_WHY}


def test_disk_rule_counts_whats_already_listed():
    listed = {"big": {"listed": NOW, "leaves": NOW + DAY, "why": "60 days unwatched"}}
    p = plan([item("big", 70, size=0.5 * TB), item("a", 40, size=0.2 * TB)], listed, used=ls.DISK_LIMIT + 0.03)
    assert p["add"] == []  # leaving "big" already brings it under the limit


def test_under_the_limit_no_early_listing():
    assert plan([item("a", 40)], used=ls.DISK_LIMIT - 0.01)["add"] == []


def test_already_listed_not_added_again():
    listed = {"old": {"listed": NOW - DAY, "leaves": NOW + 6 * DAY, "why": "60 days unwatched"}}
    assert plan([item("old", 90)], listed)["add"] == []


def test_a_show_is_one_item_dated_by_its_newest_episode():
    files = [{"seasonNumber": 1, "dateAdded": "2026-07-01T00:00:00Z", "size": 5, "id": 11},
             {"seasonNumber": 1, "dateAdded": "2026-09-01T00:00:00Z", "size": 6, "id": 12},
             {"seasonNumber": 2, "dateAdded": "2026-08-01T00:00:00Z", "size": 7, "id": 21}]
    x = ls.show_of({"id": 3, "title": "Show", "tvdbId": 99, "tags": []}, files)
    assert (x["key"], x["kind"], x["title"], x["size"], x["file_ids"]) == ("show:3", "show", "Show", 18, [11, 12, 21])
    assert x["added"] == ls.ts("2026-09-01T00:00:00Z")  # a new episode restarts the clock
    assert ls.show_of({"id": 4, "title": "Empty"}, []) is None


def test_a_show_follows_the_same_rules_as_a_movie():
    show = lambda key, days, **kw: item(key, days, kind="show", **kw)
    p = plan([show("old", 61), show("new", 20), show("seen", 90, played=True), show("loved", 90, favorite=True)])
    assert keys(p["add"]) == ["old"] and p["add"][0]["why"] == "60 days unwatched"


def test_an_old_season_entry_drops_off():  # listed by the season rule before 11-08: its key no longer exists
    p = plan([item("show:35", 24, kind="show")], {"season:35:3": {"listed": NOW - DAY, "leaves": NOW + 6 * DAY, "why": "past where you are", "title": "x", "size": 1}})
    assert [d["key"] for d in p["drop"]] == ["season:35:3"] and p["add"] == []


if __name__ == "__main__":
    tests = [f for n, f in sorted(globals().items()) if n.startswith("test_")]
    for t in tests:
        t()
    print(f"{len(tests)} checks passed")
