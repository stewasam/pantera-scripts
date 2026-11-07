"""Self-check for library-audit.orphan_folders: extras skipped, unimported seasons found."""
import importlib.util, os, tempfile
spec = importlib.util.spec_from_file_location("la", "/home/sam/docker/media-stack/scripts/library-audit.py")
la = importlib.util.module_from_spec(spec); spec.loader.exec_module(la)
root = tempfile.mkdtemp(); lib = tempfile.mkdtemp(dir=root)
dl = os.path.join(root, "downloads"); os.makedirs(dl)
def video(path, link=False):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f: f.truncate(200_000_000)   # sparse: big enough to count, no real space
    if link: os.link(path, os.path.join(lib, os.path.basename(path)))
video(f"{dl}/Film.2001/Film.2001.mkv", link=True)              # film in library
video(f"{dl}/Film.2001/Extras/Featurette.mkv")                  # its extras: skip
video(f"{dl}/Show.Complete/Show.S01/Show.S01E01.mkv", link=True)
video(f"{dl}/Show.Complete/Show.S02/Show.S02E01.mkv")           # never imported: must be found
video(f"{dl}/Show.Complete/Show.Specials/Doc.mkv")              # bonus: found, later judged as extra
video(f"{dl}/Lone.Movie.mkv")                                   # loose file, not in library: found
la.DOWNLOADS = dl
found = la.orphan_folders(set())
flat = {os.path.relpath(os.path.join(d, f), dl) for top in found.values() for d, fs in top.items() for f in fs}
assert "Show.Complete/Show.S02/Show.S02E01.mkv" in flat, flat
assert "Lone.Movie.mkv" in flat, flat
assert not any("Featurette" in x or x.endswith("Film.2001.mkv") or "S01E01" in x for x in flat), flat
assert la.EXTRAS.search("Show.Specials") and not la.EXTRAS.search("Show.S02")
print("ok:", sorted(flat))
