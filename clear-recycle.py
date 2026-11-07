# Remove recycle-bin copies of today's deleted movies (Sam approved freeing the space). Keeps the 9 older leftovers
# and anything Radarr still has in the library.
import importlib.util, os, re, shutil
S = "/home/sam/docker/media-stack/scripts"
s = importlib.util.spec_from_file_location("ls", f"{S}/leaving-soon.py"); ls = importlib.util.module_from_spec(s); s.loader.exec_module(ls)
BIN = "/media/.recycle/radarr"
OLD = {15, 17654, 1366, 103, 218, 629, 630, 597, 33}  # in the bin before today's run
have = {m["tmdbId"] for m in ls.Pantera().radarr("/movie")}
for d in sorted(os.listdir(BIN)):
    m = re.search(r"\[tmdbid-(\d+)\]", d)
    if not m or int(m.group(1)) in OLD or int(m.group(1)) in have:
        print("kept", d); continue
    shutil.rmtree(os.path.join(BIN, d)); print("removed", d)
