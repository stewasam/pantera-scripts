#!/usr/bin/env python3
"""Nightly, after iptv-clean.py: a real TV guide for the cleaned channel list, for Threadfin.

Pluto TV, Samsung TV Plus and Roku channels are matched exactly by the id in their stream link (jmp2.uk/plu-<id>,
/stvp-<id>, /rok-<id>) to i.mjh.nz's guides; every other channel by its name against epg.pw's US guide and Plex's.
Writes config/threadfin/playlists/guide.xml: one <channel> per matched channel, its id the channel's key (KEY below),
and its programmes from 2 h ago to 36 h ahead. Channels with no match keep Threadfin's 1-hour dummy blocks.
"""
import gzip
import os
import re
import sys
import tempfile
import time
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from xml.sax.saxutils import escape, quoteattr

HERE = os.path.dirname(os.path.abspath(__file__))
PLAYLIST = os.path.join(HERE, "..", "config", "threadfin", "playlists", "us-clean.m3u")
OUT = os.path.join(HERE, "..", "config", "threadfin", "playlists", "guide.xml")
UA = "pantera-iptv-guide/1.0 (personal homelab)"
BY_ID = {"plu": "https://i.mjh.nz/PlutoTV/all.xml.gz", "stvp": "https://i.mjh.nz/SamsungTVPlus/all.xml.gz",
         "rok": "https://i.mjh.nz/Roku/all.xml.gz"}
BY_NAME = ["https://epg.pw/xmltv/epg_US.xml", "https://i.mjh.nz/Plex/all.xml.gz"]
PAST, AHEAD = 2 * 3600, 36 * 3600
LINK = re.compile(r"jmp2\.uk/(plu|stvp|rok)-([A-Za-z0-9]+)")
ATTR = re.compile(r'([\w-]+)="([^"]*)"')


def key(tvg_id: str, name: str) -> str:
    """How Threadfin's mapping (iptv-apply.py) and this guide name a channel: its tvg-id, else its name."""
    return tvg_id or "name:" + name


def norm(name: str) -> str:
    """'CBS News (1080p) [Not 24/7]' / 'CBS News HD' -> 'cbs news': for matching names across guides."""
    n = re.sub(r"\([^)]*\)|\[[^\]]*\]", " ", name.lower())
    n = re.sub(r"\b(hd|uhd|fhd|sd|4k|east|tv)\b", " ", n)
    return " ".join(re.sub(r"[^a-z0-9]+", " ", n).split())


def channels(text: str):
    """The playlist's channels: [(key, name, link source or None, link id or None)]."""
    out, info = [], None
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("#EXTINF"):
            info = line
        elif line and not line.startswith("#") and info:
            a = dict(ATTR.findall(info))
            name = info.rsplit(",", 1)[-1].strip()
            m = LINK.search(line)
            out.append((key(a.get("tvg-id", ""), name), name, m.group(1) if m else None, m.group(2) if m else None))
            info = None
    return out


