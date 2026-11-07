# One-off (Sam 2026-10-22): delete every movie nobody has favorited in Jellyfin, recycle-bin copy included.
import importlib.util, os, shutil, time
S = "/home/sam/docker/media-stack/scripts"
s = importlib.util.spec_from_file_location("ls", f"{S}/leaving-soon.py"); ls = importlib.util.module_from_spec(s); s.loader.exec_module(ls)
BIN = "/media/.recycle/radarr"
p = ls.Pantera()
go = sorted((x for x in p.items(time.time()) if x["kind"] == "movie" and not x["favorite"]), key=lambda x: x["title"])
assert 170 <= len(go) <= 180, f"list changed: {len(go)} movies, stopping"  # Sam approved 178
print("deleting", len(go), flush=True)
freed = 0
for x in go:
    try:
        folder = os.path.basename(p.radarr(f"/movie/{x['id']}")["path"].rstrip("/"))
        freed += p.delete(x)
        b = os.path.join(BIN, folder)
        if folder and os.path.isdir(b):
            shutil.rmtree(b)
        print("deleted", x["title"], ls.gb(x["size"]), "(recycle copy left)" if os.path.exists(b) else "", flush=True)
    except Exception as e:
        print("FAILED", x["title"], repr(e), flush=True)
print("DONE torrents freed", ls.gb(freed), flush=True)
