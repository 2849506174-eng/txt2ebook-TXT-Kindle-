#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Quick functional regression on the sandbox-free production instance."""
import io
import json
import time
import urllib.request

BASE = "http://127.0.0.1:8765"
BOUNDARY = "----regr1234"


def multipart(fields, files):
    buf = io.BytesIO()
    for k, v in fields.items():
        buf.write(f"--{BOUNDARY}\r\n".encode())
        buf.write(f'Content-Disposition: form-data; name="{k}"\r\n\r\n'.encode())
        buf.write(v.encode("utf-8"))
        buf.write(b"\r\n")
    for k, (fname, content) in files.items():
        buf.write(f"--{BOUNDARY}\r\n".encode())
        buf.write(f'Content-Disposition: form-data; name="{k}"; filename="{fname}"\r\n'.encode())
        buf.write(b"Content-Type: text/plain\r\n\r\n")
        buf.write(content)
        buf.write(b"\r\n")
    buf.write(f"--{BOUNDARY}--\r\n".encode())
    return buf.getvalue()


def post_mp(path, fields, files, timeout=120):
    data = multipart(fields, files)
    req = urllib.request.Request(BASE + path, data=data, method="POST", headers={
        "Content-Type": f"multipart/form-data; boundary={BOUNDARY}"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def req(method, path, body=None, timeout=90):
    data = json.dumps(body).encode() if body is not None else None
    h = {"Content-Type": "application/json"} if body is not None else {}
    r = urllib.request.Request(BASE + path, data=data, headers=h, method=method)
    with urllib.request.urlopen(r, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", "replace"))


def poll(job_id, seconds=300):
    end = time.time() + seconds
    while time.time() < end:
        j = req("GET", f"/progress/{job_id}")
        if j.get("status") in ("done", "error", "cancelled"):
            return j
        time.sleep(1)
    return None


print("1. health:", req("GET", "/health")["ok"])
print("2. sources:", len(req("GET", "/sources")["sources"]), "个书源")
print("3. config:", req("GET", "/config")["ok"])

txt = ("\u4e66\u540d\uff1a\u56de\u5f52\u6d4b\u8bd5\n\u4f5c\u8005\uff1a\u6d4b\u8bd5\n\n"
       "\u7b2c\u4e00\u7ae0 \u5f00\u59cb\n\u6b63\u6587\u4e00\u3002\u8bf7\u6536\u85cf\u672c\u7ad9\u3002\n\n"
       "\u7b2c\u4e8c\u7ae0 \u7ed3\u675f\n\u6b63\u6587\u4e8c\u3002\n").encode("utf-8")

print("4. preview:", post_mp("/preview", {"clean_ads": "1"}, {"file": ("t.txt", txt)})["ok"])
print("5. merge:", post_mp("/merge", {}, {"file": ("t.txt", txt)})["ok"])
j = post_mp("/convert", {"format": "txt", "clean_ads": "1"}, {"file": ("t.txt", txt)})
res = poll(j["job_id"])
print("6. convert txt->txt:", res["status"], res.get("message", "")[:30])
j = post_mp("/read/open", {}, {"file": ("t.txt", txt)})
print("7. read/open:", j["ok"], "|", j.get("title"))
sid = j.get("sid")
if sid:
    ch = req("GET", f"/read/chapter?s={sid}&n=0")
    print("8. read/chapter:", ch["ok"], "|", ch.get("chapter_title"))
print("9. jobs:", len(req("GET", "/jobs")["jobs"]), "条记录")
req("POST", "/read/clear_recent")
print("10. clear_recent ok")