def when(stamp: str):
    """XMLTV '20261028183000 -0400' -> epoch seconds; None if unreadable."""
    try:
        return int(datetime.strptime(stamp.strip(), "%Y%m%d%H%M%S %z").timestamp())
    except ValueError:
        try:
            return int(datetime.strptime(stamp.strip()[:14], "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc).timestamp())
        except ValueError:
            return None


def stream(url: str):
    """The guide as a stream, gunzipped when it is .gz (read piece by piece: epg.pw's US guide is ~160 MB)."""
    r = urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": UA}), timeout=180)
    return gzip.GzipFile(fileobj=r) if url.endswith(".gz") else r


def merge(src, wanted, out, now, by_name=False, taken=None):
    """Copy one guide's programmes for the wanted channels into [out] (re-keyed to our keys).
    wanted: {their channel id: our key} (by id), or {normalised name: our key} (by name, first match wins).
    Returns {our key: (name, icon)} for the channels that matched."""
    found, ids = {}, ({} if by_name else dict(wanted))
    taken = taken if taken is not None else set()
    for _, el in ET.iterparse(src):
        if el.tag == "channel":
            cid = el.get("id", "")
            if by_name:
                for dn in el.iter("display-name"):
                    ours = wanted.get(norm(dn.text or ""))
                    if ours and ours not in taken:
                        ids[cid] = ours
                        taken.add(ours)
                        break
            if cid in ids:
                icon = el.find("icon")
                found[ids[cid]] = (el.findtext("display-name") or "", icon.get("src", "") if icon is not None else "")
            el.clear()
        elif el.tag == "programme":
            ours = ids.get(el.get("channel", ""))
            start, stop = when(el.get("start", "")), when(el.get("stop", ""))
            if ours and start and stop and start < stop and stop >= now - PAST and start <= now + AHEAD:
                el.set("channel", ours)
                for t in el.findall("title"):
                    t.attrib.pop("lang", None)   # epg.pw labels English titles "zh"
                out.write(ET.tostring(el, encoding="unicode").strip() + "\n")
            el.clear()
    return found


def main():
    now = time.time()
    chans = channels(open(PLAYLIST, encoding="utf-8").read())
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    found, taken = {}, set()
    with tempfile.TemporaryFile("w+", encoding="utf-8") as progs:
        for prefix, url in BY_ID.items():
            wanted = {cid: k for k, _, p, cid in chans if p == prefix}
            if not wanted:
                continue
            try:
                got = merge(stream(url), wanted, progs, now)
            except Exception as e:
                print(f"{url}: {type(e).__name__}", flush=True)
                continue
            found.update(got); taken |= set(got)
            print(f"{prefix}: {len(got)} of {len(wanted)} matched", flush=True)
        for url in BY_NAME:
            wanted = {}
            for k, name, p, _ in chans:
                if k not in taken and p is None and norm(name):
                    wanted.setdefault(norm(name), k)
            try:
                got = merge(stream(url), wanted, progs, now, by_name=True, taken=taken)
            except Exception as e:
                print(f"{url}: {type(e).__name__}", flush=True)
                continue
            found.update(got)
            print(f"{url.split('/')[2]} by name: {len(got)} matched", flush=True)
        if len(found) < 50:
            sys.exit("too few channels matched: keeping the previous guide")
        tmp = OUT + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write('<?xml version="1.0" encoding="UTF-8"?>\n<tv generator-info-name="pantera iptv-guide">\n')
            names = {k: n for k, n, _, _ in chans}
            for k in found:
                f.write(f"<channel id={quoteattr(k)}><display-name>{escape(names.get(k, found[k][0]))}</display-name></channel>\n")
            progs.seek(0)
            for line in progs:
                f.write(line)
            f.write("</tv>\n")
        os.replace(tmp, OUT)
    print(f"guide: {len(found)} of {len(chans)} channels", flush=True)


def selftest():
    import io
    assert norm("CBS News (1080p) [Not 24/7]") == norm("CBS News HD") == "cbs news"
    m3u = ('#EXTM3U\n#EXTINF:-1 tvg-id="X.us@SD",Pluto One\nhttps://jmp2.uk/plu-abc123.m3u8\n'
           '#EXTINF:-1 tvg-id="CBSNews.us",CBS News (720p)\nhttps://cdn.example.com/cbs.m3u8\n'
           '#EXTINF:-1,No Id\nhttps://cdn.example.com/x.m3u8\n')
    c = channels(m3u)
    assert c == [("X.us@SD", "Pluto One", "plu", "abc123"), ("CBSNews.us", "CBS News (720p)", None, None), ("name:No Id", "No Id", None, None)]
    now = when("20261028183000 +0000")
    g = ('<tv><channel id="abc123"><display-name>Pluto One</display-name></channel>'
         '<programme start="20261028180000 +0000" stop="20261028190000 +0000" channel="abc123"><title lang="zh">Now</title></programme>'
         '<programme start="20261101180000 +0000" stop="20261101190000 +0000" channel="abc123"><title>Too far</title></programme>'
         '<programme start="20261028180000 +0000" stop="20261028190000 +0000" channel="zzz"><title>Not ours</title></programme></tv>')
    out = io.StringIO()
    assert set(merge(io.BytesIO(g.encode()), {"abc123": "X.us@SD"}, out, now)) == {"X.us@SD"}
    assert 'channel="X.us@SD"' in out.getvalue() and "Too far" not in out.getvalue() and "Not ours" not in out.getvalue()
    assert 'lang=' not in out.getvalue()
    g2 = ('<tv><channel id="9"><display-name>CBS News HD</display-name></channel>'
          '<programme start="20261028180000 +0000" stop="20261028190000 +0000" channel="9"><title>Evening</title></programme></tv>')
    out2 = io.StringIO()
    assert set(merge(io.BytesIO(g2.encode()), {"cbs news": "CBSNews.us"}, out2, now, by_name=True)) == {"CBSNews.us"}
    assert "Evening" in out2.getvalue()
    print("selftest ok")


if __name__ == "__main__":
    selftest() if "--selftest" in sys.argv else main()
