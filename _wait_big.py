#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Wait for the big grab to finish, then report."""
import json
import time
import urllib.request

BASE = "http://127.0.0.1:8765"
t0 = time.time()
while time.time() - t0 < 3600:
    r = urllib.request.urlopen(BASE + "/jobs", timeout=30)
    j = json.loads(r.read().decode("utf-8"))
    grabs = [it for it in j["jobs"] if it.get("kind") == "grab"
             and it.get("title") == "圣墟"]
    if not grabs:
        time.sleep(20)
        continue
    g = grabs[0]
    if g["status"] in ("done", "error", "cancelled"):
        print("FINAL:", json.dumps({k: g.get(k) for k in
                                    ("status", "message", "progress",
                                     "chars", "skipped", "book_id")},
                                   ensure_ascii=False))
        print("elapsed:", round(time.time() - t0), "s")
        bid = g.get("book_id")
        if bid and g["status"] == "done":
            import io
            c = io.open(rf"D:\Projects\txt2ebook\library\{bid}\content.txt",
                        encoding="utf-8").read()
            import re
            n = sum(1 for l in c.splitlines()
                    if len(l) < 30 and re.match(r"^第[0-9零一二三四五六七八九十百千两万]+章", l))
            print("章节标题数:", n, "| 总字符:", len(c),
                  "| 文件MB:", round(len(c.encode("utf-8")) / 1048576, 2))
        break
    time.sleep(20)
else:
    print("TIMEOUT waiting")
