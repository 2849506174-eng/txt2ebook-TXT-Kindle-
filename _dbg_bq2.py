#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import re
import sys
import urllib.request

sys.path.insert(0, r'D:\Projects\txt2ebook')
import server

url = "https://www.biqege.com/book/heduh/hkbjhuni.html"
rhtml = server._render_page(url)
print("渲染 len:", len(rhtml))
text = server.decode_web_bytes(rhtml.encode("utf-8", "replace"))
# div#txt after render
ex = server._SelectorExtractor([{"tag": "div", "id": "txt"}])
ex.feed(text)
ex.close()
out = ex.result() or ""
print("div#txt 提取:", len(out), "字符")
print("开头:", repr(out[:80]))
print("末行:", repr(out.splitlines()[-1][:40]) if out.splitlines() else "")
# paging
pages = server._chapter_pages(url, text)
print("分页:", pages)
