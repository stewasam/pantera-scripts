"""Self-check for media-guard.verdict: disguised programs removed, real media left alone."""
import importlib.util, os, tempfile
spec = importlib.util.spec_from_file_location("mg", "/home/sam/docker/media-stack/scripts/media-guard.py")
mg = importlib.util.module_from_spec(spec); spec.loader.exec_module(mg)
d = tempfile.mkdtemp()
def make(name, head):
    p = os.path.join(d, name); open(p, "wb").write(head + b"\0" * 64); return p
assert mg.verdict(make("Episode.mkv", b"\x1aE\xdf\xa3")) is None                     # real Matroska
assert mg.verdict(make("Song.flac", b"fLaC")) is None
assert mg.verdict(make("Episode.S01E03.mkv", b"MZ\x90\x00"))[0] == "remove"           # program dressed as a video
assert mg.verdict(make("setup.bat", b"@echo off"))[0] == "remove"
assert mg.verdict(make("Codec.mp4", b"\x7fELF"))[0] == "remove"
assert mg.verdict(make("Extras.zip", b"PK\x03\x04"))[0] == "report"
assert mg.verdict(make("readme.txt", b"#!notes")) is None
print("ok")

# owning_torrent: magnets with no path yet must match nothing; the closest folder wins
T = [{"name": "magnet", "content_path": "", "save_path": "/media/downloads"},
     {"name": "root", "content_path": "/media/downloads", "save_path": "/media/downloads"},
     {"name": "Film", "content_path": "/media/downloads/Film.2015", "save_path": "/media/downloads"},
     {"name": "Film2", "content_path": "/media/downloads/Film.2015.Extras", "save_path": "/media/downloads"}]
assert mg.owning_torrent("/media/downloads/Film.2015/ad.url", T)["name"] == "Film"
assert mg.owning_torrent("/media/downloads/Other/x.exe", T) is None
assert mg.verdict(make("Visit.url", b"[InternetShortcut]"))[1].endswith(".url file")
print("ok: torrent matching")
