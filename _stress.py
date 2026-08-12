#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Big-book stress test: grab 200 chapters from a static site."""
import json
import time
import urllib.request

BASE = "http://127.0.0.1:8765"


def req(method, path, body=None, timeout=180):
    data = json.dumps(body).encode() if body is not None else None
    h = {"Content-Type": "application/json"} if body is not None else {}
    r = urllib.request.Request(BASE + path, data=data, headers=h, method=method)
    with urllib.request.urlopen(r, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", "replace"))


j = req("POST", "/grab", {
    "url": "http://www.biquge5200.cc/52_52542/",
    "mode": "auto", "clean_ads": "1", "render": "auto",
    "max_chapters": 200})
t0 = time.time()
last = 0
while True:
    p = req("GET", "/progress/" + j["job_id"], timeout=60)
    if p.get("status") in ("done", "error", "cancelled"):
        break
    prog = p.get("progress") or 0
    if prog >= last + 20:
        last = prog
        print(f"  {prog}% done_parts={p.get('done_parts')}/{p.get('total_parts')} "
              f"elapsed={time.time()-t0:.0f}s msg={p.get('message')[:30]}")
    time.sleep(3)
print("最终:", json.dumps({k: p.get(k) for k in
                           ("status", "message", "chars", "skipped")},
                          ensure_ascii=False))
print("总耗时: %.0f 秒" % (time.time() - t0))
bid = p.get("book_id")
if bid and p.get("status") == "done":
    import io
    c = io.open(rf"D:\Projects\txt2ebook\library\{bid}\content.txt",
                encoding="utf-8").read()
    print("文件大小: %.1f MB, 字符: %d" % (len(c.encode("utf-8")) / 1048576, len(c)))
    # count chapter headings
    import re
    n = sum(1 for l in c.splitlines()
            if len(l) < 30 and re.match(r"^第[0-9零一二三四五六七八九十百千两万]+章", l))
    print("章节标题数:", n)
