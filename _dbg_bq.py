#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import re
import sys
import urllib.request

sys.path.insert(0, r'D:\Projects\txt2ebook')
import server

UA = server.GRAB_UA

def fetch(url, timeout=25):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()

# find a real chapter URL from biqege book TOC
toc = server.decode_web_bytes(fetch("http://www.biqege.com/book/heduh/"))
links = server._extract_chapter_links(toc, "http://www.biqege.com/book/heduh/")
print("章节:", links[0][0][:20], links[0][1])
url = links[0][1]
text = server.decode_web_bytes(fetch(url))
print("章节页:", len(text))
# all divs with id/class
for m2 in re.finditer(r'<div[^>]*\b(id|class)=["\']([^"\']+)["\'][^>]*>(.{0,100})', text, re.S):
    nm = m2.group(2)
    seg = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", m2.group(3)))
    if re.search(r'content|nr|read|txt|text|article|book|chapter|main|body|show|neirong', nm, re.I):
        print(f"  div {m2.group(1)}={nm!r}: {seg[:55]!r}")
# check a few common selectors
for sel in ([{"tag": "div", "class": "neirong"}], [{"tag": "div", "id": "booktxt"}],
            [{"tag": "div", "class": "showtxt"}], [{"tag": "div", "id": "content1"}],
            [{"tag": "div", "class": "content1"}], [{"tag": "p"}]):
    ex = server._SelectorExtractor(sel)
    ex.feed(text)
    ex.close()
    out = ex.result()
    if out:
        print(f"  sel {sel} -> {len(out)}字符: {out[:40]!r}")
