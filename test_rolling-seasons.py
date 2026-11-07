"""Self-check for rolling-seasons.want. Run: python3 test_rolling-seasons.py"""
import importlib.util
spec = importlib.util.spec_from_file_location("rs", "/home/sam/docker/media-stack/scripts/rolling-seasons.py")
rs = importlib.util.module_from_spec(spec); spec.loader.exec_module(rs)
S, EPS = {1, 2, 3, 4}, {1: 10, 2: 10, 3: 10, 4: 10}

# nobody started: stay on season 1, nothing fetched, 2+ stop
assert rs.want(S, {}, EPS) == (None, {2, 3, 4})
# 4 of 10 into season 1: not halfway yet
assert rs.want(S, {1: 4}, EPS) == (None, {2, 3, 4})
# exactly halfway through season 1: fetch season 2, 3+ stop
assert rs.want(S, {1: 5}, EPS) == (2, {3, 4})
# on season 3 (started at 3, skipped 1-2): earlier seasons untouched, 4 stops until halfway
assert rs.want(S, {3: 1}, EPS) == (None, {4})
# halfway through the last season: nothing to fetch, nothing to stop
assert rs.want(S, {4: 9}, EPS) == (None, set())
# watched season numbers Sonarr doesn't have (anime numbering): skip the show
assert rs.want(S, {7: 3}, EPS) is None
# unknown episode count: never fetch
assert rs.want(S, {1: 5}, {}) == (None, {2, 3, 4})
print("7 checks passed")
