# Read-only: torrents whose every file has no other hard link (nothing in the library uses them).
import importlib.util, os, json, collections
S = "/home/sam/docker/media-stack/scripts"
s = importlib.util.spec_from_file_location("ls", f"{S}/leaving-soon.py"); ls = importlib.util.module_from_spec(s); s.loader.exec_module(ls)
p = ls.Pantera()
cats = collections.Counter(); out = []
for t in p.qbt("torrents/info"):
    cp = t["content_path"]
    if not os.path.exists(cp): continue
    files = [cp] if os.path.isfile(cp) else [os.path.join(d, f) for d, _, fs in os.walk(cp) for f in fs]
    if files and all(os.stat(f).st_nlink == 1 for f in files):
        tags = t.get("tags", "")
        cats[t["category"]] += t["size"]
        out.append({"hash": t["hash"], "name": t["name"], "cat": t["category"], "tags": tags, "size": t["size"]})
json.dump(out, open(f"{S}/orphan-torrents-2026-10-22.json", "w"), indent=1)
for c, b in cats.items(): print(c or "(none)", ls.gb(b), sum(1 for o in out if o["cat"] == c))
for o in out:
    if "review" in o["tags"]: print("review-tagged:", o["name"], ls.gb(o["size"]))
