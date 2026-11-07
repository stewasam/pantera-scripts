# Remove radarr torrents nothing in the library links to (today's deleted movies). Rechecks links before each delete.
import importlib.util, os, json
S = "/home/sam/docker/media-stack/scripts"
s = importlib.util.spec_from_file_location("ls", f"{S}/leaving-soon.py"); ls = importlib.util.module_from_spec(s); s.loader.exec_module(ls)
p = ls.Pantera(); n = b = 0
for o in json.load(open(f"{S}/orphan-torrents-2026-10-22.json")):
    if o["cat"] != "radarr": continue
    info = p.qbt(f"torrents/info?hashes={o['hash']}")
    if not info: continue
    cp = info[0]["content_path"]
    files = [cp] if os.path.isfile(cp) else [os.path.join(d, f) for d, _, fs in os.walk(cp) for f in fs]
    if not files or any(os.stat(f).st_nlink > 1 for f in files):
        print("skipped (now linked)", o["name"]); continue
    p.qbt("torrents/delete", {"hashes": o["hash"], "deleteFiles": "true"}); n += 1; b += o["size"]
    print("removed", o["name"], ls.gb(o["size"]), flush=True)
print("DONE", n, ls.gb(b))
