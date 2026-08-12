#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Full probe: home -> book TOC -> chapter -> content completeness."""
import re
import sys
import urllib.parse
import urllib.request

sys.path.insert(0, r'D:\Projects\txt2ebook')
import server

UA = server.GRAB_UA
CH_RE = re.compile(r'第\s*[0-9零一二三四五六七八九十百千两万]+\s*[章节回卷]')


def fetch(url, timeout=25):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def chapter_links(text, base):
    return server._extract_chapter_links(text, base)


def probe(name, home):
    print(f"===== {name} ({home}) =====")
    try:
        raw = fetch(home)
        text = server.decode_web_bytes(raw)
    except Exception as e:
        print("  首页失败:", type(e).__name__, e)
        return
    print(f"  首页 {len(raw)}B, JS壳: {server._looks_js_loaded(text)}")
    # find a book link
    links = re.findall(r'<a[^>]*href\s*=\s*["\']([^"\']+)["\'][^>]*>([^<]{2,40})</a>', text)
    books = []
    seen = set()
    for h, tx in links:
        h2, tx2 = h.strip(), tx.strip()
        if re.search(r'[\u4e00-\u9fff]', tx2) and h2 not in seen \
                and re.match(r'^/[^/\s]+/[^/\s]+/?$', h2) \
                and not re.search(r'\.(html?|php|js|css|png|jpg)$', h2, re.I):
            seen.add(h2)
            books.append((h2, tx2))
    print("  书链接:", books[:3])
    if not books:
        print("  ⚠️ 首页无书链接")
        return
    b_url, b_tx = books[0]
    try:
        raw2 = fetch(urllib.parse.urljoin(home, b_url))
        t2 = server.decode_web_bytes(raw2)
    except Exception as e:
        print("  目录失败:", type(e).__name__, e)
        return
    chs = chapter_links(t2, urllib.parse.urljoin(home, b_url))
    uniq = len(set(h for _, h in chs))
    print(f"  目录 {len(raw2)}B, 章节链接 {len(chs)} (去重 {uniq}) [{b_tx[:14]}]")
    if not chs:
        print("  ⚠️ 无章节链接")
        return
    # chapter page
    ch_url = chs[0][1]
    try:
        raw3 = fetch(ch_url)
        t3 = server.decode_web_bytes(raw3)
    except Exception as e:
        print("  章节失败:", type(e).__name__, e)
        return
    print(f"  章节页 {len(raw3)}B", end="")
    if len(raw3) < 3000:
        print(" ⚠️ 疑似JS壳,试渲染...", end="")
        try:
            rhtml = server._render_page(ch_url)
            t3 = server.decode_web_bytes(rhtml.encode("utf-8", "replace"))
            print(f" 渲染后 {len(rhtml)}B", end="")
        except Exception as e:
            print(f" 渲染失败 {e}", end="")
    print()
    # extract content via the generic path
    heading, body = server._extract_chapter(t3, None, ch_url)
    if body:
        cjk = sum(1 for c in body if "\u4e00" <= c <= "\u9fff")
        print(f"  提取: {len(body)}字符({cjk}汉字) | heading: {str(heading)[:24]}")
        print(f"  末行: {body.splitlines()[-1][:36]!r}")
    else:
        print("  ⚠️ 提取为空")
    # paging
    pages = server._chapter_pages(ch_url, t3)
    if pages:
        print(f"  分页: {len(pages)+1} 页, 已支持跟随")
    print()


for name, home in (
    ("顶点 biqudu.com", "https://www.biqudu.com/"),
    ("新笔趣阁 xinbiquge.com", "http://www.xinbiquge.com/"),
    ("鲲弩 kunnu.com", "http://www.kunnu.com/"),
    ("燃文 ranwen.la", "http://www.ranwen.la/"),
    ("笔趣阁 biqege.com", "http://www.biqege.com/"),
):
    try:
        probe(name, home)
    except Exception as e:
        print(f"===== {name} =====\n  异常: {type(e).__name__} {str(e)[:100]}\n")
