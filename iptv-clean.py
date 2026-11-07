#!/usr/bin/env python3
"""Nightly: iptv-org's full channel list (index.m3u), cleaned, for Threadfin.

Keeps channels from named https sources that answer with a live playlist. Drops plain http,
bare-IP servers (the unlicensed cable rebroadcasts), geo-blocked ones, blocklisted hosts and dead links.
Writes config/threadfin/playlists/us-clean.m3u (Threadfin sees /home/threadfin/conf/playlists/us-clean.m3u).
"""
import concurrent.futures as cf
import ipaddress
import os
import re
import sys
import urllib.request
from urllib.parse import urlparse

SOURCE = "https://iptv-org.github.io/iptv/index.m3u"
HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "..", "config", "threadfin", "playlists", "us-clean.m3u")
SETTINGS = os.path.join(HERE, "iptv-settings.txt")     # which countries to keep, which categories to drop (edit freely)
BLOCKLIST = os.path.join(HERE, "iptv-blocklist.txt")   # one host per line: sources found rebroadcasting paid channels
UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36"
MIN_KEEP = 50   # fewer than this means the source or the network broke: keep yesterday's list


def entries(text: str):
    """(info lines, url) per channel: the #EXTINF line plus any option lines before its URL."""
    info = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#EXTM3U"):
            continue
        if line.startswith("#"):
            if line.startswith("#EXTINF"):
                info = [line]
            elif info:
                info.append(line)
        elif info:
            yield info, line
            info = []


def settings(text: str):
    """("countries: us uk ca", "skip: Religious Shop") -> ({"us","uk","ca"}, {"religious","shop"}); empty = keep all."""
    want, skip = set(), set()
    for line in text.splitlines():
        key, _, rest = line.partition(":")
        words = {w.lower() for w in rest.replace(",", " ").split()}
        if key.strip().lower() == "countries": want |= words
        elif key.strip().lower() == "skip": skip |= words
    return want, skip


def wanted(info: list, countries: set, skip: set) -> bool:
    """The channel's country (tvg-id "Name.us@SD") is one to keep, and none of its categories is skipped."""
    m = re.search(r'tvg-id="[^"]*\.([a-z]{2})(?:@[^"]*)?"', info[0])
    if countries and (not m or m.group(1) not in countries):
        return False
    g = re.search(r'group-title="([^"]*)"', info[0])
    return not (skip and g and {x.strip().lower() for x in g.group(1).split(";")} & skip)


def allowed(info: list, url: str, blocked: set) -> bool:
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if parsed.scheme != "https" or not host or host in blocked:
        return False
    try:
        ipaddress.ip_address(host)
        return False   # a bare address, not a broadcaster's or a streaming service's
    except ValueError:
        pass
    return "[Geo-blocked]" not in info[0]


def live(url: str) -> bool:
    try:
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=10) as resp:
            head = resp.read(2048)
        return b"#EXTM3U" in head or head[:1] == b"\x47"   # an HLS playlist, or MPEG-TS
    except Exception:
        return False


def main():
    blocked = set()
    if os.path.exists(BLOCKLIST):
        blocked = {h.strip().lower() for h in open(BLOCKLIST) if h.strip() and not h.startswith("#")}
    req = urllib.request.Request(SOURCE, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=60) as resp:
        text = resp.read().decode("utf-8", "replace")
    found = list(entries(text))
    countries, skip = settings(open(SETTINGS).read()) if os.path.exists(SETTINGS) else (set(), set())
    candidates = [(i, u) for i, u in found if allowed(i, u, blocked) and wanted(i, countries, skip)]
    with cf.ThreadPoolExecutor(24) as pool:
        ok = list(pool.map(lambda e: live(e[1]), candidates))
    kept = [e for e, good in zip(candidates, ok) if good]
    print(f"{len(found)} listed, {len(candidates)} allowed, {len(kept)} live", flush=True)
    if len(kept) < MIN_KEEP:
        print("too few live: keeping the previous list", flush=True)
        sys.exit(1)
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    tmp = OUT + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write("#EXTM3U\n")
        for info, url in kept:
            f.write("\n".join(info) + "\n" + url + "\n")
    os.replace(tmp, OUT)


def selftest():
    sample = ("#EXTM3U\n#EXTINF:-1 tvg-id=\"A.us\",Good\nhttps://cdn.example.com/a.m3u8\n"
              "#EXTINF:-1,Plain\nhttp://cdn.example.com/b.m3u8\n"
              "#EXTINF:-1,Bare\nhttps://23.237.104.106:8080/c.m3u8\n"
              "#EXTINF:-1,Geo [Geo-blocked]\nhttps://cdn.example.com/d.m3u8\n"
              "#EXTINF:-1,Blocked\nhttps://bad.example.net/e.m3u8\n"
              "#EXTINF:-1,Opts\n#EXTVLCOPT:http-referrer=https://x\nhttps://cdn.example.com/f.m3u8\n")
    got = [(i[0].split(",")[-1], len(i)) for i, u in entries(sample) if allowed(i, u, {"bad.example.net"})]
    assert got == [("Good", 1), ("Opts", 2)], got
    assert settings("# x\ncountries: us, UK ca\nskip: Religious Shop") == ({"us", "uk", "ca"}, {"religious", "shop"})
    us = ['#EXTINF:-1 tvg-id="CBSNews.us@SD" group-title="News",CBS News']
    hymns = ['#EXTINF:-1 tvg-id="Hymns.us" group-title="Music;Religious",Hymns']
    de = ['#EXTINF:-1 tvg-id="ZDF.de@HD" group-title="General",ZDF']
    nameless = ['#EXTINF:-1 tvg-id="" group-title="News",Mystery']
    assert wanted(us, {"us"}, set()) and not wanted(de, {"us"}, set()) and wanted(de, set(), set())
    assert not wanted(hymns, set(), {"religious"}) and wanted(us, set(), {"religious"})
    assert not wanted(nameless, {"us"}, set()) and wanted(nameless, set(), set())
    print("selftest ok")


if __name__ == "__main__":
    selftest() if "--selftest" in sys.argv else main()
