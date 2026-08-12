#!/usr/bin/env python3
"""
txt2ebook - Local web app to convert TXT files to MOBI / AZW / AZW3 / EPUB.

Features:
- Async job model with live progress (frontend polls /progress/<job_id>).
- Auto-splits large TXT files by chapter into multiple parts.
- Converts each part with Calibre; packages multiple parts into a ZIP.
- No external Python dependencies (stdlib only). Conversion via Calibre.
"""
import gzip
import hashlib
import html as html_mod
import json
import mimetypes
import os
import re
import shutil
import socket
import ssl
import subprocess
import sys
import threading
import time
import urllib.request
import uuid
import zipfile
from html.parser import HTMLParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import quote, unquote, urljoin, urlparse

BASE = Path(__file__).resolve().parent
OUTPUT = BASE / "output"
OUTPUT.mkdir(parents=True, exist_ok=True)

# User config (persisted in config.json): currently the output directory.
CONFIG_FILE = BASE / "config.json"
CONFIG = {}

# Custom background images live on the server so any number can be kept and
# switched between (browser localStorage was too small for more than a few).
BG_DIR = BASE / "backgrounds"
BG_META_FILE = BASE / "backgrounds.json"
BG_DIR.mkdir(parents=True, exist_ok=True)
# Accepted background image formats (.gif is animated and used as-is).
BG_EXTS = (".jpg", ".jpeg", ".png", ".webp", ".gif")
# Background videos (play in a loop, optional audio with a mute toggle).
BG_VIDEO_EXTS = (".mp4", ".webm")


def _load_bg_meta():
    try:
        data = json.loads(BG_META_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except Exception:
        return []


def _save_bg_meta(meta):
    try:
        BG_META_FILE.write_text(json.dumps(meta, ensure_ascii=False),
                                encoding="utf-8")
    except Exception:
        pass


def load_config():
    global CONFIG
    try:
        CONFIG = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    except Exception:
        CONFIG = {}
    if not isinstance(CONFIG, dict):
        CONFIG = {}


def save_config():
    try:
        CONFIG_FILE.write_text(json.dumps(CONFIG, ensure_ascii=False),
                               encoding="utf-8")
    except Exception:
        pass


def output_base():
    """Effective base directory for job outputs (custom or default)."""
    p = CONFIG.get("output_dir")
    if p:
        try:
            d = Path(p).expanduser()
            d.mkdir(parents=True, exist_ok=True)
            return d
        except Exception:
            pass
    return OUTPUT


def output_dirs():
    """All bases that may hold job dirs (current custom base + default),
    so old jobs stay reachable after the user switches output directory."""
    seen = {}
    for d in (output_base(), OUTPUT):
        try:
            seen[str(d.resolve())] = d
        except OSError:
            pass
    return list(seen.values())


# ---- reading sessions (in-memory, ephemeral; dirs swept like job dirs) ----
READ_SESSIONS = {}
READ_TTL = 24 * 3600
READ_MAX_TEXT = 30 * 1024 * 1024
READ_LIB = BASE / "library"           # persistent per-book cleaned text
READ_LIB_MAX = 30
READ_STATE_FILE = BASE / "readstate.json"  # progress + bookmarks


def _prune_read_sessions():
    now = time.time()
    for sid in [s for s, v in list(READ_SESSIONS.items())
                if now - v["created"] > READ_TTL]:
        READ_SESSIONS.pop(sid, None)

def _load_read_state():
    try:
        data = json.loads(READ_STATE_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save_read_state(state):
    try:
        READ_STATE_FILE.write_text(json.dumps(state, ensure_ascii=False),
                                   encoding="utf-8")
    except Exception:
        pass


def _split_chapters(text):
    """Split text into chapters by heading lines; preamble (书名/作者 etc.)
    is merged into the first real chapter."""
    rx = CHAPTER_RE
    out = []
    cur = []
    cur_title = None
    cur_has = False
    for ln in text.splitlines(keepends=True):
        if rx.match(ln):
            if cur_has:
                out.append({"title": cur_title, "text": "".join(cur)})
            cur_title = ln.strip()
            cur = [ln]
            cur_has = True
        else:
            cur.append(ln)
    if cur and cur_has:
        out.append({"title": cur_title, "text": "".join(cur)})
    return out


def _open_read_session(title, raw_text, book_id):
    """Open (or reuse) a reading session backed by the local library, so books
    can be reopened later and progress/bookmarks remembered. ``raw_text`` may
    be None when reopening from the library."""
    _prune_read_sessions()
    state = _load_read_state()
    entry = state.get(book_id) or {}
    lib = READ_LIB / book_id
    content = lib / "content.txt"
    if content.is_file():
        text = content.read_text(encoding="utf-8")
    else:
        text, _removed = _clean_ad_lines(raw_text or "")
        if len(text.encode("utf-8")) > READ_MAX_TEXT:
            raise ValueError("小说过大(净化后超过 30MB),建议用转换功能拆分后再阅读")
        if _count_chapters(text) < 2:
            text = _insert_sections(text)
        lib.mkdir(parents=True, exist_ok=True)
        content.write_text(text, encoding="utf-8")
        try:
            (lib / "meta.json").write_text(json.dumps(
                {"title": title or "未命名", "opened": time.time()},
                ensure_ascii=False), encoding="utf-8")
        except Exception:
            pass
    chapters = _split_chapters(text)
    if not chapters:
        chapters = [{"title": entry.get("title") or title or "正文", "text": text}]
    sid = uuid.uuid4().hex[:12]
    READ_SESSIONS[sid] = {"created": time.time(),
                          "title": entry.get("title") or title or "未命名",
                          "chapters": chapters, "book_id": book_id}
    entry["title"] = READ_SESSIONS[sid]["title"]
    entry["opened"] = time.time()
    state[book_id] = entry
    _save_read_state(state)
    _prune_read_lib()
    return sid, entry.get("chapter") or 0


def _prune_read_lib():
    """Keep the local reading library bounded (drop oldest books)."""
    try:
        dirs = [d for d in READ_LIB.iterdir() if d.is_dir()]
        if len(dirs) <= READ_LIB_MAX:
            return
        aged = []
        for d in dirs:
            try:
                meta = json.loads((d / "meta.json").read_text(encoding="utf-8"))
                aged.append((meta.get("opened", 0), d.name))
            except Exception:
                aged.append((0, d.name))
        aged.sort()
        for _opened, name in aged[:len(dirs) - READ_LIB_MAX]:
            shutil.rmtree(READ_LIB / name, ignore_errors=True)
            state = _load_read_state()
            state.pop(name, None)
            _save_read_state(state)
    except Exception:
        pass
FORMATS = {"mobi": "mobi", "azw": "azw", "azw3": "azw3", "epub": "epub", "kfx": "kfx", "txt": "txt"}
# Calibre has no native azw/kfx output: azw is a MOBI rename, kfx is produced
# by Amazon Kindle Previewer from an intermediate MOBI. txt is native.
CONVERT_EXT = {"mobi": "mobi", "azw": "mobi", "azw3": "azw3", "epub": "epub", "kfx": "mobi", "txt": "txt"}
# Accepted custom cover image extensions (passed straight to ebook-convert).
COVER_EXTS = {".jpg", ".jpeg", ".png", ".webp"}
# Accepted input formats: TXT for the full text pipeline, ebook formats for
# direct format conversion (single file, no merge/split).
INPUT_EXTS = {".txt", ".epub", ".mobi", ".azw", ".azw3"}
# Archives are extracted on upload; the book files inside are used instead.
ARCHIVE_EXTS = {".zip", ".rar", ".7z"}
MAX_ARCHIVE_FILES = 1000
MAX_EXTRACT_BYTES = 2 * 1024 * 1024 * 1024


class ArchiveError(Exception):
    pass


def find_7z():
    """Locate a free archive tool for RAR / 7Z (ZIP uses the stdlib and needs
    nothing). Prefers 7-Zip, falls back to RARLAB's free unrar. Never uses
    paid/shareware tools like WinRAR or WinZip."""
    for exe in ("7z.exe", "7za.exe", "unrar.exe"):
        p = shutil.which(exe)
        if p:
            return p
    for c in (r"C:\Program Files\7-Zip\7z.exe",
              r"C:\Program Files (x86)\7-Zip\7z.exe",
              r"D:\Apps\7-Zip\7z.exe"):
        if os.path.isfile(c):
            return c
    return None


def _extract_archive(arc_path, dest):
    """Extract an archive into ``dest`` and return the book files inside
    (INPUT_EXTS). Sanitized against path traversal and size bombs."""
    suffix = arc_path.suffix.lower()
    dest.mkdir(parents=True, exist_ok=True)
    if suffix == ".zip":
        try:
            with zipfile.ZipFile(arc_path) as zf:
                total = 0
                n = 0
                for info in zf.infolist():
                    n += 1
                    total += info.file_size
                    if n > MAX_ARCHIVE_FILES or total > MAX_EXTRACT_BYTES:
                        raise ArchiveError("压缩包过大或文件过多,已中止解压")
                    target = (dest / info.filename).resolve()
                    if not str(target).startswith(str(dest.resolve()) + os.sep):
                        continue  # skip path-traversal entries
                    if info.is_dir():
                        target.mkdir(parents=True, exist_ok=True)
                        continue
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with zf.open(info) as src, open(target, "wb") as out:
                        shutil.copyfileobj(src, out)
        except zipfile.BadZipFile:
            raise ArchiveError("ZIP 文件损坏或无法解析")
    else:
        z7 = find_7z()
        if z7 is None:
            raise ArchiveError(
                "RAR / 7Z 需要免费的 7-Zip 才能解压。请安装 7-Zip "
                "(https://www.7-zip.org/) 后重试,或改用 ZIP 格式(无需任何额外软件)")
        if os.path.basename(z7).lower().startswith("unrar"):
            cmd = [z7, "x", "-y", str(arc_path), str(dest) + os.sep]
        else:
            cmd = [z7, "x", "-y", "-o" + str(dest), str(arc_path)]
        proc = subprocess.run(cmd, capture_output=True, timeout=300)
        if proc.returncode != 0:
            raise ArchiveError("解压失败,请检查压缩包是否损坏")
    books = []
    for p in sorted(dest.rglob("*")):
        if p.is_file() and p.suffix.lower() in INPUT_EXTS:
            books.append(p)
        if len(books) > MAX_ARCHIVE_FILES:
            break
    if not books:
        raise ArchiveError("压缩包内未找到 TXT / EPUB / MOBI / AZW / AZW3 文件")
    return books
# Job history persistence (survives restarts; downloads still expire with the
# normal output TTL).
HISTORY_FILE = BASE / "history.json"
HISTORY_MAX = 300
HISTORY_TTL = 7 * 24 * 3600
# Auto-insert a synthetic section heading every ~10k chars when the TXT has no
# chapter markers at all, so the book still gets a TOC and can be split.
SECTION_CHARS = 10000

HOST = "127.0.0.1"
PORT = int(os.environ.get("TXT2EBOOK_PORT") or 8765)

# When CONFIG["host"] == "0.0.0.0", the server listens on all network
# interfaces so phones/tablets on the same LAN can open the page too.
# Default stays loopback-only for privacy (the service has no auth).
def effective_host():
    return "0.0.0.0" if CONFIG.get("host") == "0.0.0.0" else HOST


def lan_ip():
    """Best-effort local LAN IPv4 for printing the phone-access URL."""
    try:
        import socket
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return None

# Files larger than this get auto-split by chapter.
SPLIT_THRESHOLD = 5 * 1024 * 1024      # 5 MB: files larger than this get split
PART_TARGET_BYTES = 5 * 1024 * 1024    # aim for ~5 MB per part (split at chapter edges)
HEURISTICS_LIMIT = 1_000_000           # enable heuristics only under 1 MB

# Chapter heading detector for Chinese novels + English "Chapter N".
# Covers numbered headings (第X章/节/回/卷) plus common unnumbered ones
# (楔子/序/序章/引子/前言/番外/尾声/后记/终章 ...).
_SPECIAL_HEADINGS = r"楔子|序章|序言|序幕|引子|前言|后记|番外|尾声|终章"
CHAPTER_RE = re.compile(
    r"^\s*(?:第\s*[0-9零一二三四五六七八九十百千两万亿]+\s*[章节回卷]"
    r"|Chapter\s+[0-9]+"
    r"|(?:" + _SPECIAL_HEADINGS + r"|序)\s*$)",
    re.IGNORECASE,
)
# XPath handed to Calibre for chapter marks (fast path, no heuristics).
CHAPTER_XPATH = (
    r"//h:h1 | //*[re:test(., "
    r"'^\s*(第\s*[0-9零一二三四五六七八九十百千两万亿]+\s*[章节回卷]"
    r"|Chapter\s+[0-9]+"
    r"|(?:" + _SPECIAL_HEADINGS + r"|序)\s*$)', 'i')]"
)

# In-memory job registry: job_id -> dict(status, progress, parts, ...)
JOBS = {}
JOBS_LOCK = threading.Lock()
HISTORY = {}  # persisted terminal jobs, loaded from HISTORY_FILE at startup

# --- P1: limit how many conversions run at once (each spawns ebook-convert). ---
MAX_CONCURRENT = max(1, (os.cpu_count() or 2) // 2)
CONVERT_SEM = threading.BoundedSemaphore(MAX_CONCURRENT)

# --- P0: job/output retention. Finished jobs and their files are pruned after
# JOB_TTL seconds; at most MAX_JOBS newest jobs are kept regardless of age. ---
JOB_TTL = 6 * 3600          # 6 hours
MAX_JOBS = 50
UPLOAD_MAX_BYTES = 500 * 1024 * 1024   # reject uploads larger than 500 MB
MAX_FIELD_BYTES = 1024 * 1024          # per-form-field cap (guards memory)


def find_ebook_convert():
    exe = shutil.which("ebook-convert")
    if exe:
        return exe
    for c in (
        r"D:\Apps\Calibre2\ebook-convert.exe",
        r"C:\Program Files\Calibre2\ebook-convert.exe",
        r"C:\Program Files (x86)\Calibre2\ebook-convert.exe",
    ):
        if os.path.isfile(c):
            return c
    return None


EBOOK_CONVERT = find_ebook_convert()


def find_calibre_tool(name):
    """Find a Calibre CLI tool (e.g. calibre-debug) next to ebook-convert."""
    if EBOOK_CONVERT:
        cand = Path(EBOOK_CONVERT).parent / (name + ".exe")
        if cand.is_file():
            return str(cand)
    exe = shutil.which(name)
    return exe


def find_kindle_previewer():
    """Locate Amazon Kindle Previewer 3 (required for KFX output).
    Returns None when not installed; the UI then disables the KFX option.
    """
    exe = shutil.which("KindlePreviewer.exe")
    if exe:
        return exe
    for c in (
        r"C:\Program Files (x86)\Amazon\Kindle Previewer 3\KindlePreviewer.exe",
        r"C:\Program Files\Amazon\Kindle Previewer 3\KindlePreviewer.exe",
        r"D:\Apps\Kindle Previewer 3\KindlePreviewer.exe",
    ):
        if os.path.isfile(c):
            return c
    return None


KINDLE_PREVIEWER = find_kindle_previewer()


def _supports_chapter_pattern():
    """Some Calibre builds lack the --chapter-pattern CLI option; probe once."""
    try:
        out = subprocess.run([EBOOK_CONVERT, "--help"], capture_output=True,
                             timeout=30).stdout
        return b"--chapter-pattern" in out
    except Exception:
        return False


SUPPORTS_CHAPTER_PATTERN = bool(EBOOK_CONVERT) and _supports_chapter_pattern()


def _encoding_score(s):
    """Heuristic score for CJK decoding quality. GB18030 can decode almost any
    byte stream, so Big5 files would silently decode as GBK garbage; score both
    candidates and pick the better one. Wrong decodes typically produce
    private-use chars (GB18030's PUA mapping), Bopomofo / small-form punct
    (Big5 reading of GBK punctuation), or box-drawing glyphs."""
    cjk = sum(1 for c in s
              if "\u4e00" <= c <= "\u9fff" or "\u3400" <= c <= "\u4dbf")
    pua = sum(1 for c in s if 0xE000 <= ord(c) <= 0xF8FF)
    bopomo = sum(1 for c in s if 0x3100 <= ord(c) <= 0x312F)
    small = sum(1 for c in s if 0xFE50 <= ord(c) <= 0xFE6F)
    boxes = sum(1 for c in s if 0x2500 <= ord(c) <= 0x257F)
    fffd = s.count("\ufffd")
    return cjk - pua * 4 - bopomo * 8 - small * 8 - boxes * 4 - fffd * 10


def read_text_auto(path):
    """Read a text file: UTF-8 (BOM) first, then pick the best-scoring CJK
    decode between GB18030 and Big5, falling back to lossy UTF-8."""
    raw = Path(path).read_bytes()
    for enc in ("utf-8-sig", "utf-8"):
        try:
            return raw.decode(enc), enc
        except UnicodeDecodeError:
            continue
    best, best_enc, best_score = None, None, None
    for enc in ("gb18030", "big5", "big5hkscs"):
        try:
            dec = raw.decode(enc)
        except UnicodeDecodeError:
            continue
        sc = _encoding_score(dec)
        if best is None or sc > best_score:
            best, best_enc, best_score = dec, enc, sc
    if best is not None:
        return best, best_enc
    return raw.decode("utf-8", "replace"), "utf-8"


_NUM_RE = re.compile(r"\d+")


def natural_sort_key(name):
    """Sort key that orders names by the numbers embedded in them (方案 A).

    e.g. "小说_1-100.txt" < "小说_101-200.txt" < "小说_201-300.txt".
    Files with no digits keep a stable, case-insensitive alphabetical order and
    sort after numbered ones. Returns a tuple mixing the first number found
    (primary), then all numbers, then the lowercased name as a tiebreaker.
    """
    nums = [int(x) for x in _NUM_RE.findall(name)]
    first = nums[0] if nums else -1
    return (0 if nums else 1, first, nums, name.lower())


def _strip_repeat_header(text):
    """Remove leading 书名/作者/简介 metadata lines from a follow-on volume so
    the merged book does not repeat them mid-text. Only strips within the first
    handful of lines and stops at the first real content/chapter line.
    """
    lines = text.splitlines(keepends=True)
    i = 0
    checked = 0
    while i < len(lines) and checked < 12:
        ln = lines[i]
        stripped = ln.strip()
        checked += 1
        if stripped == "":
            i += 1
            continue
        is_meta = (any(rx.match(ln) for rx in TITLE_RES)
                   or any(rx.match(ln) for rx in AUTHOR_RES)
                   or any(rx.match(ln) for rx in INTRO_RES))
        if is_meta:
            i += 1
            continue
        break
    return "".join(lines[i:])


def merge_txt_files(paths, out_path):
    """Merge several TXT files (already in the desired order) into out_path.

    - Auto-detects each file's encoding via read_text_auto.
    - Keeps the first file's header (书名/作者/简介) intact; strips repeated
      headers from subsequent volumes.
    - Joins parts with a blank line so chapter detection stays clean.
    Returns the total character count written.
    """
    chunks = []
    for k, p in enumerate(paths):
        text, _ = read_text_auto(p)
        # Normalize newlines so mixed \r\n / \r / \n across volumes don't create
        # stray blank lines or lone \r after concatenation.
        text = text.replace("\r\n", "\n").replace("\r", "\n")
        if k > 0:
            text = _strip_repeat_header(text)
        text = text.strip("\n")
        chunks.append(text)
    merged = "\n\n".join(chunks) + "\n"
    # newline="" prevents Windows from turning every \n into \r\n on write.
    with open(out_path, "w", encoding="utf-8", newline="") as fh:
        fh.write(merged)
    return len(merged)


# Metadata extractors: match common Chinese TXT headers like
#   书名:xxx   /   书名:xxx   /   《xxx》
#   作者:xxx   /   作者:xxx
# NOTE: the full-width colon is written as \uff1a explicitly so it survives
# source re-encoding (a literal : was previously flattened to ':', which
# broke metadata detection for files using the full-width colon).
_COLON = r"[:\uff1a]"
TITLE_RES = [
    re.compile(r"^\s*[书書]\s*名\s*" + _COLON + r"\s*(.+?)\s*$"),
    re.compile(r"^\s*《\s*(.+?)\s*》\s*$"),
]
AUTHOR_RES = [
    re.compile(r"^\s*作\s*者\s*" + _COLON + r"\s*(.+?)\s*$"),
    re.compile(r"^\s*著\s*者\s*" + _COLON + r"\s*(.+?)\s*$"),
]
INTRO_RES = [
    re.compile(r"^\s*(?:[简簡]\s*介|内[容內]\s*[简簡]介|[简簡]\s*述)\s*" + _COLON + r"\s*(.+?)\s*$"),
]


def extract_metadata(text, fallback_title):
    """Scan the first ~40 lines for 书名/作者/简介. Returns (title, author, intro).
    Falls back to the filename stem for the title, and None for author/intro.
    """
    title = author = intro = None
    head_lines = text.splitlines()[:40]
    for ln in head_lines:
        if title is None:
            for rx in TITLE_RES:
                m = rx.match(ln)
                if m:
                    title = m.group(1).strip()
                    break
        if author is None:
            for rx in AUTHOR_RES:
                m = rx.match(ln)
                if m:
                    author = m.group(1).strip()
                    break
        if intro is None:
            for rx in INTRO_RES:
                m = rx.match(ln)
                if m:
                    intro = m.group(1).strip()
                    break
        # Stop early once we hit the first chapter
        if CHAPTER_RE.match(ln):
            break
    if not title:
        title = fallback_title
    return title, author, intro


def generate_cover(title, author, out_path):
    """Use calibre-debug to render a default cover JPG. Returns True on success."""
    dbg = find_calibre_tool("calibre-debug")
    if not dbg:
        return False
    authors_list = [author] if author else ['未知作者']
    script = (
        "from calibre.ebooks.covers import create_cover\n"
        f"data = create_cover({title!r}, {authors_list!r})\n"
        f"open({str(out_path)!r}, 'wb').write(data)\n"
    )
    # Write script to a temp .py next to output and run it
    script_path = Path(out_path).with_suffix(".covergen.py")
    try:
        script_path.write_text(script, encoding="utf-8")
        proc = subprocess.run([dbg, str(script_path)],
                              capture_output=True, timeout=120)
        ok = Path(out_path).is_file() and Path(out_path).stat().st_size > 0
        return ok
    except Exception:
        return False
    finally:
        script_path.unlink(missing_ok=True)


def split_by_size(text, target_bytes, chapter_re=None):
    """Split text into parts of roughly target_bytes each, breaking only at
    chapter boundaries so no chapter is cut in half. Any preamble before the
    first chapter stays with part 1. Falls back to a single part if there are
    fewer than 2 chapters.
    """
    rx = chapter_re or CHAPTER_RE
    lines = text.splitlines(keepends=True)
    heads = [i for i, ln in enumerate(lines) if rx.match(ln)]
    if len(heads) < 2:
        return [text]

    # Build chapter blocks: [start_line, end_line) for each chapter.
    # Preamble (before first chapter) is merged into the first block.
    bounds = heads + [len(lines)]
    blocks = []
    for k in range(len(heads)):
        start = 0 if k == 0 else bounds[k]
        end = bounds[k + 1]
        block_text = "".join(lines[start:end])
        blocks.append(block_text)

    parts = []
    cur = []
    cur_bytes = 0
    for block in blocks:
        b = len(block.encode("utf-8"))
        # If adding this block would exceed target AND we already have content,
        # flush the current part first.
        if cur and cur_bytes + b > target_bytes:
            parts.append("".join(cur))
            cur = []
            cur_bytes = 0
        cur.append(block)
        cur_bytes += b
    if cur:
        parts.append("".join(cur))
    return parts


def _count_chapters(text, chapter_re=None):
    """Number of lines that look like chapter headings."""
    rx = chapter_re or CHAPTER_RE
    n = 0
    for ln in text.splitlines():
        if rx.match(ln):
            n += 1
    return n


_AD_LINE_RE = re.compile(
    r"^\s*(?:"
    r"(?:https?://|www\.)\S+"
    r"|(?:请|請)(?:收藏|记住|记)(?:本站|本网站|这个网站)?[。！!？?]?"
    r"|最新章节(?:请)?(?:百度|搜索)\S*"
    r"|(?:手机|电脑|wap)用户请访问\S*"
    r"|最快更新\S*"
    r"|无弹窗\S*免费阅读\S*"
    r"|(?:本站|网站)首发\S*"
    r"|首发\S*首发\S*"
    r"|本章未完[，,]?请(?:点击)?下一章继续阅读"
    r"|.*本章未完.*(?:下一章|浏览器|请点击).*"
    r"|(?:书友群|QQ群)\S*"
    r"|喜欢\S*请(?:收藏|投推荐票)\S*"
    r"|随手收藏[，,]方便下次阅读"
    r"|如果您觉得\S*请(?:收藏|推荐给朋友)\S*"
    r"|更多精彩小说请访问\S*"
    r"|.*看后求收藏.*"
    r"|.*(?:最新|新)网址[：:]\S*"
    r"|.*记住(?:新|旧)?域名.*"
    r"|.*更多内容加载中.*"
    r"|.*本站只支持手机浏览器访问.*"
    r"|.*请勿开启浏览器阅读模式.*"
    r"|(?:上?—?页|下?—?页|上一页|下一页|目录|返回书页|章节目录|上?—?章|下?—?章|上一章|下一章|加入书签|本章未完[，,]?请(?:点击)?下一页继续阅读)"
    r"|(?:上?—?章|下?—?章|上一章|下一章|目录|书架|排行榜|返回|首页){2,}"
    r"|^(?:首页|返回|我的|排行榜|书架|设置|搜索|加入书签|存书签|书签|关灯|护眼|夜间模式|推荐本书|章节列表|TXT下载|字[:：]?|大|中|小|←|→|上一章|下一章|目录|[\u4e00-\u9fff]{2,4}小说)$"
    r"|^[\u4e00-\u9fff·]{2,12}\s*>\s*[\u4e00-\u9fff·]{2,12}.*$"
    r")\s*$",
    re.IGNORECASE,
)


_AD_INLINE_RE = re.compile(
    r"(?:请|請)(?:收藏|记住|记)本站[。！!？?]?"
    r"|最新章节请(?:百度|搜索)\S*[。！!]?"
    r"|(?:手机|电脑)用户请访问\S*[。！!]?"
    r"|本站首发[。！!]?"
    r"|更多精彩小说请访问\S*[。！!]?"
    r"|最快更新[。！!]?"
    r"|无弹窗[，,]无广告[。！!]?"
    r"|看后求收藏[（(][^）)]*[）)]"
    r"|(?:最新|新)网址[：:]\S*"
    r"|[（(]\s*第?\s*\d+\s*/\s*\d+\s*页?\s*[)）]"
    r"|[（(]本章未完[，,]?请(?:点击)?下一页继续阅读[）)]"
)


def _clean_ad_lines(text):
    """Strip common novel-site ad / watermark lines. Conservative patterns so
    real content is never touched. Also removes short inline ad phrases that
    sites embed mid-paragraph. Returns (new_text, removed_count)."""
    out = []
    removed = 0
    for ln in text.splitlines(keepends=True):
        s = ln.strip()
        if not s:
            out.append(ln)
            continue
        # URL-ish line: one token, no CJK, short, and must actually look like
        # a URL (scheme / www. / known TLD) so plain lines like "2026.08.11"
        # or "e.g." are never mistaken for ads.
        _low = s.lower()
        is_urlish = (
            len(s) < 100 and " " not in s and "." in s
            and not any("\u4e00" <= c <= "\u9fff" for c in s)
            and (_low.startswith(("http://", "https://", "www."))
                 or re.search(
                     r"\.(?:com|net|org|cn|cc|top|xyz|info|biz|me|tv|io|html?|php|txt)$",
                     _low))
        )
        if _AD_LINE_RE.match(s) or is_urlish:
            removed += 1
            continue
        out.append(ln)
    text = "".join(out)
    # inline ad phrases embedded inside otherwise-clean paragraphs
    text, inline = _AD_INLINE_RE.subn("", text)
    return text, removed + inline


def _insert_sections(text, target_chars=SECTION_CHARS):
    """Insert synthetic section headings (第N节) at line boundaries roughly
    every ``target_chars`` characters. Used when the text has no chapter
    markers at all, so the book still gets a navigable TOC and can be split.
    """
    lines = text.splitlines(keepends=True)
    out = []
    acc = 0
    sec = 0
    for ln in lines:
        out.append(ln)
        acc += len(ln)
        if acc >= target_chars:
            sec += 1
            out.append(f"\n第{sec}节\n")
            acc = 0
    return "".join(out)


# ================= web novel grabber =================
# Personal-use web novel fetching: URL -> clean TXT in the local library.
# Extraction prefers trafilatura when installed (optional dependency); the
# stdlib fallback (urllib + HTMLParser) is the default path. Book sources are
# data-driven JSON files under sources/ - adapting a new site means adding a
# JSON file, not changing code.

GRAB_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
           "Chrome/126.0 Safari/537.36")
GRAB_TIMEOUT = 25
GRAB_MAX_PAGE_BYTES = 4 * 1024 * 1024
GRAB_MAX_CHAPTERS = 3000
GRAB_SEM = threading.BoundedSemaphore(2)
SOURCES_DIR = BASE / "sources"

# Chapter-heading pattern used to find chapter links on a TOC page.
_GRAB_CH_RE = re.compile(
    r"(?:第\s*[0-9零一二三四五六七八九十百千两万]+\s*[章节回卷]"
    r"|Chapter\s+[0-9]+"
    r"|(?:" + _SPECIAL_HEADINGS + r"))",
    re.IGNORECASE,
)
# Site-branding fragments stripped from <title> tags when guessing a book name.
_SITE_NOISE = ("笔趣阁", "无弹窗", "最新章节", "免费阅读", "全文阅读", "在线阅读",
               "txt下载", "小说阅读网", "5200", "88笔趣阁", "新笔趣阁",
               "笔尖中文", "书友最值得收藏", "最新章节目录", "全文", "小说",
               "章节目录", "无弹窗阅读")


class GrabError(Exception):
    pass


def _load_sources():
    """Load book-source JSON files from sources/*.json into a dict by id."""
    srcs = {}
    if SOURCES_DIR.is_dir():
        for p in sorted(SOURCES_DIR.glob("*.json")):
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
            except Exception:
                continue
            if not isinstance(data, dict) or not data.get("id"):
                continue
            data["_file"] = p.name
            srcs[data["id"]] = data
    return srcs


SOURCES = _load_sources()


def _sniff_charset(raw):
    """Best-effort charset from BOM / <meta charset> in the first bytes."""
    if raw.startswith(b"\xef\xbb\xbf"):
        return "utf-8-sig"
    head = raw[:4096].decode("latin-1", "replace")
    m = re.search(r'<meta[^>]+charset=["\']?([\w-]+)', head, re.I)
    if m:
        return m.group(1)
    m = re.search(
        r'<meta[^>]+http-equiv=["\']?content-type["\']?[^>]+'
        r'content=["\']?[^;]*;\s*charset=([\w-]+)', head, re.I)
    if m:
        return m.group(1)
    return None


def decode_web_bytes(raw, forced=None):
    """Decode fetched bytes to text. A source-declared encoding wins;
    otherwise BOM/meta sniff, then the same _encoding_score pick used for
    local files (utf-8 -> gb18030/big5)."""
    if forced:
        enc = "gb18030" if forced.lower() in ("gbk", "gb2312") else forced
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            return raw.decode(enc, "replace")
    enc = _sniff_charset(raw)
    for cand in (enc, "utf-8-sig", "utf-8"):
        if not cand:
            continue
        try:
            return raw.decode(cand)
        except UnicodeDecodeError:
            continue
    best, best_enc, best_score = None, None, None
    for cand in ("gb18030", "big5"):
        try:
            dec = raw.decode(cand)
        except UnicodeDecodeError:
            continue
        sc = _encoding_score(dec)
        if best is None or sc > best_score:
            best, best_enc, best_score = dec, cand, sc
    if best is not None:
        return best
    return raw.decode("utf-8", "replace")


def _http_fetch(url, job_id=None, timeout=GRAB_TIMEOUT, referer=None):
    """Download a page. Returns (raw_bytes, final_url). Retries once without
    TLS verification when the site has a broken certificate (common on
    ad-ridden novel mirrors), and retries transient 5xx responses."""
    def _open(ctx):
        req = urllib.request.Request(url, headers={
            "User-Agent": GRAB_UA,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.5",
        })
        if referer:
            req.add_header("Referer", referer)
        return urllib.request.urlopen(req, timeout=timeout, context=ctx)

    for attempt in range(4):
        try:
            resp = _open(None)
        except ssl.SSLError:
            resp = _open(ssl._create_unverified_context())
        except urllib.error.HTTPError as e:
            if e.code >= 500 and attempt < 3:
                time.sleep(2.0 + attempt * 2.0)
                continue
            raise GrabError(f"HTTP {e.code}: {url}")
        except (urllib.error.URLError, socket.timeout, OSError) as e:
            if attempt < 3:
                time.sleep(2.0 + attempt * 2.0)
                continue
            raise GrabError(f"无法访问页面: {e}")
        try:
            with resp:
                raw = resp.read(GRAB_MAX_PAGE_BYTES + 1)
                if len(raw) > GRAB_MAX_PAGE_BYTES:
                    raise GrabError("页面过大,已中止")
                cenc = resp.headers.get("Content-Encoding", "")
                final = resp.geturl()
        except GrabError:
            raise
        except Exception as e:
            raise GrabError(f"读取页面失败: {e}")
        break
    else:
        raise GrabError(f"HTTP 多次失败: {url}")
    if cenc == "gzip":
        raw = gzip.decompress(raw)
    elif cenc == "deflate":
        import zlib
        try:
            raw = zlib.decompress(raw)
        except zlib.error:
            raw = zlib.decompress(raw, -zlib.MAX_WBITS)
    return raw, final


# Tags that start a new line when converting HTML to text. ``div`` is NOT one
# of them: divs are usually wrappers, and breaking on every div would split
# article text around ad/script divs embedded mid-chapter.
_BLOCK_TAGS = {"p", "br", "li", "tr", "blockquote", "pre", "dd", "dt",
               "h1", "h2", "h3", "h4", "h5", "h6", "section", "article",
               "table", "ul", "ol", "hr", "form", "fieldset", "figure",
               "header", "footer", "nav", "aside"}
_SKIP_TAGS = {"script", "style", "noscript", "head", "iframe", "object",
              "embed", "svg", "canvas", "template", "select", "button"}


def _norm_line(s):
    return re.sub(r"[ \t\u3000]+", " ", s).strip()


class _TextCollector(HTMLParser):
    """Collect visible text from HTML; block elements become line breaks and
    script/style content is dropped. Returns a list of text lines."""
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.lines = []
        self.cur = []
        self.skip = 0

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        if tag in _SKIP_TAGS:
            self.skip += 1
            return
        if self.skip:
            return
        if tag in _BLOCK_TAGS or tag == "br":
            self._flush()

    def handle_endtag(self, tag):
        tag = tag.lower()
        if tag in _SKIP_TAGS:
            if self.skip:
                self.skip -= 1
            return
        if self.skip:
            return
        if tag in _BLOCK_TAGS or tag == "br":
            self._flush()

    def handle_data(self, data):
        if self.skip:
            return
        if data:
            self.cur.append(data)

    def _flush(self):
        s = _norm_line("".join(self.cur))
        if s:
            self.lines.append(s)
        self.cur = []

    def finish(self):
        self._flush()
        return self.lines


class _SelectorExtractor(HTMLParser):
    """Capture the text of the first element matching one of the given
    selectors. Selector = {"tag": "div", "id": "content"} - tag required,
    attributes optional (id exact, class token match)."""
    def __init__(self, selectors):
        super().__init__(convert_charrefs=True)
        self.selectors = selectors
        self.depth = 0
        self.match_tag = None
        self.lines = []
        self.cur = []
        self.in_skip = 0
        self._done = False

    def _matches(self, tag, attrs):
        attrs = dict(attrs)
        for sel in self.selectors:
            if sel.get("tag", "").lower() != tag:
                continue
            ok = True
            for k, v in sel.items():
                if k == "tag":
                    continue
                av = attrs.get(k)
                if k == "class":
                    if not av or v not in av.split():
                        ok = False
                        break
                elif av != v:
                    ok = False
                    break
            if ok:
                return True
        return False

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        if self._done:
            return
        if tag in _SKIP_TAGS:
            self.in_skip += 1
            return
        if self.depth:
            # only nested <div>s participate in depth counting (they must be
            # balanced in well-formed pages); <p>/<span>/... only flush. This
            # keeps extraction from running past the container when paragraphs
            # are unclosed or the page is sloppy.
            if tag == "div":
                self.depth += 1
            if tag in _BLOCK_TAGS:
                self._flush()
            return
        if self._matches(tag, attrs):
            self.match_tag = tag
            self.depth = 1

    def handle_endtag(self, tag):
        tag = tag.lower()
        if self._done:
            return
        if tag in _SKIP_TAGS:
            if self.in_skip:
                self.in_skip -= 1
            return
        if not self.depth:
            return
        if tag == self.match_tag:
            self.depth -= 1
            if self.depth == 0:
                self._flush()
                self._done = True
        elif tag == "div":
            # balance a nested div whose start tag we counted
            if self.depth:
                self.depth -= 1
        elif tag in _BLOCK_TAGS:
            self._flush()

    def handle_data(self, data):
        if self._done or self.in_skip or not self.depth:
            return
        if data:
            self.cur.append(data)

    def _flush(self):
        s = _norm_line("".join(self.cur))
        if s:
            self.lines.append(s)
        self.cur = []

    def result(self):
        return "\n".join(self.lines) if self.lines else None


def _extract_generic(html_text):
    """Trafilatura first (optional dependency), else a stdlib heuristic: find
    the longest run of consecutive text lines with decent CJK density, falling
    back to the largest text block overall."""
    try:
        import trafilatura
        out = trafilatura.extract(html_text, include_comments=False,
                                  include_tables=False)
        if out and out.strip():
            return out.strip()
    except Exception:
        pass
    collector = _TextCollector()
    try:
        collector.feed(html_text)
        collector.close()
    except Exception:
        pass
    lines = collector.finish()
    if not lines:
        return ""
    best = []
    cur = []
    for ln in lines:
        cjk = sum(1 for c in ln if "\u4e00" <= c <= "\u9fff")
        good = cjk >= 10 or (cjk / max(1, len(ln)) > 0.25 and cjk >= 3)
        if good:
            cur.append(ln)
            if len(cur) > len(best):
                best = list(cur)
        else:
            cur = []
    if len(best) >= 3:
        return "\n".join(best)
    return max(lines, key=len)


def _clean_page_title(raw):
    """Guess a book/chapter name from a <title> tag: drop parts that are pure
    site branding and strip common site suffixes."""
    if not raw:
        return ""
    s = raw.strip()
    parts = [p.strip() for p in re.split(r"[-_|·—]", s) if p.strip()]
    if len(parts) > 1:
        kept = [p for p in parts
                if not (len(p) <= 8 and any(n in p for n in _SITE_NOISE))]
        # a part that looks like a chapter heading is not the book name
        kept = [p for p in kept if not _GRAB_CH_RE.search(p)] or kept
        if kept:
            # prefer a part that is completely free of site branding
            clean = [p for p in kept if not any(n in p for n in _SITE_NOISE)]
            s = max(clean or kept, key=len)
    s = re.sub(r"(?:无弹窗|最新章节(?:列表)?|全文免费阅读|免费阅读|全文阅读|在线阅读|最新章节目录|"
               r"小说阅读网|txt下载|笔趣阁|5200|88笔趣阁|新笔趣阁|笔尖中文).*$", "", s)
    return s.strip(" _-|·（）()")


def _page_author(html_text):
    """Best-effort author from a TOC page (作 者:xxx / 作者:xxx). The colon is
    required so search-box placeholders like 书名、作者、角色 never match."""
    m = re.search(r"作\s*者\s*[:：]\s*(?:<[^>]+>)*\s*([^<>\s]{2,20})",
                  html_text)
    if m:
        return m.group(1).strip()
    return None


def _extract_chapter_links(html_text, base_url, link_re=None):
    """All anchors whose text looks like a chapter heading, in document
    order. Returns [(title, abs_url)] limited to the same site."""
    link_re = link_re or _GRAB_CH_RE
    links = []
    for m in re.finditer(
            r'<a[^>]*href\s*=\s*["\']([^"\']+)["\'][^>]*>(.*?)</a>',
            html_text, re.S | re.I):
        href = m.group(1).strip()
        text = _norm_line(re.sub(r"<[^>]+>", " ", m.group(2)))
        text = html_mod.unescape(text).strip()
        if not text or not link_re.search(text):
            continue
        low = href.lower()
        if low.startswith(("javascript:", "#", "mailto:")):
            continue
        abs_url = urljoin(base_url, href)
        if not abs_url.startswith(("http://", "https://")):
            continue
        bnet = urlparse(base_url).netloc
        anet = urlparse(abs_url).netloc
        if bnet and anet and anet != bnet \
                and not anet.endswith("." + bnet) and not bnet.endswith("." + anet):
            continue
        links.append((text, abs_url))
    return links


def _order_chapters(links):
    """Dedupe (keep last occurrence - sites list newest chapters first in a
    'recent' block before the full list) and put chapters in reading order."""
    seen = {}
    for text, url in links:
        # pop + re-insert so the LAST occurrence also wins the position
        seen.pop(url, None)
        seen[url] = (text, url)
    ordered = list(seen.values())
    if len(ordered) > 1:
        nums = []
        for text, _u in ordered[:120]:
            m = re.search(r"第\s*([0-9]+)\s*[章节回卷]", text)
            nums.append(int(m.group(1)) if m else None)
        desc = sum(1 for i in range(1, len(nums))
                   if nums[i] is not None and nums[i - 1] is not None
                   and nums[i] < nums[i - 1])
        asc = sum(1 for i in range(1, len(nums))
                  if nums[i] is not None and nums[i - 1] is not None
                  and nums[i] > nums[i - 1])
        if desc > asc:
            ordered.reverse()
    return ordered


def _toc_pages(base_url, html_text, pages_cfg, visited):
    """Follow TOC pagination links (e.g. 下一页 / 更多章节列表). Returns a
    list of additional page URLs (unvisited)."""
    out = []
    if not pages_cfg:
        return out
    try:
        rx = re.compile(pages_cfg.get("next_text_re") or "下一页|下页|更多章节")
    except re.error:
        return out
    for m in re.finditer(
            r'<a[^>]*href\s*=\s*["\']([^"\']+)["\'][^>]*>(.*?)</a>',
            html_text, re.S | re.I):
        href = m.group(1).strip()
        tx = _norm_line(re.sub(r"<[^>]+>", " ", m.group(2)))
        if not rx.search(tx):
            continue
        low = href.lower()
        if low.startswith(("javascript:", "#", "mailto:")):
            continue
        u = urljoin(base_url, href)
        if u.startswith(("http://", "https://")) and u not in visited:
            out.append(u)
    return out


def _chapter_pages(base_url, html_text, use_text_fallback=True):
    """Additional page URLs for a multi-page chapter (x.html -> x_2.html,
    x_3.html ...). Same-stem numeric links are authoritative; the 下一页/下页
    text fallback (``use_text_fallback``) is only used when the chapter has
    no same-stem paging - iterative paging walks must disable it so the
    "next chapter" link is never mistaken for a page of this chapter."""
    pages = []
    path = urlparse(base_url).path.rstrip("/")
    stem = path.rsplit("/", 1)[-1]
    stem_noext, ext = os.path.splitext(stem)
    # a paged URL (xxx-2.html / xxx_3.html / xxx_2/ ) must still match
    # sibling pages; strip any numeric suffix from the stem
    stem_noext = re.sub(r"[-_]\d+$", "", stem_noext)
    if not stem_noext:
        return pages
    if ext:
        page_re = re.compile(r"^" + re.escape(stem_noext) + r"[-_](\d+)\."
                             + re.escape(ext.lstrip(".")) + r"$")
        path_re = re.compile(r"^(.*/)?" + re.escape(stem_noext) + r"/(\d+)\."
                             + re.escape(ext.lstrip(".")) + r"$")
    else:
        page_re = re.compile(r"^" + re.escape(stem_noext) + r"[-_](\d+)/?$")
        path_re = None
    text_hits = []
    for m in re.finditer(
            r'<a[^>]*href\s*=\s*["\']([^"\']+)["\'][^>]*>([^<]{1,20})</a>',
            html_text, re.I):
        href = m.group(1).strip()
        tx = _norm_line(m.group(2))
        name = href.rstrip("/").rsplit("/", 1)[-1]
        # same chapter stem with a numeric suffix: xxx_2.html / xxx-2.html
        mm = page_re.match(name)
        if mm:
            pages.append((int(mm.group(1)), urljoin(base_url, href)))
            continue
        # path-style paging: .../54852657/2.html
        if path_re:
            pm = path_re.match(href)
            if pm:
                pages.append((int(pm.group(2)), urljoin(base_url, href)))
                continue
        elif use_text_fallback and tx in ("下一页", "下页", "下—页", "第2页") \
                and not href.lower().startswith(("javascript:", "#")):
            text_hits.append(urljoin(base_url, href))
    if not pages:
        pages = [(999 + i, u) for i, u in enumerate(text_hits)]
    pages.sort(key=lambda x: x[0])
    out = []
    for _n, u in pages:
        if u not in out:
            out.append(u)
    return out[:20]


def _looks_obfuscated(text):
    """Font-anti-scrape sites replace CJK with private-use glyphs; image
    chapters have almost no text at all."""
    if not text or not text.strip():
        return True
    cjk = sum(1 for c in text if "\u4e00" <= c <= "\u9fff")
    pua = sum(1 for c in text if 0xE000 <= ord(c) <= 0xF8FF)
    n = len(text)
    if pua / max(1, n) > 0.01:
        return True
    if n > 200 and cjk / n < 0.05:
        return True
    if n > 50 and cjk < 10:
        return True
    return False


def _safe_stem(name):
    stem = re.sub(r"[\\/:*?\"<>|\r\n\t]", " ", name or "")
    stem = re.sub(r"\s+", " ", stem).strip(" .")
    return stem[:80] or "book"


def _match_source(url):
    for s in SOURCES.values():
        rx = s.get("url_re")
        if rx:
            try:
                if re.search(rx, url, re.I):
                    return s
            except re.error:
                continue
    return None


def _link_prefixes(links):
    """Counter of directory prefixes among chapter links (path minus the
    filename). A real book TOC keeps one dominant prefix; a site homepage
    mixes many books (plus recommendation links matching chapter patterns)."""
    pref = {}
    for _tx, u in links:
        parts = urlparse(u).path.strip("/").split("/")
        key = "/".join(parts[:-1]) if len(parts) > 1 else "/".join(parts)
        pref[key] = pref.get(key, 0) + 1
    return pref


# Common content containers tried when no source config matched (generic
# mode): the first one with substantial CJK text wins.
_COMMON_CONTENT_SELECTORS = [
    {"tag": "div", "id": "content"},
    {"tag": "div", "class": "novelcontent"},
    {"tag": "div", "id": "chaptercontent"},
    {"tag": "div", "class": "con"},
    {"tag": "div", "class": "content"},
    {"tag": "div", "id": "txt"},
    {"tag": "div", "class": "txt"},
    {"tag": "div", "class": "nr1"},
    {"tag": "div", "class": "nr"},
    {"tag": "div", "class": "read-content"},
    {"tag": "div", "id": "booktxt"},
    {"tag": "div", "id": "BookText"},
    {"tag": "div", "class": "article-content"},
    {"tag": "article"},
]


def _extract_chapter(html_text, source, base_url):
    """Extract (chapter_title, content_text) from a chapter page."""
    title = None
    content = None
    if source:
        sel_title = (source.get("chapter") or {}).get("title") or []
        sel_body = (source.get("chapter") or {}).get("content") or []
        if sel_body:
            ex = _SelectorExtractor(sel_body)
            try:
                ex.feed(html_text)
                ex.close()
            except Exception:
                pass
            content = ex.result()
        if sel_title:
            ex2 = _SelectorExtractor(sel_title)
            try:
                ex2.feed(html_text)
                ex2.close()
            except Exception:
                pass
            title = ex2.result()
            if title and not _GRAB_CH_RE.search(title):
                # the first h1 is often the site logo; look for a
                # chapter-looking h1 anywhere on the page
                for m in re.finditer(r"<h1[^>]*>(.*?)</h1>",
                                     html_text, re.S | re.I):
                    cand = _norm_line(re.sub(r"<[^>]+>", " ", m.group(1)))
                    if _GRAB_CH_RE.search(cand):
                        title = cand
                        break
    if content is None:
        # no source config: try common content containers first, then the
        # generic longest-text-block heuristic
        for sel in _COMMON_CONTENT_SELECTORS:
            try:
                ex = _SelectorExtractor([sel])
                ex.feed(html_text)
                ex.close()
                out = ex.result()
            except Exception:
                out = None
            if out and len(out.strip()) > 100:
                content = out
                break
    if content is None:
        content = _extract_generic(html_text)
    if not title:
        # prefer an h1 that looks like a chapter heading (the first h1 is
        # often the site logo)
        for m in re.finditer(r"<h1[^>]*>(.*?)</h1>", html_text, re.S | re.I):
            cand = _norm_line(re.sub(r"<[^>]+>", " ", m.group(1)))
            if _GRAB_CH_RE.search(cand):
                title = cand
                break
        if not title:
            m = re.search(r"<h1[^>]*>(.*?)</h1>", html_text, re.S | re.I)
            if m:
                title = _norm_line(re.sub(r"<[^>]+>", " ", m.group(1)))
    if not title:
        m = re.search(r"<title>([^<]*)</title>", html_text, re.S | re.I)
        if m:
            title = _clean_page_title(m.group(1))
    if title:
        # strip page-number suffixes like (第1/2页) and breadcrumbs like
        # "捞尸人 > 第一章" (keep the last crumb = the chapter itself)
        title = re.sub(r"[（(]\s*第?\s*\d+\s*/\s*\d+\s*页?\s*[)）]\s*$",
                       "", title).strip()
        if ">" in title:
            title = title.split(">")[-1].strip()
    return title, content


def _write_library_book(title, author, text, source_url):
    """Store a cleaned book into library/ and open a reading session.
    Returns (book_id, sid_or_None)."""
    book_id = hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()[:16]
    lib = READ_LIB / book_id
    lib.mkdir(parents=True, exist_ok=True)
    (lib / "content.txt").write_text(text, encoding="utf-8")
    try:
        (lib / "meta.json").write_text(json.dumps(
            {"title": title or "未命名", "author": author or "",
             "opened": time.time(), "source_url": source_url,
             "grabbed": True}, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass
    sid = None
    try:
        sid, _resume = _open_read_session(title or "未命名", None, book_id)
    except Exception:
        pass
    _prune_read_lib()
    return book_id, sid


def _grab_single_chapter(job_id, url, html_text, final_url, source, enc,
                         clean_ads, _log, render="auto"):
    """A single chapter page -> one book. Returns dict or raises GrabError."""
    use_render = render == "on"
    title, content = _extract_chapter(html_text, source, final_url)
    if content is None or len(content.strip()) < 50 \
            or _looks_obfuscated(content):
        raise GrabError("未能提取到正文(页面可能需要登录、是动态页面或使用字体反爬/图片章节)")
    if clean_ads:
        content, _removed = _clean_ad_lines(content)
    # iteratively follow ALL pages of a multi-page chapter
    pages = _chapter_pages(final_url, html_text)
    if pages:
        _log(f"章节有多个分页,正在抓取剩余页面...")
        seen = {final_url}
        cur_url, cur_html = final_url, html_text
        for _hop in range(15):
            if _is_cancelled(job_id):
                raise GrabError("cancelled")
            nxt = None
            for pu in _chapter_pages(cur_url, cur_html, use_text_fallback=False):
                if pu not in seen:
                    nxt = pu
                    break
            if nxt is None:
                break
            seen.add(nxt)
            try:
                html2, _f, _m = _fetch_html(nxt, job_id, source,
                                            prefer_render=use_render,
                                            referer=cur_url)
            except GrabError:
                break
            _t, part = _extract_chapter(html2, source, nxt)
            if part and not _looks_obfuscated(part):
                if clean_ads:
                    part, _r2 = _clean_ad_lines(part)
                content = content.rstrip() + "\n\n" + part.strip()
            cur_url, cur_html = nxt, html2
            set_job(job_id, progress=30 + min(40, _hop * 8))
    m = re.search(r"<title>([^<]*)</title>", html_text, re.S | re.I)
    page_title = _clean_page_title(m.group(1)) if m else ""
    book_title = page_title or title or "未命名章节"
    # short/placeholder titles (e.g. just the site name) fall back to the
    # breadcrumb book name when the page has one (捞尸人 > 第一章)
    if len(book_title) < 4 or book_title in ("章节页", "未命名章节"):
        m1 = re.search(r"<h1[^>]*>(.*?)</h1>", html_text, re.S | re.I)
        if m1:
            crumb = _norm_line(re.sub(r"<[^>]+>", " ", m1.group(1)))
            if ">" in crumb:
                book_title = crumb.split(">")[0].strip() or book_title
    # prepend the chapter heading so chapter detection / TOC works
    heading = (title or "").strip()
    content = content.strip()
    if heading and not _GRAB_CH_RE.search(content.split("\n", 1)[0]):
        content = heading + "\n" + content
    return {"title": book_title, "text": content, "chapters": 1,
            "chapter_titles": [title or book_title]}


def _fetch_chapter_all(job_id, ch_url, html_c, source, enc, use_render=False):
    """Extract a chapter and iteratively follow ALL of its pages
    (xxx_2.html, xxx_3.html ...). Returns (heading, full_body)."""
    heading, body = _extract_chapter(html_c, source, ch_url)
    if body is None:
        return heading, body
    current_url = ch_url
    current_html = html_c
    seen = {ch_url}
    for _hop in range(15):
        if _is_cancelled(job_id):
            raise GrabError("cancelled")
        nxt = None
        for pu in _chapter_pages(current_url, current_html,
                                 use_text_fallback=False):
            if pu not in seen:
                nxt = pu
                break
        if nxt is None:
            break
        seen.add(nxt)
        try:
            html_p, _f, _m = _fetch_html(nxt, job_id, source,
                                         prefer_render=use_render,
                                         referer=current_url)
        except GrabError:
            break
        _t, part = _extract_chapter(html_p, source, nxt)
        if part and not _looks_obfuscated(part):
            part = part.strip()
            # drop the repeated heading at the top of a paged part
            first, _, rest = part.partition("\n")
            first_norm = re.sub(r"[（(]\s*第?\s*\d+\s*/\s*\d+\s*页?\s*[)）]\s*$",
                                "", _norm_line(first)).strip()
            if _t and first_norm == _norm_line(_t):
                part = rest.lstrip("\n")
            body = body.rstrip() + "\n\n" + part
        current_url = nxt
        current_html = html_p
    return heading, body


def _grab_whole_book(job_id, url, html_text, final_url, source, enc,
                     clean_ads, max_chapters, _log, render="auto"):
    """A TOC page -> every chapter fetched and merged. Returns dict."""
    use_render = render == "on"
    toc_cfg = (source or {}).get("toc") or {}
    link_re = None
    try:
        link_re = re.compile(toc_cfg.get("link_re") or str(_GRAB_CH_RE.pattern))
    except re.error:
        link_re = _GRAB_CH_RE
    chapters = _extract_chapter_links(html_text, final_url, link_re)
    # follow TOC pagination (sites that split the chapter list across pages)
    visited = {final_url}
    pages_cfg = toc_cfg.get("pages")
    if not pages_cfg and source is None:
        # generic mode (no source config): auto-follow 下一页/更多章节 links
        pages_cfg = {"next_text_re": "下一页|下页|更多章节|更多",
                     "max_pages": 30}
    pages_cfg = pages_cfg or {}
    max_pages = int(pages_cfg.get("max_pages") or 0) or 20
    queue = _toc_pages(final_url, html_text, pages_cfg, visited)
    hops = 0
    while queue and hops < max_pages:
        pu = queue.pop(0)
        if pu in visited:
            continue
        visited.add(pu)
        try:
            html_p, _f2, _m2 = _fetch_html(pu, job_id, source,
                                            prefer_render=use_render,
                                            referer=final_url)
        except GrabError:
            continue
        chapters += _extract_chapter_links(html_p, pu, link_re)
        hops += 1
        set_job(job_id, progress=8, message=f"正在读取目录分页 {hops}/{max_pages}...")
        queue += _toc_pages(pu, html_p, pages_cfg, visited)
    chapters = _order_chapters(chapters)
    _log(f"目录共 {len(chapters)} 章")
    if max_chapters and len(chapters) > max_chapters:
        _log(f"章节过多,仅抓取前 {max_chapters} 章")
        chapters = chapters[:max_chapters]
    if not chapters:
        raise GrabError("目录页未找到章节链接(页面可能需要登录或为动态页面)")

    m = re.search(r"<title>([^<]*)</title>", html_text, re.S | re.I)
    book_title = _clean_page_title(m.group(1)) if m else ""
    author = _page_author(html_text)

    parts = []
    skipped = []
    ads_total = 0
    total = len(chapters)
    _log_rendered = False
    set_job(job_id, total_parts=total, done_parts=0, progress=10,
            message=f"目录 {total} 章,开始逐章抓取...")
    for i, (ch_title, ch_url) in enumerate(chapters, 1):
        if _is_cancelled(job_id):
            raise GrabError("cancelled")
        body = None
        heading = None
        try:
            html_c, _f, _m = _fetch_html(ch_url, job_id, source,
                                         prefer_render=use_render,
                                         referer=final_url)
            heading, body = _fetch_chapter_all(job_id, ch_url, html_c,
                                               source, enc,
                                               use_render=use_render)
            # static page may be a JS shell / encrypted content: retry with
            # the browser renderer when the body is suspiciously small
            if (body is None or len(body.strip()) < 600) and not use_render:
                try:
                    html_r, _f2, _m2 = _fetch_html(
                        ch_url, job_id, source, prefer_render=True,
                        referer=final_url)
                    heading2, body2 = _fetch_chapter_all(
                        job_id, ch_url, html_r, source, enc, use_render=True)
                    if body2 and len(body2.strip()) >= 50:
                        heading, body = heading2, body2
                        use_render = True
                        if not _log_rendered:
                            _log("检测到章节为 JS 动态加载,已启用浏览器渲染")
                            _log_rendered = True
                except GrabError:
                    pass
            if body is None or len(body.strip()) < 50:
                skipped.append((ch_title, "空内容/图片章节"))
                body = None
            elif _looks_obfuscated(body):
                skipped.append((ch_title, "疑似字体反爬"))
                body = None
        except GrabError as e:
            skipped.append((ch_title, str(e)))
            body = None
        if body is not None:
            if clean_ads:
                body, r = _clean_ad_lines(body)
                ads_total += r
            h = (heading or ch_title).strip()
            h = re.sub(r"[（(]\s*第?\s*\d+\s*/\s*\d+\s*页?\s*[)）]\s*$",
                       "", h).strip()
            b = body.strip()
            # drop a duplicate heading at the top of the body (rendered pages
            # often repeat the h1 inside the content container)
            first, _, rest = b.partition("\n")
            if _norm_line(first) == h:
                b = rest.lstrip("\n")
            elif b == h:
                b = ""
            # strip nav tokens glued to the first line (上一章目录下一章 …)
            b = re.sub(r"^(?:上?—?章|下?—?章|上一章|下一章|目录|书架|排行榜|返回|首页){2,}\s*",
                       "", b)
            parts.append(h + "\n" + b)
        pct = 10 + int(i / total * 80)
        set_job(job_id, done_parts=i, progress=pct,
                message=f"正在抓取 {i}/{total} 章: {ch_title[:22]}")
        if i % 25 == 0:
            _log(f"已抓取 {i}/{total} 章 · 跳过 {len(skipped)}")
        time.sleep(0.15)
    if not parts:
        raise GrabError("所有章节都未能提取(该站可能使用字体反爬/图片章节或需要登录)")
    if len(skipped) > len(parts) * 2:
        _log(f"⚠️ 大量章节失败({len(skipped)}/{total}),该站可能使用字体反爬或图片章节")
    text = "\n\n".join(parts)
    if _count_chapters(text) < 2:
        text = _insert_sections(text)
    return {"title": book_title, "author": author, "text": text,
            "chapters": len(parts), "skipped": skipped,
            "ads_removed": ads_total, "chapter_titles": [p.split("\n", 1)[0] for p in parts]}


# ===== optional headless-browser rendering (Playwright) =====
# Static fetching can't see JS-loaded content. When enabled we drive a real
# headless browser (system Chrome/Edge via Playwright, or Playwright's own
# Chromium if installed) so JS-rendered pages still work. Optional:
#   pip install playwright
#   python -m playwright install chromium   (or just have Chrome/Edge)

RENDER_TIMEOUT = 20000
_RENDER_LOCK = threading.Lock()
_render_worker = None
_render_queue = None
_render_ready = None   # None=untried, False=missing deps, True=ok
_render_browser = None


def _find_chrome():
    for c in (r"C:\Program Files\Google\Chrome\Application\chrome.exe",
              r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
              r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
              r"C:\Program Files\Microsoft\Edge\Application\msedge.exe"):
        if os.path.isfile(c):
            return c
    return None


def _render_available():
    """True when Playwright + a browser binary are usable."""
    global _render_ready
    if _render_ready is None:
        try:
            import playwright.sync_api  # noqa: F401
            _render_ready = _find_chrome() is not None or bool(
                (Path(os.environ.get("LOCALAPPDATA", "")) / "ms-playwright").exists())
        except Exception:
            _render_ready = False
    return _render_ready


def _ensure_render_worker():
    """Start the dedicated render thread (Playwright sync API is bound to
    the thread that started it, so all rendering goes through one thread)."""
    global _render_worker, _render_queue
    if _render_worker is not None:
        return
    import queue
    _render_queue = queue.Queue()

    def _worker():
        global _render_browser
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            kwargs = {}
            exe = _find_chrome()
            if exe:
                kwargs["executable_path"] = exe
            _render_browser = p.chromium.launch(
                headless=True, args=["--no-sandbox", "--disable-gpu"],
                **kwargs)
            while True:
                item = _render_queue.get()
                if item is None:
                    break
                url, sel, holder = item
                try:
                    page = _render_browser.new_page(user_agent=GRAB_UA,
                                                    locale="zh-CN")
                    try:
                        page.set_default_timeout(RENDER_TIMEOUT)
                        page.goto(url, wait_until="domcontentloaded",
                                  timeout=RENDER_TIMEOUT)
                        # JS-injected content needs a moment; poll the text
                        # length until it stabilizes at a substantial size
                        # (never grab a skeleton), up to the deadline
                        if sel:
                            try:
                                page.wait_for_selector(sel, timeout=12000)
                            except Exception:
                                pass
                        last_len = -1
                        stable = 0
                        deadline = time.time() + 15
                        while time.time() < deadline:
                            try:
                                ln = page.evaluate(
                                    "document.body ? document.body.innerText.length : 0")
                            except Exception:
                                ln = 0
                            if ln == last_len:
                                stable += 1
                            else:
                                stable = 0
                                last_len = ln
                            if stable >= 4 and ln > 800:
                                break
                            time.sleep(0.5)
                        holder["html"] = page.content()
                    finally:
                        page.close()
                except Exception as e:
                    holder["error"] = str(e)
                _render_queue.task_done()

    _render_worker = threading.Thread(target=_worker, daemon=True)
    _render_worker.start()


def _render_page(url, wait_selector=None, timeout=RENDER_TIMEOUT + 8000):
    """Render a page in headless Chromium and return its HTML."""
    if not _render_available():
        raise GrabError("未安装浏览器渲染组件(需要 pip install playwright;\n"
                        "且系统装有 Chrome/Edge,或运行 playwright install chromium)")
    _ensure_render_worker()
    holder = {}
    _render_queue.put((url, wait_selector, holder))
    end = time.time() + timeout
    while time.time() < end:
        if "html" in holder or "error" in holder:
            break
        time.sleep(0.2)
    if "html" in holder:
        return holder["html"]
    if "error" in holder:
        raise GrabError(f"浏览器渲染失败: {holder['error']}")
    raise GrabError("浏览器渲染超时")


def _looks_js_loaded(html_text):
    """Heuristic: page looks like a JS-only shell."""
    s = html_text.lower()
    if "enable javascript to run this app" in s:
        return True
    if "加载中" in html_text and len(html_text) < 4000:
        return True
    if len(html_text) < 2000:
        return True
    return False


def _fetch_html(url, job_id=None, source=None, prefer_render=False,
                render_fallback=True, referer=None):
    """Fetch a page: plain HTTP first; when it looks JS-loaded (or
    prefer_render), render it in headless Chromium. Returns (html, mode)
    where mode is 'static' or 'render'."""
    enc = (source or {}).get("encoding") or None
    try:
        raw, final = _http_fetch(url, job_id, referer=referer)
        html = decode_web_bytes(raw, enc)
    except GrabError:
        html = None
        final = url
    if html is not None and not _looks_js_loaded(html) and not prefer_render:
        return html, final, "static"
    # static path failed or looks JS-loaded: try rendering
    if render_fallback:
        try:
            rhtml = _render_page(url)
            rtext = decode_web_bytes(rhtml.encode("utf-8", "replace"), enc)
            return rtext, final, "render"
        except GrabError:
            pass
    if html is not None:
        return html, final, "static"
    raise GrabError(f"无法访问页面: {url}")


def _run_grab_body(job_id, url, source, mode, clean_ads, render,
                   title_override, author_override, max_chapters, _log):
    html_text, final_url, fetch_mode = _fetch_html(url, job_id, source,
                                                   prefer_render=(render == "on"))
    if source is None:
        source = _match_source(final_url) or _match_source(url)
    enc = (source or {}).get("encoding") or None
    if fetch_mode == "render":
        # page was JS-loaded; keep rendering for the rest of this job
        render = "on"
        _log("该页面为 JS 动态加载,已启用浏览器渲染模式")
    set_job(job_id, progress=5, message="已下载页面,正在解析...")
    _log(f"已下载页面 · 方式 {'浏览器渲染' if fetch_mode == 'render' else '直连'} · "
         f"书源 {source.get('name') if source else '通用提取'}")

    links = _extract_chapter_links(html_text, final_url)
    page_mode = mode
    if page_mode == "auto":
        # many chapter links under one dominant URL prefix = a book TOC;
        # links spread over many prefixes = homepage/recent list
        if len(links) >= 5:
            pref = _link_prefixes(links)
            top, n = max(pref.items(), key=lambda kv: kv[1])
            if n / len(links) >= 0.8:
                page_mode = "toc"
        if page_mode == "auto":
            page_mode = "chapter"
    _log(f"页面类型: {'目录页' if page_mode == 'toc' else '章节页'} · "
         f"识别到章节链接 {len(links)} 个")

    if page_mode == "chapter":
        res = _grab_single_chapter(job_id, url, html_text, final_url,
                                   source, enc, clean_ads, _log, render)
    else:
        res = _grab_whole_book(job_id, url, html_text, final_url,
                               source, enc, clean_ads, max_chapters, _log, render)

    title = (title_override or res.get("title") or "未命名").strip()
    author = (author_override or res.get("author") or "").strip()
    text = res["text"]
    if len(text.encode("utf-8")) > READ_MAX_TEXT:
        raise GrabError("抓取内容过大(净化后超过 30MB),请用「转电子书」拆分后再阅读")
    book_id, sid = _write_library_book(title, author, text, url)

    # also offer the raw TXT for download / normal conversion flow
    job_dir = output_base() / job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    try:
        (job_dir / ".txt2ebook_job").write_text("1")
    except OSError:
        pass
    stem = _safe_stem(title)
    txt_path = job_dir / (stem + ".txt")
    txt_path.write_text(text, encoding="utf-8")

    skipped = res.get("skipped") or []
    ads_n = res.get("ads_removed") or 0
    msg = (f"《{title}》抓取完成 · {res.get('chapters', 1)} 章 · "
           f"{len(text):,} 字")
    if skipped:
        msg += f" · 跳过 {len(skipped)} 章"
    if ads_n:
        msg += f" · 清理广告 {ads_n} 行"
    _log(msg)
    set_job(job_id, book_id=book_id, title=title, author=author, sid=sid,
            message=msg)
    _finish_job(job_id, status="done", progress=100, book_id=book_id,
                sid=sid, title=title, author=author,
                chapters=res.get("chapters", 1),
                chars=len(text), skipped=len(skipped),
                download=f"/download/{job_id}/{quote(txt_path.name)}",
                filename=txt_path.name, size=txt_path.stat().st_size,
                is_zip=False, message=msg)


def run_grab(job_id, url, source=None, mode="auto", clean_ads=True,
             render="auto", title_override=None, author_override=None,
             max_chapters=None):
    """Worker thread for /grab: fetch a chapter page or a whole book and
    store a clean TXT into the library + job dir. kind='grab'.
    ``render``: auto = static first, fall back to headless browser when a
    page looks JS-loaded; off = static only; on = always render."""
    log = []

    def _log(msg):
        log.append(str(msg))
        set_job(job_id, log=log[-150:])

    if not GRAB_SEM.acquire(blocking=False):
        set_job(job_id, status="running", message="排队中(等待空闲抓取名额)...")
        while not GRAB_SEM.acquire(timeout=1):
            if _is_cancelled(job_id):
                _finish_job(job_id, status="cancelled", message="已取消")
                return
    try:
        try:
            _run_grab_body(job_id, url, source, mode, clean_ads, render,
                           title_override, author_override, max_chapters, _log)
        except GrabError as e:
            if str(e) == "cancelled":
                _finish_job(job_id, status="cancelled", message="已取消")
            else:
                _finish_job(job_id, status="error", message=str(e))
        except Exception as e:
            _finish_job(job_id, status="error", message=f"抓取失败: {e}")
    finally:
        GRAB_SEM.release()


def _run_proc_cancellable(job_id, cmd, timeout_s):
    """Run ``cmd`` with cancellation + timeout support.

    Pipes are drained on reader threads so a chatty process can never deadlock
    on a full pipe. Returns (rc, out, err, timed_out, cancelled).
    """
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    out_chunks, err_chunks = [], []

    def _drain(stream, sink):
        for line in iter(stream.readline, b""):
            sink.append(line)

    t_out = threading.Thread(target=_drain, args=(proc.stdout, out_chunks),
                             daemon=True)
    t_err = threading.Thread(target=_drain, args=(proc.stderr, err_chunks),
                             daemon=True)
    t_out.start()
    t_err.start()
    rc = None
    timed_out = False
    cancelled = False
    deadline = time.time() + timeout_s
    while rc is None:
        if _is_cancelled(job_id):
            cancelled = True
            proc.kill()
            rc = proc.wait()
            break
        try:
            rc = proc.wait(timeout=1)
        except subprocess.TimeoutExpired:
            if time.time() > deadline:
                timed_out = True
                proc.kill()
                rc = proc.wait()
                break
    t_out.join(timeout=5)
    t_err.join(timeout=5)
    return rc, b"".join(out_chunks), b"".join(err_chunks), timed_out, cancelled


def set_job(job_id, **kw):
    with JOBS_LOCK:
        JOBS[job_id].update(kw)


def _is_cancelled(job_id):
    """True when the user asked to cancel this job."""
    with JOBS_LOCK:
        return bool(JOBS.get(job_id, {}).get("cancel_requested"))


def _load_history():
    """Load persisted job history from HISTORY_FILE. Returns dict job_id -> entry."""
    hist = {}
    try:
        data = json.loads(HISTORY_FILE.read_text(encoding="utf-8"))
        for item in data:
            if isinstance(item, dict) and item.get("job_id"):
                hist[item["job_id"]] = item
    except Exception:
        pass
    return hist


def _save_history_locked():
    """Prune and persist HISTORY to disk. Caller must hold JOBS_LOCK."""
    now = time.time()
    for jid in [j for j, e in HISTORY.items()
                if e.get("created") and now - e["created"] > HISTORY_TTL]:
        HISTORY.pop(jid, None)
    if len(HISTORY) > HISTORY_MAX:
        by_age = sorted(HISTORY.values(), key=lambda x: x.get("created") or 0)
        for old in by_age[:len(HISTORY) - HISTORY_MAX]:
            HISTORY.pop(old.get("job_id"), None)
    try:
        data = sorted(HISTORY.values(),
                      key=lambda x: x.get("created") or 0, reverse=True)
        HISTORY_FILE.write_text(json.dumps(data, ensure_ascii=False),
                                encoding="utf-8")
    except Exception:
        pass


def _finish_job(job_id, **kw):
    """Set a terminal status on a job and persist it to history."""
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        if job is None:
            return
        job.update(kw)
        if not job.get("saved"):
            job["saved"] = True
            HISTORY[job_id] = {
                "job_id": job_id,
                "kind": job.get("kind", "convert"),
                "title": job.get("title"),
                "filename": job.get("filename"),
                "format": job.get("format"),
                "status": job.get("status"),
                "message": job.get("message"),
                "progress": job.get("progress"),
                "size": job.get("size"),
                "download": job.get("download"),
                "created": job.get("created"),
                "merged_from": job.get("merged_from"),
                "cover": job.get("cover"),
                "split_mb": job.get("split_mb"),
                "auto_chapters": job.get("auto_chapters"),
                "book_id": job.get("book_id"),
                "ttl": job.get("ttl", JOB_TTL),
            }
            _save_history_locked()


def run_conversion(job_id, src_path, fmt, stem, job_dir, nosplit=False,
                   custom_cover=None, split_mb=None, auto_chapters=True,
                   clean_ads=True, title_override=None, author_override=None,
                   chapter_re=None, retention_hours=None):
    """Worker thread: extract metadata, generate cover, split if needed,
    convert each part with metadata/cover/series, zip results.

    Bounded by CONVERT_SEM so at most MAX_CONCURRENT conversions run at once;
    excess jobs wait in a 'queued' state instead of overloading the machine.
    When ``nosplit`` is set, the book is always converted as a single volume
    regardless of size. ``custom_cover`` is an image path used instead of the
    generated cover; ``split_mb`` overrides the split threshold in MB;
    ``clean_ads`` strips novel-site ad lines; ``title_override`` /
    ``author_override`` replace the extracted metadata; ``chapter_re`` is a
    custom chapter-heading regex; ``retention_hours`` overrides the output TTL.
    """
    if not CONVERT_SEM.acquire(blocking=False):
        set_job(job_id, status="running", message="排队中(等待空闲转换名额)...")
        # Cancellable queue wait: re-check the flag every second.
        while not CONVERT_SEM.acquire(timeout=1):
            if _is_cancelled(job_id):
                src_path.unlink(missing_ok=True)
                _finish_job(job_id, status="cancelled", message="已取消")
                return
    try:
        _run_conversion_body(job_id, src_path, fmt, stem, job_dir,
                             nosplit, custom_cover, split_mb, auto_chapters,
                             clean_ads, title_override, author_override,
                             chapter_re, retention_hours)
    finally:
        CONVERT_SEM.release()


def _run_conversion_body(job_id, src_path, fmt, stem, job_dir, nosplit=False,
                         custom_cover=None, split_mb=None, auto_chapters=True,
                         clean_ads=True, title_override=None, author_override=None,
                         chapter_re=None, retention_hours=None):
    try:
        is_txt = src_path.suffix.lower() == ".txt"
        chapter_regex = None
        if chapter_re:
            try:
                chapter_regex = re.compile(chapter_re, re.IGNORECASE)
            except re.error:
                chapter_regex = None

        text = None
        total_chars = 0
        size = src_path.stat().st_size
        inserted_sections = False
        ads_removed = 0

        if is_txt:
            text, enc = read_text_auto(src_path)
            total_chars = len(text)
            # --- Ad/watermark line cleanup (optional) ---
            if clean_ads:
                text, ads_removed = _clean_ad_lines(text)
            # --- Auto-sections: books with no chapter headings at all get
            # synthetic 第N节 markers so TOC and splitting still work. ---
            if auto_chapters and _count_chapters(text, chapter_regex) < 2:
                text = _insert_sections(text)
                inserted_sections = True
                total_chars = len(text)
                size = len(text.encode("utf-8"))
            # --- Metadata (1) ---
            title, author, intro = extract_metadata(text, stem)
        else:
            # ebook input: no text pipeline, title falls back to the filename
            title, author, intro = stem, None, None

        # User overrides win over extracted metadata.
        if title_override and title_override.strip():
            title = title_override.strip()
        if author_override and author_override.strip():
            author = author_override.strip()

        # --- Cover (2): custom image wins, otherwise auto-generate ---
        cover_path = None
        has_cover = False
        if fmt == "txt":
            pass  # plain text output has no cover
        elif custom_cover:
            cover_path = custom_cover
            has_cover = True
            set_job(job_id, cover="custom")
        else:
            cover_path = job_dir / "_cover.jpg"
            has_cover = generate_cover(title, author, cover_path)
            set_job(job_id, cover="auto" if has_cover else "none")

        # Decide split (skipped for ebook inputs and nosplit requests).
        threshold_bytes = (split_mb or (SPLIT_THRESHOLD / (1024 * 1024))) * 1024 * 1024
        if not is_txt:
            parts = [None]
        elif nosplit:
            parts = [text]
        elif size > threshold_bytes:
            parts = split_by_size(text, int(threshold_bytes), chapter_regex)
            if len(parts) == 1:
                # Big file but no recognized chapters: silently converting it
                # whole would be slow and produce a sluggish book, so warn.
                set_job(job_id, warning=(
                    "文件超过拆分阈值但未识别到章节,已按整本转换;"
                    "超大文件转换可能很慢,Kindle 上也可能卡顿"))
        else:
            parts = [text]

        n = len(parts)
        meta_msg = f"《{title}》"
        if author:
            meta_msg += f" · {author}"
        if inserted_sections:
            meta_msg += " · 已自动分节"
        if ads_removed:
            meta_msg += f" · 清理广告 {ads_removed} 行"
        sec_note = " · 已自动分节" if inserted_sections else ""
        ads_note = f" · 清理广告 {ads_removed} 行" if ads_removed else ""
        set_job(job_id, status="running", total_parts=n, done_parts=0,
                title=title, author=author or "",
                message=meta_msg + " · " +
                        (f"{total_chars:,} 字" if is_txt else f"{size // 1024:,} KB") +
                        (f" · 拆分为 {n} 个部分" if n > 1 else ""))

        ext = FORMATS[fmt]
        build_ext = CONVERT_EXT[fmt]
        out_files = []

        for idx, part_text in enumerate(parts, 1):
            if n > 1:
                part_stem = f"{stem}_{idx:02d}"
                # (3) Per-part title carries the sequence: "书名 (1/5)"
                part_title = f"{title} ({idx}/{n})"
            else:
                part_stem = stem
                part_title = title
            if part_text is None:
                # ebook input: reuse the uploaded source file directly
                part_txt = src_path
            else:
                part_txt = job_dir / (part_stem + ".txt")
                part_txt.write_text(part_text, encoding="utf-8")

            if fmt == "txt" and is_txt:
                # TXT in -> TXT out: no conversion needed, hand back the
                # processed text (ad-cleaned / auto-sectioned / split).
                out_files.append(part_txt)
                pct = int(idx / n * 95)
                set_job(job_id, done_parts=idx, progress=pct)
                continue

            build_path = job_dir / (part_stem + "." + build_ext)
            out_path = job_dir / (part_stem + "." + ext)

            cmd = [EBOOK_CONVERT, str(part_txt), str(build_path)]
            if is_txt:
                cmd += ["--input-encoding", "utf-8"]

            # --- Metadata (1) + split numbering (3) ---
            cmd += ["--title", part_title, "--language", "zh"]
            if author:
                cmd += ["--authors", author]
            if intro:
                cmd += ["--comments", intro]
            if n > 1:
                # Series ensures correct ordering on the Kindle bookshelf.
                cmd += ["--series", title, "--series-index", str(idx)]
            # --- Cover (2) ---
            if has_cover:
                cmd += ["--cover", str(cover_path)]

            # --- Chapters / TOC (4) ---
            part_size = part_txt.stat().st_size
            if not is_txt:
                # ebook input: let Calibre keep its own chapter/TOC detection
                pass
            elif chapter_regex is not None:
                # custom chapter rule: pass the regex to Calibre when it is
                # supported; otherwise fall back to the default XPath (the
                # custom rule still drives splitting and preview stats)
                cmd += ["--chapter-mark", "pagebreak"]
                if SUPPORTS_CHAPTER_PATTERN:
                    cmd += ["--chapter-pattern", chapter_re]
                else:
                    cmd += ["--chapter", CHAPTER_XPATH]
                cmd += ["--level1-toc", CHAPTER_XPATH,
                        "--max-toc-links", "5000",
                        "--toc-threshold", "1"]
            elif part_size <= HEURISTICS_LIMIT:
                cmd.append("--enable-heuristics")
                cmd += ["--level1-toc", CHAPTER_XPATH,
                        "--max-toc-links", "5000",
                        "--toc-threshold", "1"]
            else:
                cmd += ["--chapter", CHAPTER_XPATH, "--chapter-mark", "pagebreak"]
                # Build a navigable TOC from the detected chapters (depth 1) and
                # ensure the reader's TOC is populated even for plain text.
                cmd += ["--level1-toc", CHAPTER_XPATH,
                        "--max-toc-links", "5000",
                        "--toc-threshold", "1"]

            set_job(job_id, current=idx,
                    message=f"正在转换第 {idx}/{n} 部分..."
                            + (sec_note if idx == 1 else ""))

            # --- Convert (5): run ebook-convert for this part (cancellable) ---
            timeout_s = max(480, int(part_size / (3 * 1024 * 1024) * 300) + 300)
            rc, out, err, timed_out, cancelled = _run_proc_cancellable(
                job_id, cmd, timeout_s)
            if timed_out:
                _finish_job(job_id, status="error",
                            message=f"第 {idx} 部分转换超时,文件可能过大")
                return
            if cancelled:
                _finish_job(job_id, status="cancelled", message="已取消")
                return
            if rc != 0 or not build_path.is_file():
                tail = (err or out or b"").decode("utf-8", "replace")[-1200:]
                _finish_job(job_id, status="error",
                            message=f"第 {idx} 部分转换失败", detail=tail)
                return

            # --- KFX (6): Kindle Previewer turns the intermediate MOBI into
            # KFX when requested (Amazon-only format, optional install). ---
            if fmt == "kfx":
                if KINDLE_PREVIEWER is None:
                    _finish_job(job_id, status="error",
                                message="未找到 Kindle Previewer,无法生成 KFX。"
                                        "请安装 Amazon Kindle Previewer 3")
                    return
                kfx_dir = job_dir / f"{part_stem}_kfx"
                kfx_dir.mkdir(exist_ok=True)
                set_job(job_id, message=f"正在生成 KFX(第 {idx}/{n} 部分)...")
                rc2, out2, err2, to2, cc2 = _run_proc_cancellable(
                    job_id,
                    [KINDLE_PREVIEWER, str(build_path), "-o", str(kfx_dir)],
                    max(timeout_s, 600))
                if cc2:
                    _finish_job(job_id, status="cancelled", message="已取消")
                    return
                if to2 or rc2 != 0:
                    tail = (err2 or out2 or b"").decode("utf-8", "replace")[-1200:]
                    _finish_job(job_id, status="error",
                                message=f"第 {idx} 部分 KFX 生成失败", detail=tail)
                    return
                kfx_file = next(kfx_dir.rglob("*.kfx"), None)
                if kfx_file is None:
                    _finish_job(job_id, status="error",
                                message=f"第 {idx} 部分未找到 KFX 输出文件",
                                detail=(err2 or out2 or b"").decode("utf-8", "replace")[-1200:])
                    return
                build_path.unlink(missing_ok=True)  # drop the intermediate MOBI
                build_path = kfx_file

            if build_path != out_path:
                if out_path.exists():
                    out_path.unlink()
                build_path.rename(out_path)
            if fmt == "kfx":
                # Keep Previewer sidecar files (cover thumbnails etc.): kfx_dir
                # stays inside the job dir and is removed with it after the
                # retention period.
                pass

            out_files.append(out_path)
            # progress: leave last 5% for packaging
            pct = int(idx / n * 95)
            set_job(job_id, done_parts=idx, progress=pct)

            # Clean the part txt to save space
            part_txt.unlink(missing_ok=True)

        # Clean intermediate files to save disk (7): source txt + cover.
        src_path.unlink(missing_ok=True)
        if cover_path:
            cover_path.unlink(missing_ok=True)

        # Package result
        if len(out_files) == 1:
            final = out_files[0]
            _finish_job(job_id, status="done", progress=100,
                        download=f"/download/{job_id}/{quote(final.name)}",
                        filename=final.name,
                        size=final.stat().st_size,
                        is_zip=False,
                        message="转换完成" + sec_note + ads_note)
        else:
            zip_name = f"{stem}_{fmt}_{len(out_files)}部分.zip"
            zip_path = job_dir / zip_name
            set_job(job_id, message="正在打包 ZIP...", progress=97)
            with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
                for f in out_files:
                    zf.write(f, f.name)
            _finish_job(job_id, status="done", progress=100,
                        download=f"/download/{job_id}/{quote(zip_name)}",
                        filename=zip_name,
                        size=zip_path.stat().st_size,
                        is_zip=True,
                        parts_count=len(out_files),
                        message=f"转换完成,共 {len(out_files)} 个文件" + sec_note + ads_note)
    except subprocess.TimeoutExpired:
        _finish_job(job_id, status="error", message="转换超时,文件可能过大")
    except Exception as e:
        _finish_job(job_id, status="error", message=f"内部错误: {e}")


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, body, ctype="text/html; charset=utf-8", extra=None):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        # never cache pages/APIs so UI updates show up immediately
        self.send_header("Cache-Control", "no-store")
        if extra:
            for k, v in extra.items():
                self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code, obj):
        self._send(code, json.dumps(obj), "application/json; charset=utf-8")

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            self._send(200, INDEX_HTML)
        elif self.path == "/health":
            self._json(200, {"ok": EBOOK_CONVERT is not None,
                             "ebook_convert": EBOOK_CONVERT or "NOT FOUND",
                             "kindle_previewer": KINDLE_PREVIEWER or None})
        elif self.path == "/jobs":
            self.handle_jobs()
        elif self.path == "/config":
            self.handle_get_config()
        elif self.path == "/bg/list":
            self.handle_bg_list()
        elif self.path == "/sources":
            self.handle_sources()
        elif self.path == "/read/recent":
            self.handle_read_recent()
        elif self.path.split("?", 1)[0] == "/read/chapter":
            self.handle_read_chapter()
        elif self.path.startswith("/backgrounds/"):
            self.handle_background()
        elif self.path.startswith("/progress/"):
            self.handle_progress()
        elif self.path.startswith("/download/"):
            self.handle_download()
        else:
            self._send(404, "Not found")

    def handle_progress(self):
        job_id = self.path.split("/", 2)[2]
        with JOBS_LOCK:
            job = JOBS.get(job_id)
            snap = dict(job) if job else None
        if snap is None:
            self._json(404, {"error": "job not found"})
            return
        self._json(200, snap)

    def handle_jobs(self):
        """Return job history: persisted entries from previous runs plus the
        live in-memory jobs (newest first). Live entries override history."""
        with JOBS_LOCK:
            by_id = {jid: dict(e) for jid, e in HISTORY.items()}
            for jid, j in JOBS.items():
                by_id[jid] = {
                    "job_id": jid,
                    "kind": j.get("kind", "convert"),
                    "title": j.get("title"),
                    "filename": j.get("filename"),
                    "format": j.get("format"),
                    "status": j.get("status"),
                    "message": j.get("message"),
                    "progress": j.get("progress"),
                    "size": j.get("size"),
                    "download": j.get("download"),
                    "created": j.get("created"),
                    "merged_from": j.get("merged_from"),
                    "cover": j.get("cover"),
                    "split_mb": j.get("split_mb"),
                    "auto_chapters": j.get("auto_chapters"),
                    "book_id": j.get("book_id"),
                }
        out = []
        for it in by_id.values():
            it["downloadable"] = False
            if it.get("status") == "done" and it.get("download"):
                parts = it["download"].split("/")
                if len(parts) == 4 and parts[1] == "download":
                    for base in output_dirs():
                        if (base / parts[2] / parts[3]).is_file():
                            it["downloadable"] = True
                            break
            out.append(it)
        out.sort(key=lambda x: x.get("created") or 0, reverse=True)
        self._json(200, {"jobs": out})

    def handle_cancel(self):
        """Ask a running/queued job to stop. The worker checks the flag while
        waiting for a queue slot and while ebook-convert runs."""
        try:
            length = int(self.headers.get("Content-Length", 0))
        except ValueError:
            length = 0
        data = {}
        if 0 < length < 65536:
            try:
                data = json.loads(self.rfile.read(length).decode("utf-8", "replace"))
            except Exception:
                data = {}
        job_id = data.get("job_id") or ""
        with JOBS_LOCK:
            job = JOBS.get(job_id)
            if job is None:
                self._json(404, {"error": "job not found"})
                return
            if job.get("status") not in ("queued", "running"):
                self._json(400, {"error": "任务不在进行中"})
                return
            job["cancel_requested"] = True
        self._json(200, {"ok": True})

    def handle_clear_history(self):
        """Manually clear the conversion history.

        mode="records": remove the records only (output files kept).
        mode="all": also delete the output files/dirs (running jobs untouched).
        """
        try:
            length = int(self.headers.get("Content-Length", 0))
        except ValueError:
            length = 0
        data = {}
        if 0 < length < 65536:
            try:
                data = json.loads(self.rfile.read(length).decode("utf-8", "replace"))
            except Exception:
                data = {}
        mode = data.get("mode")
        if mode not in ("records", "all"):
            self._json(400, {"error": "mode 必须是 records 或 all"})
            return
        freed = 0
        with JOBS_LOCK:
            ids = list(HISTORY.keys())
            # Records are terminal jobs, so they are also safe to drop from the
            # live registry; running/queued jobs are never touched.
            for jid in ids:
                JOBS.pop(jid, None)
            if mode == "all":
                for jid in ids:
                    for base in output_dirs():
                        d = base / jid
                        if d.is_dir():
                            try:
                                freed += sum(f.stat().st_size
                                            for f in d.rglob("*") if f.is_file())
                            except OSError:
                                pass
                            shutil.rmtree(d, ignore_errors=True)
                # orphan dirs not referenced by any live job (crashed leftovers).
                # Only job dirs (32-hex names) qualify - never touch unrelated
                # user folders that may live inside a custom output directory.
                live = set(JOBS.keys())
                for base in output_dirs():
                    for d in base.iterdir():
                        if (d.is_dir() and _is_job_dir_name(d.name)
                                and (d / ".txt2ebook_job").is_file()
                                and d.name not in live):
                            try:
                                freed += sum(f.stat().st_size
                                            for f in d.rglob("*") if f.is_file())
                            except OSError:
                                pass
                            shutil.rmtree(d, ignore_errors=True)
            for jid in ids:
                HISTORY.pop(jid, None)
            _save_history_locked()
        self._json(200, {"ok": True, "cleared": len(ids),
                         "freed_mb": round(freed / 1048576, 1)})

    def handle_download(self):
        parts = self.path.split("/", 3)
        if len(parts) < 4:
            self._send(400, "Bad request")
            return
        token, fname = parts[2], parts[3]
        from urllib.parse import unquote
        fname = unquote(fname)
        if not token.isalnum() or "/" in fname or "\\" in fname or ".." in fname:
            self._send(400, "Bad request")
            return
        fpath = None
        for base in output_dirs():
            cand = base / token / fname
            if cand.is_file():
                fpath = cand
                break
        if fpath is None:
            self._send(404, "File not found")
            return
        ctype = mimetypes.guess_type(str(fpath))[0] or "application/octet-stream"
        from urllib.parse import quote
        size = fpath.stat().st_size
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(size))
        self.send_header(
            "Content-Disposition",
            f"attachment; filename*=UTF-8''{quote(fname)}")
        self.end_headers()
        with open(fpath, "rb") as fh:
            shutil.copyfileobj(fh, self.wfile, length=1024 * 1024)

    def do_POST(self):
        if self.path == "/merge":
            self.handle_merge()
        elif self.path == "/preview":
            self.handle_preview()
        elif self.path == "/convert":
            self.handle_convert()
        elif self.path == "/cancel":
            self.handle_cancel()
        elif self.path == "/clear_history":
            self.handle_clear_history()
        elif self.path == "/config":
            self.handle_set_config()
        elif self.path == "/sources":
            self.handle_sources_save()
        elif self.path == "/sources/delete":
            self.handle_sources_delete()
        elif self.path == "/sources/probe":
            self.handle_sources_probe()
        elif self.path == "/grab":
            self.handle_grab()
        elif self.path == "/convert_lib":
            self.handle_convert_lib()
        elif self.path == "/bg/upload":
            self.handle_bg_upload()
        elif self.path == "/bg/delete":
            self.handle_bg_delete()
        elif self.path == "/bg/select":
            self.handle_bg_select()
        elif self.path == "/read/open":
            self.handle_read_open()
        elif self.path == "/read/reopen":
            self.handle_read_reopen()
        elif self.path == "/read/recent":
            self.handle_read_recent()
        elif self.path == "/read/progress":
            self.handle_read_progress()
        elif self.path == "/read/bookmark":
            self.handle_read_bookmark()
        elif self.path == "/read/bookmark_remove":
            self.handle_read_bookmark_remove()
        elif self.path == "/read/delete":
            self.handle_read_delete()
        elif self.path == "/read/clear_recent":
            self.handle_read_clear_recent()
        else:
            self._send(404, "Not found")

    def _read_upload(self):
        """Validate the multipart request and stream all files into a fresh
        job_dir. Returns (job_id, job_dir, fields, files) on success, or None
        after already sending an error response.
        """
        ctype = self.headers.get("Content-Type", "")
        if "multipart/form-data" not in ctype:
            self._json(400, {"error": "Expected multipart/form-data"})
            return None
        boundary = None
        for part in ctype.split(";"):
            part = part.strip()
            if part.startswith("boundary="):
                boundary = part[9:].strip('"')
        if not boundary:
            self._json(400, {"error": "Missing boundary"})
            return None
        try:
            length = int(self.headers.get("Content-Length", 0))
        except ValueError:
            length = 0
        if length <= 0:
            self._json(400, {"error": "Empty body"})
            return None
        if length > UPLOAD_MAX_BYTES:
            self._json(413, {"error":
                f"文件过大(上限 {UPLOAD_MAX_BYTES // (1024*1024)} MB)"})
            return None

        job_id = uuid.uuid4().hex
        job_dir = output_base() / job_id
        job_dir.mkdir(parents=True, exist_ok=True)
        try:
            (job_dir / ".txt2ebook_job").write_text("1")
        except OSError:
            pass
        try:
            fields, files = stream_multipart(
                self.rfile, length, boundary.encode("utf-8"), job_dir)
        except Exception as e:
            shutil.rmtree(job_dir, ignore_errors=True)
            self._json(400, {"error": f"解析上传失败: {e}"})
            return None

        files = [f for f in files if f["filename"]
                 and f["path"].is_file() and f["path"].stat().st_size > 0]
        # Separate the optional custom cover from the TXT files.
        covers = [f for f in files if f.get("name") == "cover"]
        files = [f for f in files if f.get("name") != "cover"]

        # Archives: extract them and use the book files inside instead.
        expanded = []
        for f in files:
            if Path(f["filename"]).suffix.lower() in ARCHIVE_EXTS:
                try:
                    inner = _extract_archive(
                        f["path"], job_dir / ("arc_" + f["path"].stem))
                except ArchiveError as e:
                    shutil.rmtree(job_dir, ignore_errors=True)
                    self._json(400, {"error": str(e)})
                    return None
                for p in inner:
                    expanded.append({"path": p, "filename": p.name, "name": "file"})
            else:
                expanded.append(f)
        files = expanded

        if not files:
            shutil.rmtree(job_dir, ignore_errors=True)
            self._json(400, {"error": "No file uploaded"})
            return None
        # Accept TXT plus ebook inputs (epub/mobi/azw/azw3) for format
        # conversion; anything else is rejected before reaching ebook-convert.
        bad = [f["filename"] for f in files
               if Path(f["filename"]).suffix.lower() not in INPUT_EXTS]
        if bad:
            shutil.rmtree(job_dir, ignore_errors=True)
            shown = ", ".join(bad[:3]) + (" 等" if len(bad) > 3 else "")
            self._json(400, {"error": f"仅支持 TXT / EPUB / MOBI / AZW / AZW3 / ZIP / RAR / 7Z 文件: {shown}"})
            return None
        # Custom cover: must be a non-empty image; move it into the job dir.
        cover_path = None
        if covers:
            cv = covers[0]
            suffix = Path(cv["filename"]).suffix.lower()
            if suffix not in COVER_EXTS:
                shutil.rmtree(job_dir, ignore_errors=True)
                self._json(400, {"error": "封面仅支持 JPG / PNG / WEBP 图片"})
                return None
            cover_path = job_dir / ("_cover_custom" + suffix)
            cv["path"].replace(cover_path)
        return job_id, job_dir, fields, files, cover_path

    @staticmethod
    def _ordered_files(files, order_raw):
        """Order files by the UI-supplied index list, else natural sort (方案 A)."""
        order_raw = (order_raw or "").strip()
        if order_raw:
            try:
                idx_order = [int(x) for x in order_raw.split(",") if x != ""]
                if sorted(idx_order) == list(range(len(files))):
                    return [files[i] for i in idx_order]
            except ValueError:
                pass
        return sorted(files, key=lambda f: natural_sort_key(f["filename"]))

    def handle_preview(self):
        """Pre-conversion info: detected title/author, stats and split estimate.
        Uploaded files are discarded afterwards (no job is created)."""
        got = self._read_upload()
        if got is None:
            return
        job_id, job_dir, fields, files, cover_path = got
        try:
            clean_ads = fields.get("clean_ads", "1").strip().lower() \
                not in ("0", "false", "off", "no", "")
            auto_chapters = fields.get("auto_chapters", "1").strip().lower() \
                not in ("0", "false", "off", "no", "")
            nosplit = fields.get("nosplit", "").strip() in ("1", "true", "on", "yes")
            chapter_re = fields.get("chapter_re", "").strip()
            chapter_regex = None
            if chapter_re:
                try:
                    chapter_regex = re.compile(chapter_re, re.IGNORECASE)
                except re.error:
                    chapter_regex = None
            split_mb = None
            raw_mb = fields.get("split_mb", "").strip()
            if raw_mb:
                try:
                    split_mb = min(max(float(raw_mb), 1.0), 500.0)
                except ValueError:
                    split_mb = None

            files = self._ordered_files(files, fields.get("order"))
            if not all(f["filename"].lower().endswith(".txt") for f in files):
                f0 = files[0]
                stem = Path(os.path.basename(f0["filename"])).stem or "book"
                self._json(200, {"ok": True, "non_txt": True, "title": stem,
                                 "ext": Path(f0["filename"]).suffix.lower().lstrip(".")})
                return
            if len(files) == 1:
                stem = Path(os.path.basename(files[0]["filename"])).stem or "book"
                src = files[0]["path"]
            else:
                first_stem = Path(os.path.basename(files[0]["filename"])).stem
                stem = first_stem or "book"
                src = job_dir / (stem + ".src.txt")
                merge_txt_files([f["path"] for f in files], src)

            text, _ = read_text_auto(src)
            ads_removed = 0
            if clean_ads:
                text, ads_removed = _clean_ad_lines(text)
            title, author, intro = extract_metadata(text, stem)
            chapters = _count_chapters(text, chapter_regex)
            auto_sections = False
            if auto_chapters and chapters < 2:
                text2 = _insert_sections(text)
                chapters = _count_chapters(text2, chapter_regex)
                auto_sections = True
            size = len(text.encode("utf-8"))
            threshold = (split_mb or (SPLIT_THRESHOLD / (1024 * 1024))) * 1024 * 1024
            if nosplit:
                parts_n = 1
            elif size > threshold:
                parts_n = max(1, len(split_by_size(text, int(threshold), chapter_regex)))
            else:
                parts_n = 1
            self._json(200, {"ok": True, "non_txt": False, "title": title,
                             "author": author or "", "intro": (intro or "")[:300],
                             "chars": len(text), "chapters": chapters,
                             "parts": parts_n, "ads_removed": ads_removed,
                             "auto_sections": auto_sections})
        except Exception as e:
            self._json(400, {"error": f"预览失败: {e}"})
        finally:
            shutil.rmtree(job_dir, ignore_errors=True)

    def handle_get_config(self):
        """Return the current output directory + LAN-access setting."""
        self._json(200, {"ok": True,
                         "output_dir": CONFIG.get("output_dir") or "",
                         "effective": str(output_base()),
                         "lan": CONFIG.get("host") == "0.0.0.0",
                         "lan_ip": (lan_ip()
                                    if CONFIG.get("host") == "0.0.0.0"
                                    else None)})

    def handle_set_config(self):
        """Change the output directory (absolute path; empty = default)."""
        try:
            length = int(self.headers.get("Content-Length", 0))
        except ValueError:
            length = 0
        data = {}
        if 0 < length < 65536:
            try:
                data = json.loads(self.rfile.read(length).decode("utf-8", "replace"))
            except Exception:
                data = {}
        od = (data.get("output_dir") or "").strip().strip('"')
        lan = data.get("lan")
        if lan is not None:
            # LAN access toggle: needs a server restart to take effect (the
            # socket bind happens in main()). Store it and tell the UI.
            lan_on = (lan is True
                      or str(lan).strip().lower() in ("1", "true", "on", "yes"))
            if lan_on:
                CONFIG["host"] = "0.0.0.0"
            else:
                CONFIG.pop("host", None)
            save_config()
            self._json(200, {"ok": True, "restart": True,
                             "lan": lan_on,
                             "lan_ip": lan_ip() if lan_on else None})
            return
        if od:
            p = Path(od).expanduser()
            if not p.is_absolute():
                self._json(400, {"error": "请填写绝对路径,例如 D:\\Books\\输出"})
                return
            try:
                p.mkdir(parents=True, exist_ok=True)
                probe = p / ".write_test"
                probe.write_text("ok")
                probe.unlink(missing_ok=True)
            except Exception as e:
                self._json(400, {"error": f"目录不可用: {e}"})
                return
            CONFIG["output_dir"] = str(p)
        else:
            CONFIG.pop("output_dir", None)
        save_config()
        self._json(200, {"ok": True,
                         "output_dir": CONFIG.get("output_dir") or "",
                         "effective": str(output_base())})

    def handle_bg_list(self):
        self._json(200, {"ok": True, "bgs": _load_bg_meta(),
                         "selected": CONFIG.get("selected_bg")})

    def handle_bg_upload(self):
        """Store an uploaded background image (client already downscaled it)."""
        ctype = self.headers.get("Content-Type", "")
        boundary = None
        for part in ctype.split(";"):
            part = part.strip()
            if part.startswith("boundary="):
                boundary = part[9:].strip('"')
        try:
            length = int(self.headers.get("Content-Length", 0))
        except ValueError:
            length = 0
        if not boundary or length <= 0:
            self._json(400, {"error": "Bad upload"})
            return
        # Pre-parse guard: must at least fit the largest allowed type (video).
        # The format-specific limit (20MB image / 200MB video) is enforced
        # per-file AFTER parsing, so video uploads aren't cut off mid-request.
        if length > 205 * 1024 * 1024:
            self._json(413, {"error": "文件过大(上限 200MB)"})
            return
        tmp = BG_DIR / "_tmp"
        tmp.mkdir(exist_ok=True)
        try:
            fields, files = stream_multipart(self.rfile, length,
                                             boundary.encode("utf-8"), tmp)
        except Exception as e:
            shutil.rmtree(tmp, ignore_errors=True)
            self._json(400, {"error": f"解析失败: {e}"})
            return
        imgs = [f for f in files if f["filename"] and f["path"].is_file()]
        if not imgs:
            shutil.rmtree(tmp, ignore_errors=True)
            self._json(400, {"error": "未收到图片"})
            return
        f = imgs[0]
        suffix = Path(f["filename"]).suffix.lower()
        is_video = suffix in BG_VIDEO_EXTS
        if not is_video and suffix not in BG_EXTS:
            shutil.rmtree(tmp, ignore_errors=True)
            self._json(400, {"error": "仅支持 JPG / PNG / WEBP / GIF / MP4 / WEBM"})
            return
        limit = 200 * 1024 * 1024 if is_video else 20 * 1024 * 1024
        if f["path"].stat().st_size > limit:
            shutil.rmtree(tmp, ignore_errors=True)
            self._json(413, {"error": "图片过大(上限 20MB)" if not is_video
                             else "视频过大(上限 200MB)"})
            return
        bid = uuid.uuid4().hex[:12]
        out = BG_DIR / (bid + suffix)
        f["path"].replace(out)
        shutil.rmtree(tmp, ignore_errors=True)
        meta = _load_bg_meta()
        meta.append({"id": bid, "name": Path(f["filename"]).name,
                     "kind": "video" if is_video else "image",
                     "size": out.stat().st_size, "created": time.time()})
        meta = meta[-50:]
        _save_bg_meta(meta)
        CONFIG["selected_bg"] = bid
        save_config()
        self._json(200, {"ok": True, "id": bid, "bgs": meta,
                         "selected": bid})

    def handle_bg_delete(self):
        try:
            length = int(self.headers.get("Content-Length", 0))
        except ValueError:
            length = 0
        data = {}
        if 0 < length < 65536:
            try:
                data = json.loads(self.rfile.read(length).decode("utf-8", "replace"))
            except Exception:
                data = {}
        ids = set(data.get("ids") or [])
        delete_files = data.get("delete_files", True)
        meta = _load_bg_meta()
        kept = [m for m in meta if m["id"] not in ids]
        if delete_files:
            for m in meta:
                if m["id"] in ids:
                    for suffix in BG_EXTS + BG_VIDEO_EXTS:
                        (BG_DIR / (m["id"] + suffix)).unlink(missing_ok=True)
        _save_bg_meta(kept)
        if CONFIG.get("selected_bg") in ids:
            CONFIG.pop("selected_bg", None)
            save_config()
        self._json(200, {"ok": True, "bgs": kept,
                         "selected": CONFIG.get("selected_bg")})

    def handle_bg_select(self):
        try:
            length = int(self.headers.get("Content-Length", 0))
        except ValueError:
            length = 0
        data = {}
        if 0 < length < 65536:
            try:
                data = json.loads(self.rfile.read(length).decode("utf-8", "replace"))
            except Exception:
                data = {}
        bid = data.get("id") or ""
        if bid and any(m["id"] == bid for m in _load_bg_meta()):
            CONFIG["selected_bg"] = bid
        else:
            CONFIG.pop("selected_bg", None)
        save_config()
        self._json(200, {"ok": True, "selected": CONFIG.get("selected_bg")})

    def handle_background(self):
        """Serve a stored background file by id (videos support Range)."""
        bid = self.path.split("/", 2)[2]
        if not re.fullmatch(r"[0-9a-f]{12}", bid):
            self._send(404, "Not found")
            return
        for suffix in BG_EXTS + BG_VIDEO_EXTS:
            p = BG_DIR / (bid + suffix)
            if not p.is_file():
                continue
            ctype = mimetypes.guess_type(str(p))[0] or "application/octet-stream"
            size = p.stat().st_size
            rng = self.headers.get("Range")
            if rng and ctype.startswith("video/"):
                m = re.match(r"bytes=(\d*)-(\d*)\s*$", rng.strip())
                if not m or (not m.group(1) and not m.group(2)):
                    self.send_response(416)
                    self.send_header("Content-Range", f"bytes */{size}")
                    self.end_headers()
                    return
                start = int(m.group(1)) if m.group(1) else None
                end = int(m.group(2)) if m.group(2) else None
                if start is None:
                    # suffix range "bytes=-N": serve the last N bytes
                    start = max(0, size - end)
                    end = size - 1
                if end is None or end >= size:
                    end = size - 1
                if start > end or start >= size:
                    self.send_response(416)
                    self.send_header("Content-Range", f"bytes */{size}")
                    self.end_headers()
                    return
                length = end - start + 1
                self.send_response(206)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(length))
                self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
                self.send_header("Accept-Ranges", "bytes")
                self.send_header("Cache-Control", "max-age=86400")
                self.end_headers()
                with open(p, "rb") as fh:
                    fh.seek(start)
                    remaining = length
                    while remaining > 0:
                        chunk = fh.read(min(1024 * 1024, remaining))
                        if not chunk:
                            break
                        self.wfile.write(chunk)
                        remaining -= len(chunk)
                return
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(size))
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Cache-Control", "max-age=86400")
            self.end_headers()
            with open(p, "rb") as fh:
                shutil.copyfileobj(fh, self.wfile, length=1024 * 1024)
            return
        self._send(404, "Not found")

    def handle_read_open(self):
        """Open a local TXT (or archive of TXT); cleaned + chapters. Books are
        stored in the local library so reopening resumes at the last chapter."""
        got = self._read_upload()
        if got is None:
            return
        job_id, job_dir, fields, files, cover_path = got
        try:
            files = self._ordered_files(files, fields.get("order"))
            txts = [f for f in files
                    if f["filename"].lower().endswith(".txt")]
            if not txts:
                self._json(400, {"error": "阅读仅支持 TXT(或内含 TXT 的压缩包)"})
                return
            if len(txts) == 1:
                text, _ = read_text_auto(txts[0]["path"])
                stem = Path(os.path.basename(txts[0]["filename"])).stem or "未命名"
            else:
                stem = Path(os.path.basename(txts[0]["filename"])).stem or "未命名"
                src2 = job_dir / "read_src.txt"
                merge_txt_files([f["path"] for f in txts], src2)
                text, _ = read_text_auto(src2)
            title, _author, _intro = extract_metadata(text, stem)
            book_id = hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()[:16]
            sid, resume = _open_read_session(title or stem, text, book_id)
            s = READ_SESSIONS[sid]
            state = _load_read_state().get(book_id, {})
            self._json(200, {"ok": True, "sid": sid, "title": s["title"],
                             "chapters": len(s["chapters"]),
                             "chapter_titles": [c["title"] for c in s["chapters"]],
                             "book_id": book_id,
                             "resume_chapter": resume,
                             "bookmarks": state.get("bookmarks", [])})
        except Exception as e:
            self._json(400, {"error": f"打开失败: {e}"})
        finally:
            shutil.rmtree(job_dir, ignore_errors=True)

    def handle_read_reopen(self):
        """Reopen a previously read book from the local library by id."""
        data = self._read_json_body()
        book_id = (data.get("book_id") or "").strip()
        if not re.fullmatch(r"[0-9a-f]{16}", book_id):
            self._json(400, {"error": "无效的书本 ID"})
            return
        if not (READ_LIB / book_id / "content.txt").is_file():
            self._json(404, {"error": "本地库中找不到这本书"})
            return
        try:
            sid, resume = _open_read_session(None, None, book_id)
            s = READ_SESSIONS[sid]
            state = _load_read_state().get(book_id, {})
            self._json(200, {"ok": True, "sid": sid, "title": s["title"],
                             "chapters": len(s["chapters"]),
                             "chapter_titles": [c["title"] for c in s["chapters"]],
                             "book_id": book_id,
                             "resume_chapter": resume,
                             "bookmarks": state.get("bookmarks", [])})
        except Exception as e:
            self._json(400, {"error": f"打开失败: {e}"})

    def handle_read_recent(self):
        state = _load_read_state()
        items = sorted(state.items(),
                       key=lambda kv: kv[1].get("opened", 0), reverse=True)
        out = [{"book_id": bid, "title": v.get("title", "未命名"),
                "chapter": v.get("chapter", 0),
                "bookmarks": v.get("bookmarks", []),
                "opened": v.get("opened", 0)}
               for bid, v in items[:12]]
        self._json(200, {"ok": True, "books": out})

    def handle_read_progress(self):
        data = self._read_json_body()
        book_id = (data.get("book_id") or "").strip()
        try:
            chapter = int(data.get("chapter", 0))
        except (TypeError, ValueError):
            chapter = 0
        if re.fullmatch(r"[0-9a-f]{16}", book_id) and chapter >= 0:
            state = _load_read_state()
            if book_id in state:
                state[book_id]["chapter"] = chapter
                _save_read_state(state)
        self._json(200, {"ok": True})

    def handle_read_bookmark(self):
        data = self._read_json_body()
        book_id = (data.get("book_id") or "").strip()
        try:
            chapter = int(data.get("chapter", 0))
        except (TypeError, ValueError):
            chapter = 0
        if re.fullmatch(r"[0-9a-f]{16}", book_id) and chapter >= 0:
            state = _load_read_state()
            entry = state.setdefault(book_id, {"title": "未命名", "bookmarks": []})
            bm = [b for b in entry.get("bookmarks", []) if b.get("chapter") != chapter]
            bm.append({"chapter": chapter, "ts": time.time()})
            entry["bookmarks"] = bm[-20:]
            _save_read_state(state)
        self._json(200, {"ok": True})

    def handle_read_bookmark_remove(self):
        data = self._read_json_body()
        book_id = (data.get("book_id") or "").strip()
        try:
            chapter = int(data.get("chapter", 0))
        except (TypeError, ValueError):
            chapter = -1
        if re.fullmatch(r"[0-9a-f]{16}", book_id):
            state = _load_read_state()
            entry = state.get(book_id)
            if entry:
                entry["bookmarks"] = [b for b in entry.get("bookmarks", [])
                                      if b.get("chapter") != chapter]
                _save_read_state(state)
        self._json(200, {"ok": True})

    def handle_read_delete(self):
        data = self._read_json_body()
        book_id = (data.get("book_id") or "").strip()
        if re.fullmatch(r"[0-9a-f]{16}", book_id):
            shutil.rmtree(READ_LIB / book_id, ignore_errors=True)
            state = _load_read_state()
            state.pop(book_id, None)
            _save_read_state(state)
        self._json(200, {"ok": True})

    def handle_read_clear_recent(self):
        """Clear all reading history and the whole local library."""
        shutil.rmtree(READ_LIB, ignore_errors=True)
        READ_LIB.mkdir(parents=True, exist_ok=True)
        try:
            READ_STATE_FILE.unlink(missing_ok=True)
        except OSError:
            pass
        with JOBS_LOCK:
            for sid in [s for s, v in list(READ_SESSIONS.items())]:
                READ_SESSIONS.pop(sid, None)
        self._json(200, {"ok": True})

    def _read_json_body(self):
        try:
            length = int(self.headers.get("Content-Length", 0))
        except ValueError:
            length = 0
        if 0 < length < 65536:
            try:
                return json.loads(self.rfile.read(length).decode("utf-8", "replace"))
            except Exception:
                pass
        return {}
    def handle_read_chapter(self):
        from urllib.parse import parse_qs, urlparse
        q = parse_qs(urlparse(self.path).query)
        sid = (q.get("s") or [""])[0]
        try:
            n = int((q.get("n") or ["0"])[0])
        except ValueError:
            n = 0
        if not re.fullmatch(r"[0-9a-f]{12}", sid):
            self._json(404, {"error": "session not found"})
            return
        s = READ_SESSIONS.get(sid)
        if s is None:
            self._json(404, {"error": "阅读会话已过期,请重新打开"})
            return
        if not (0 <= n < len(s["chapters"])):
            self._json(404, {"error": "章节不存在"})
            return
        if s.get("book_id"):
            state = _load_read_state()
            if s["book_id"] in state:
                state[s["book_id"]]["chapter"] = n
                _save_read_state(state)
        self._json(200, {"ok": True, "title": s["title"], "chapter": n,
                         "chapter_title": s["chapters"][n]["title"],
                         "total": len(s["chapters"]),
                         "text": s["chapters"][n]["text"]})
    def handle_merge(self):
        """Merge 1..N uploaded TXT files into a single TXT and return it for
        download. No conversion, no Calibre, no job/progress - merging is fast
        enough to answer synchronously.
        """
        got = self._read_upload()
        if got is None:
            return
        job_id, job_dir, fields, files, cover_path = got
        files = self._ordered_files(files, fields.get("order"))

        first_stem = Path(os.path.basename(files[0]["filename"])).stem or "book"
        out_name = first_stem + "_合并.txt"
        out_path = job_dir / out_name
        try:
            if len(files) == 1:
                # Single file: just normalize newlines and hand it back.
                text, _ = read_text_auto(files[0]["path"])
                text = text.replace("\r\n", "\n").replace("\r", "\n")
                with open(out_path, "w", encoding="utf-8", newline="") as fh:
                    fh.write(text)
            else:
                merge_txt_files([f["path"] for f in files], out_path)
        except Exception as e:
            shutil.rmtree(job_dir, ignore_errors=True)
            self._json(400, {"error": f"合并失败: {e}"})
            return
        # Clean the raw uploaded parts; keep only the merged result.
        for f in files:
            f["path"].unlink(missing_ok=True)
        if cover_path:
            cover_path.unlink(missing_ok=True)  # cover is unused by /merge

        with JOBS_LOCK:
            _prune_jobs_locked()
            JOBS[job_id] = {
                "status": "done", "progress": 100,
                "created": time.time(), "kind": "merge",
                "merged_from": len(files),
                "title": first_stem,
                "filename": out_name,
                "size": out_path.stat().st_size,
                "download": f"/download/{job_id}/{quote(out_name)}",
                "saved": True,
            }
            HISTORY[job_id] = {
                "job_id": job_id, "kind": "merge",
                "title": first_stem, "filename": out_name,
                "status": "done", "progress": 100,
                "size": out_path.stat().st_size,
                "download": f"/download/{job_id}/{quote(out_name)}",
                "created": JOBS[job_id]["created"],
            }
            _save_history_locked()
        self._json(200, {
            "ok": True,
            "download": f"/download/{job_id}/{quote(out_name)}",
            "filename": out_name,
            "size": out_path.stat().st_size,
            "merged_from": len(files),
            "order": [os.path.basename(f["filename"]) for f in files],
        })

    def handle_convert(self):
        if EBOOK_CONVERT is None:
            self._json(500, {"error": "Calibre (ebook-convert) not found."})
            return
        got = self._read_upload()
        if got is None:
            return
        job_id, job_dir, fields, files, cover_path = got

        fmt = fields.get("format", "").lower().strip()
        if fmt not in FORMATS:
            shutil.rmtree(job_dir, ignore_errors=True)
            self._json(400, {"error": f"Unsupported format: {fmt}"})
            return

        nosplit = fields.get("nosplit", "").strip() in ("1", "true", "on", "yes")
        auto_chapters = fields.get("auto_chapters", "1").strip().lower() \
            not in ("0", "false", "off", "no", "")
        clean_ads = fields.get("clean_ads", "1").strip().lower() \
            not in ("0", "false", "off", "no", "")
        title_override = fields.get("title", "").strip()[:200]
        author_override = fields.get("author", "").strip()[:200]
        chapter_re = fields.get("chapter_re", "").strip()
        retention_hours = None
        raw_rt = fields.get("retention_hours", "").strip()
        if raw_rt:
            try:
                retention_hours = min(max(float(raw_rt), 1.0), 24 * 30.0)
            except ValueError:
                retention_hours = None
        split_mb = None
        raw_mb = fields.get("split_mb", "").strip()
        if raw_mb and not nosplit:
            try:
                split_mb = min(max(float(raw_mb), 1.0), 500.0)
            except ValueError:
                split_mb = None
        files = self._ordered_files(files, fields.get("order"))

        # Ebook inputs (epub/mobi/azw/azw3) only convert one file at a time.
        is_txt_all = all(f["filename"].lower().endswith(".txt") for f in files)
        if not is_txt_all and len(files) > 1:
            shutil.rmtree(job_dir, ignore_errors=True)
            self._json(400, {"error": "EPUB / MOBI 等电子书格式仅支持单个文件转换"})
            return

        # --- Build the source: single file passes through; multiple files are
        # merged (headers de-duplicated) into one book in the chosen order. ---
        if len(files) == 1:
            f0 = files[0]
            stem = Path(os.path.basename(f0["filename"])).stem or "book"
            ext = Path(f0["filename"]).suffix.lower() or ".txt"
            src_path = job_dir / (stem + ".src" + ext)
            f0["path"].replace(src_path)
            merged_from = 1
        else:
            first_stem = Path(os.path.basename(files[0]["filename"])).stem
            stem = (first_stem or "book")
            src_path = job_dir / (stem + ".src.txt")
            try:
                merge_txt_files([f["path"] for f in files], src_path)
            except Exception as e:
                shutil.rmtree(job_dir, ignore_errors=True)
                self._json(400, {"error": f"合并失败: {e}"})
                return
            # Clean the raw uploaded parts now that they're merged.
            for f in files:
                f["path"].unlink(missing_ok=True)
            merged_from = len(files)

        order_names = [os.path.basename(f["filename"]) for f in files]

        with JOBS_LOCK:
            _prune_jobs_locked()
            JOBS[job_id] = {
                "status": "queued", "progress": 0,
                "total_parts": 0, "done_parts": 0,
                "message": (f"已合并 {merged_from} 个文件,准备中..."
                            if merged_from > 1 else "已接收,准备中..."),
                "format": fmt, "created": time.time(),
                "nosplit": nosplit, "merged_from": merged_from,
                "split_mb": split_mb,
                "auto_chapters": auto_chapters,
                "cover": "custom" if cover_path else None,
                "ttl": (retention_hours or (JOB_TTL / 3600)) * 3600,
                "order": order_names,
            }

        t = threading.Thread(target=run_conversion,
                             args=(job_id, src_path, fmt, stem, job_dir),
                             kwargs={"nosplit": nosplit,
                                     "custom_cover": cover_path,
                                     "split_mb": split_mb,
                                     "auto_chapters": auto_chapters,
                                     "clean_ads": clean_ads,
                                     "title_override": title_override,
                                     "author_override": author_override,
                                     "chapter_re": chapter_re,
                                     "retention_hours": retention_hours},
                             daemon=True)
        t.start()

        self._json(200, {"ok": True, "job_id": job_id})

    def handle_sources(self):
        """List configured web-novel sources (sources/*.json)."""
        self._json(200, {"ok": True, "sources": [
            {"id": s["id"],
             "name": s.get("name") or s["id"],
             "home": s.get("home") or "",
             "encoding": s.get("encoding") or "auto"}
            for s in SOURCES.values()]})

    def handle_sources_save(self):
        """POST /sources: add or update a book source (written to
        sources/<id>.json, loaded into memory immediately)."""
        data = self._read_json_body()
        src = data.get("source")
        if not isinstance(src, dict) or not src.get("id"):
            self._json(400, {"error": "书源缺少 id"})
            return
        sid = str(src["id"]).strip()
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", sid):
            self._json(400, {"error": "书源 id 仅支持字母、数字、下划线、连字符"})
            return
        if not src.get("home"):
            self._json(400, {"error": "请填写站点主页 URL"})
            return
        if not src.get("name"):
            src["name"] = sid
        # auto url_re from the home domain when missing
        if not src.get("url_re"):
            dom = urlparse(src["home"]).netloc
            src["url_re"] = re.escape(dom)
        try:
            SOURCES_DIR.mkdir(parents=True, exist_ok=True)
            path = SOURCES_DIR / (sid + ".json")
            path.write_text(json.dumps(src, ensure_ascii=False, indent=2),
                            encoding="utf-8")
        except OSError as e:
            self._json(500, {"error": f"保存失败: {e}"})
            return
        src["_file"] = path.name
        SOURCES[sid] = src
        self._json(200, {"ok": True, "id": sid})

    def handle_sources_delete(self):
        """POST /sources/delete: remove a book source."""
        data = self._read_json_body()
        sid = (data.get("id") or "").strip()
        src = SOURCES.get(sid)
        if not src:
            self._json(404, {"error": "书源不存在"})
            return
        path = SOURCES_DIR / (src.get("_file") or (sid + ".json"))
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass
        SOURCES.pop(sid, None)
        self._json(200, {"ok": True})

    def handle_sources_probe(self):
        """POST /sources/probe {url}: fetch a chapter page and auto-detect
        the source settings (encoding, content container, chapter paging,
        TOC paging) so adding a new source is one click."""
        data = self._read_json_body()
        url = (data.get("url") or "").strip()
        if not url.startswith(("http://", "https://")):
            self._json(400, {"error": "请输入章节页 URL"})
            return
        try:
            raw, final = _http_fetch(url)
        except GrabError as e:
            self._json(400, {"error": str(e)})
            return
        enc = _sniff_charset(raw)
        text = decode_web_bytes(raw, None)
        # find the best known content container by CJK length
        common = _COMMON_CONTENT_SELECTORS
        best_sel, best_len = None, 0
        for sel in common:
            try:
                ex = _SelectorExtractor([sel])
                ex.feed(text)
                ex.close()
                out = ex.result() or ""
                cjk = sum(1 for c in out if "\u4e00" <= c <= "\u9fff")
                if cjk > best_len:
                    best_sel, best_len = sel, cjk
            except Exception:
                continue
        # chapter paging detection (number of pages incl. current)
        pages = _chapter_pages(final, text)
        n_pages = len(pages) + 1
        # TOC paging: look for the 目录/书页 link and whether it has 下一页
        toc_url = None
        for m in re.finditer(r'<a[^>]*href="([^"]+)"[^>]*>([^<]{1,20})</a>', text):
            tx = re.sub(r"<[^>]+>", " ", m.group(2))
            tx = tx.replace("&nbsp;", " ").replace("\xa0", " ")
            tx = re.sub(r"\s+", "", tx)
            if tx in ("目录", "章节目录", "返回目录", "返回书页", "返回", "书页"):
                toc_url = urljoin(final, m.group(1).strip())
                break
        toc_pages = {}
        if toc_url:
            try:
                raw_t, _ = _http_fetch(toc_url)
                text_t = decode_web_bytes(raw_t, None)
                for m in re.finditer(r'<a[^>]*href="([^"]+)"[^>]*>([^<]{1,12})</a>', text_t):
                    if re.search(r"下一页|下页|更多章节", m.group(2)):
                        toc_pages = {"next_text_re": "下一页|下页|更多章节|更多",
                                     "max_pages": 30}
                        break
            except GrabError:
                pass
        dom = urlparse(final).netloc
        src = {"id": re.sub(r"[^A-Za-z0-9_-]", "_", dom.split(".")[-2] or dom)
               if dom else "mysite",
               "name": dom or "新书源",
               "home": "https://" + dom if dom else "",
               "encoding": enc or "utf-8"}
        src["toc"] = {"link_re": str(_GRAB_CH_RE.pattern), "dedupe": "keep_last"}
        if toc_pages:
            src["toc"]["pages"] = toc_pages
        if best_sel:
            src["chapter"] = {"title": [{"tag": "h1"}],
                              "content": [best_sel], "pagination": True}
        self._json(200, {"ok": True, "suggest": src,
                         "detected": {"encoding": enc or "auto",
                                       "container": best_sel or None,
                                       "cjk_chars": best_len,
                                       "chapter_pages": n_pages,
                                       "toc_url": toc_url,
                                       "toc_paged": bool(toc_pages)}})

    def handle_grab(self):
        """Start a background grab job: a chapter page or a whole book from a
        TOC page -> clean TXT stored in the library (+ downloadable)."""
        data = self._read_json_body()
        url = (data.get("url") or "").strip()
        if not url or not url.startswith(("http://", "https://")):
            self._json(400, {"error": "请输入以 http:// 或 https:// 开头的网址"})
            return
        source_id = (data.get("source_id") or "").strip()
        source = SOURCES.get(source_id) if source_id else None
        mode = (data.get("mode") or "auto").strip().lower()
        if mode not in ("auto", "chapter", "toc"):
            mode = "auto"
        clean_ads = data.get("clean_ads", True)
        if isinstance(clean_ads, str):
            clean_ads = clean_ads.strip().lower() not in ("0", "false", "off", "no", "")
        render = (data.get("render") or "auto").strip().lower()
        if render not in ("auto", "off", "on"):
            render = "auto"
        max_chapters = 0
        try:
            max_chapters = int(data.get("max_chapters") or 0)
        except (TypeError, ValueError):
            max_chapters = 0
        cap = max_chapters or int(os.environ.get("TXT2EBOOK_GRAB_MAX_CHAPTERS")
                                  or GRAB_MAX_CHAPTERS)
        cap = min(max(1, cap), 20000)

        job_id = uuid.uuid4().hex
        with JOBS_LOCK:
            _prune_jobs_locked()
            JOBS[job_id] = {"status": "queued", "progress": 0, "kind": "grab",
                            "url": url, "created": time.time(),
                            "source": source_id or "auto", "mode": mode,
                            "message": "准备抓取..."}
        t = threading.Thread(target=run_grab, args=(job_id, url),
                             kwargs={"source": source, "mode": mode,
                                     "clean_ads": clean_ads,
                                     "render": render,
                                     "title_override": (data.get("title") or "").strip(),
                                     "author_override": (data.get("author") or "").strip(),
                                     "max_chapters": cap},
                             daemon=True)
        t.start()
        self._json(200, {"ok": True, "job_id": job_id})

    def handle_convert_lib(self):
        """Convert a book already stored in the local library (e.g. fetched
        from a web page) through the normal conversion pipeline."""
        data = self._read_json_body()
        book_id = (data.get("book_id") or "").strip()
        if not re.fullmatch(r"[0-9a-f]{16}", book_id):
            self._json(400, {"error": "无效的书本 ID"})
            return
        content = READ_LIB / book_id / "content.txt"
        if not content.is_file():
            self._json(404, {"error": "本地库中找不到这本书"})
            return
        fmt = (data.get("format") or "mobi").lower().strip()
        if fmt not in FORMATS:
            self._json(400, {"error": f"不支持的格式: {fmt}"})
            return
        if fmt != "txt" and EBOOK_CONVERT is None:
            self._json(500, {"error": "未检测到 Calibre(ebook-convert),无法转换"})
            return
        meta = {}
        try:
            meta = json.loads((READ_LIB / book_id / "meta.json")
                              .read_text(encoding="utf-8"))
        except Exception:
            pass
        title = (data.get("title") or meta.get("title") or "").strip()
        author = (data.get("author") or meta.get("author") or "").strip()

        job_id = uuid.uuid4().hex
        job_dir = output_base() / job_id
        job_dir.mkdir(parents=True, exist_ok=True)
        try:
            (job_dir / ".txt2ebook_job").write_text("1")
        except OSError:
            pass
        stem = _safe_stem(title or book_id)
        src_path = job_dir / (stem + ".src.txt")
        shutil.copyfile(content, src_path)

        nosplit = str(data.get("nosplit", "")).strip() \
            in ("1", "true", "on", "yes")
        auto_chapters = str(data.get("auto_chapters", "1")).strip().lower() \
            not in ("0", "false", "off", "no", "")
        clean_ads = str(data.get("clean_ads", "1")).strip().lower() \
            not in ("0", "false", "off", "no", "")
        chapter_re = (data.get("chapter_re") or "").strip()
        split_mb = None
        try:
            raw_mb = str(data.get("split_mb") or "").strip()
            if raw_mb and not nosplit:
                split_mb = min(max(float(raw_mb), 1.0), 500.0)
        except ValueError:
            split_mb = None
        retention_hours = None
        try:
            raw_rt = str(data.get("retention_hours") or "").strip()
            if raw_rt:
                retention_hours = min(max(float(raw_rt), 1.0), 24 * 30.0)
        except ValueError:
            retention_hours = None

        with JOBS_LOCK:
            _prune_jobs_locked()
            JOBS[job_id] = {"status": "queued", "progress": 0, "kind": "convert",
                            "format": fmt, "created": time.time(),
                            "message": "已接收,准备中...", "nosplit": nosplit,
                            "split_mb": split_mb, "auto_chapters": auto_chapters,
                            "clean_ads": clean_ads, "book_id": book_id,
                            "ttl": (retention_hours or (JOB_TTL / 3600)) * 3600}
        t = threading.Thread(target=run_conversion,
                             args=(job_id, src_path, fmt, stem, job_dir),
                             kwargs={"nosplit": nosplit,
                                     "split_mb": split_mb,
                                     "auto_chapters": auto_chapters,
                                     "clean_ads": clean_ads,
                                     "title_override": title or None,
                                     "author_override": author or None,
                                     "chapter_re": chapter_re,
                                     "retention_hours": retention_hours},
                             daemon=True)
        t.start()
        self._json(200, {"ok": True, "job_id": job_id})

    def log_message(self, fmt, *args):
        sys.stderr.write("[%s] %s\n" % (self.address_string(), fmt % args))


def _prune_jobs_locked():
    """Drop finished jobs older than JOB_TTL and cap total at MAX_JOBS.
    Also deletes their output directories. Caller must hold JOBS_LOCK.
    Running/queued jobs are never pruned."""
    now = time.time()
    removable = [
        (jid, j) for jid, j in JOBS.items()
        if j.get("status") in ("done", "error", "cancelled")
    ]
    victims = set()
    # 1) age-based (per-job TTL when the user chose a custom retention)
    for jid, j in removable:
        if now - j.get("created", now) > j.get("ttl", JOB_TTL):
            victims.add(jid)
    # 2) count-based: keep only newest MAX_JOBS overall
    if len(JOBS) > MAX_JOBS:
        by_age = sorted(removable, key=lambda kv: kv[1].get("created", 0))
        overflow = len(JOBS) - MAX_JOBS
        for jid, _ in by_age:
            if overflow <= 0:
                break
            victims.add(jid)
            overflow -= 1
    for jid in victims:
        JOBS.pop(jid, None)
        for base in output_dirs():
            shutil.rmtree(base / jid, ignore_errors=True)


def _parse_disposition(header_blob):
    """Return (name, filename) from a part's Content-Disposition header bytes."""
    name = filename = None
    for line in header_blob.split(b"\r\n"):
        if line.lower().startswith(b"content-disposition:"):
            disp = line.split(b":", 1)[1].decode("utf-8", "replace")
            for token in disp.split(";"):
                token = token.strip()
                if token.startswith("name="):
                    name = token[5:].strip('"')
                elif token.startswith("filename="):
                    filename = token[9:].strip('"')
    return name, filename


def stream_multipart(rfile, length, boundary, file_dir):
    """Stream a multipart/form-data body from ``rfile`` without buffering it all.

    Every file part is written straight to its own file under ``file_dir``
    (``upload_00.part``, ``upload_01.part``, ...). Other (small) fields are
    decoded and returned. Returns ``(fields, files)`` where ``files`` is a list
    of ``{"path": Path, "filename": str}`` in the order they arrived.

    Parsing is boundary-driven over a sliding buffer, so peak memory stays at a
    few chunks regardless of upload size.
    """
    delim = b"--" + boundary
    CHUNK = 1024 * 1024
    remaining = length
    buf = b""
    fields = {}
    files = []
    file_idx = 0

    def _read_more():
        nonlocal remaining
        if remaining <= 0:
            return b""
        want = min(CHUNK, remaining)
        data = rfile.read(want)
        remaining -= len(data)
        return data

    # Prime buffer past the first boundary.
    while delim not in buf:
        more = _read_more()
        if not more:
            break
        buf += more
    # Drop everything up to and including the first delimiter.
    idx = buf.find(delim)
    if idx != -1:
        buf = buf[idx + len(delim):]

    fout = None
    try:
        while True:
            # Each part starts with optional "\r\n", then headers, then \r\n\r\n.
            if buf[:2] == b"--":
                break  # closing delimiter -> done
            buf = buf.lstrip(b"\r\n")
            # Ensure we have the full header block.
            while b"\r\n\r\n" not in buf:
                more = _read_more()
                if not more:
                    break
                buf += more
            if b"\r\n\r\n" not in buf:
                break
            header_blob, _, buf = buf.partition(b"\r\n\r\n")
            name, filename = _parse_disposition(header_blob)
            is_file = filename is not None
            cur_path = None
            if is_file:
                cur_path = file_dir / f"upload_{file_idx:02d}.part"
                file_idx += 1
                fout = open(cur_path, "wb")
            collected = bytearray() if not is_file else None

            # Read body until next boundary, streaming to disk if it's the file.
            # A real delimiter is "\r\n--boundary" followed by "\r\n" (next part)
            # or "--" (final). We must confirm those trailing bytes so content
            # that merely contains the boundary text is not mistaken for it.
            sep = b"\r\n" + delim
            search_from = 0
            while True:
                pos = buf.find(sep, search_from)
                if pos != -1:
                    after = buf[pos + len(sep): pos + len(sep) + 2]
                    if len(after) < 2 and remaining > 0:
                        # Need more bytes to disambiguate the trailing marker.
                        more = _read_more()
                        if more:
                            buf += more
                            continue
                    if len(after) >= 2 and after != b"\r\n" and after != b"--":
                        # False positive: boundary text inside content. Keep
                        # scanning past this occurrence.
                        search_from = pos + len(sep)
                        continue
                    # Real delimiter (or stream ended right at one: treat as end).
                    chunk = buf[:pos]
                    if is_file:
                        fout.write(chunk)
                    else:
                        collected += chunk
                        if len(collected) > MAX_FIELD_BYTES:
                            raise ValueError("\u8868\u5355\u5b57\u6bb5\u8fc7\u5927")
                    buf = buf[pos + len(sep):]
                    break
                # Not found yet: flush all but a tail that could straddle the
                # boundary marker (sep + 2 trailing bytes), then read more.
                keep = len(sep) + 1
                if len(buf) > keep:
                    flush = buf[:-keep]
                    if is_file:
                        fout.write(flush)
                    else:
                        collected += flush
                        if len(collected) > MAX_FIELD_BYTES:
                            raise ValueError("\u8868\u5355\u5b57\u6bb5\u8fc7\u5927")
                    buf = buf[-keep:]
                    search_from = 0
                more = _read_more()
                if not more:
                    # End of stream without trailing boundary; take what's left.
                    if is_file:
                        fout.write(buf)
                    else:
                        collected += buf
                        if len(collected) > MAX_FIELD_BYTES:
                            raise ValueError("\u8868\u5355\u5b57\u6bb5\u8fc7\u5927")
                    buf = b""
                    break
                buf += more

            if is_file:
                fout.close()
                fout = None
                files.append({"path": cur_path, "filename": filename,
                              "name": name})
            elif name is not None:
                fields[name] = collected.decode("utf-8", "replace").strip()

            # After a part, buf either starts with "--" (end) or "\r\n" (next).
            if buf[:2] == b"--":
                break
            if remaining <= 0 and not buf:
                break
    finally:
        if fout is not None:
            fout.close()

    return fields, files


INDEX_HTML = r"""<!DOCTYPE html>
<html lang="zh-CN" data-theme="default">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>TXT 转 Kindle 电子书</title>
<style>
  /* ================= Themes ================= */
  :root {
    --bg:#0f1115; --bg2:#13161d; --card:#1a1d24; --card2:#14171d;
    --fg:#e8eaed; --muted:#9aa0a6;
    --accent:#4f8cff; --accent2:#3a6fd8; --accent-soft:rgba(79,140,255,.15);
    --ok:#34c759; --err:#ff453a; --border:#2a2e37;
    --shadow:0 12px 40px rgba(0,0,0,.45);
  }
  [data-theme="aurora"] {
    --bg:#0c1513; --bg2:#101b18; --card:#12201c; --card2:#0f1916;
    --fg:#e6f2ee; --muted:#8fa89f;
    --accent:#2dd4a7; --accent2:#0f9d7a; --accent-soft:rgba(45,212,167,.14);
    --ok:#34d399; --err:#fb7185; --border:#1e332c;
  }
  [data-theme="sakura"] {
    --bg:#171016; --bg2:#1d141b; --card:#221820; --card2:#1b1219;
    --fg:#f3e8ee; --muted:#a88e9c;
    --accent:#ff7ab8; --accent2:#d94f92; --accent-soft:rgba(255,122,184,.14);
    --ok:#4ade80; --err:#fb7185; --border:#37232f;
  }
  [data-theme="sunset"] {
    --bg:#171310; --bg2:#1e1813; --card:#251c15; --card2:#1d1611;
    --fg:#f5ead9; --muted:#b09a7f;
    --accent:#ffb454; --accent2:#e08a2e; --accent-soft:rgba(255,180,84,.14);
    --ok:#86efac; --err:#f87171; --border:#3a2c1e;
  }
  [data-theme="violet"] {
    --bg:#110f1c; --bg2:#17142a; --card:#1c1833; --card2:#151226;
    --fg:#ece9f7; --muted:#9b93c4;
    --accent:#a78bfa; --accent2:#7c5ce0; --accent-soft:rgba(167,139,250,.15);
    --ok:#6ee7b7; --err:#fda4af; --border:#2d2750;
  }
  [data-theme="paper"] {
    --bg:#f2efe8; --bg2:#e9e5db; --card:#fbfaf6; --card2:#f4f1e8;
    --fg:#2c2822; --muted:#7a746a;
    --accent:#2f6f4f; --accent2:#245a40; --accent-soft:rgba(47,111,79,.12);
    --ok:#1a7f4b; --err:#c0392b; --border:#d8d2c4;
    --shadow:0 12px 36px rgba(60,50,30,.14);
  }
  [data-theme="ocean"] {
    --bg:#0a141f; --bg2:#0e1a28; --card:#132233; --card2:#0f1c2b;
    --fg:#e6eef7; --muted:#8ba3ba;
    --accent:#38bdf8; --accent2:#0284c7; --accent-soft:rgba(56,189,248,.15);
    --ok:#34d399; --err:#fb7185; --border:#1e3348;
  }
  [data-theme="graphite"] {
    --bg:#101012; --bg2:#161618; --card:#1c1c1f; --card2:#141416;
    --fg:#e9e9ec; --muted:#98989f;
    --accent:#d4d4d8; --accent2:#8b8b93; --accent-soft:rgba(212,212,216,.14);
    --ok:#4ade80; --err:#f87171; --border:#2a2a2f;
  }
  [data-theme="wine"] {
    --bg:#150d10; --bg2:#1c1115; --card:#26171c; --card2:#1d1115;
    --fg:#f5e6e9; --muted:#b08d95;
    --accent:#ef5f7a; --accent2:#c22e4c; --accent-soft:rgba(239,95,122,.15);
    --ok:#4ade80; --err:#fb7185; --border:#3b222a;
  }
  [data-theme="lime"] {
    --bg:#11130c; --bg2:#171a10; --card:#202416; --card2:#181b10;
    --fg:#eef2e2; --muted:#a3ab8c;
    --accent:#a3e635; --accent2:#6f9f1f; --accent-soft:rgba(163,230,53,.15);
    --ok:#86efac; --err:#f87171; --border:#2d3320;
  }
  [data-theme="ice"] {
    --bg:#eef4f8; --bg2:#e3edf3; --card:#fbfdfe; --card2:#f2f7fa;
    --fg:#24333d; --muted:#6d828f;
    --accent:#0e7490; --accent2:#155e75; --accent-soft:rgba(14,116,144,.12);
    --ok:#15803d; --err:#b91c1c; --border:#cfdde6;
    --shadow:0 12px 36px rgba(40,80,110,.12);
  }
  [data-theme="sand"] {
    --bg:#f6f1e7; --bg2:#efe7d8; --card:#fdfaf3; --card2:#f7f1e3;
    --fg:#3a3126; --muted:#8d8170;
    --accent:#c2703d; --accent2:#a3542a; --accent-soft:rgba(194,112,61,.13);
    --ok:#4d7c0f; --err:#b91c1c; --border:#e0d5c0;
    --shadow:0 12px 36px rgba(90,70,40,.12);
  }
  [data-theme="miku"] {
    --bg:#0c1514; --bg2:#101b1a; --card:#13201e; --card2:#0e1716;
    --fg:#e6f5f3; --muted:#8fb5b0;
    --accent:#39c5bb; --accent2:#15968c; --accent-soft:rgba(57,197,187,.15);
    --ok:#4ade80; --err:#fb7185; --border:#1d3431;
  }
  [data-theme="cyber"] {
    --bg:#0d0b1a; --bg2:#131027; --card:#1a1530; --card2:#120e22;
    --fg:#f0ecff; --muted:#9b92c8;
    --accent:#ff2a6d; --accent2:#c71d57; --accent-soft:rgba(255,42,109,.16);
    --ok:#05d9e8; --err:#ff5c5c; --border:#2b2450;
  }
  [data-theme="shinkai"] {
    --bg:#0d1830; --bg2:#12203e; --card:#182a4d; --card2:#101d38;
    --fg:#eef3fb; --muted:#93a7c8;
    --accent:#ff9d5c; --accent2:#e2703a; --accent-soft:rgba(255,157,92,.16);
    --ok:#6ee7b7; --err:#f87171; --border:#24375c;
  }
  [data-theme="retro"] {
    --bg:#0f1210; --bg2:#141a14; --card:#1b231b; --card2:#121712;
    --fg:#e9f5e9; --muted:#93a893;
    --accent:#39ff14; --accent2:#1f9d0b; --accent-soft:rgba(57,255,20,.14);
    --ok:#39ff14; --err:#ff5c5c; --border:#27331f;
  }
  [data-theme="starry"] {
    --bg:#0a0a16; --bg2:#10102a; --card:#161636; --card2:#0e0e22;
    --fg:#eef0ff; --muted:#9aa0d8;
    --accent:#ffd76e; --accent2:#cfa427; --accent-soft:rgba(255,215,110,.15);
    --ok:#6ee7b7; --err:#fda4af; --border:#262650;
  }
  [data-theme="pastel"] {
    --bg:#fdf4f7; --bg2:#f8eaf0; --card:#fffbfd; --card2:#fdf0f5;
    --fg:#4a3540; --muted:#a38595;
    --accent:#ff8fb3; --accent2:#f2659a; --accent-soft:rgba(255,143,179,.16);
    --ok:#4d9f6d; --err:#e05656; --border:#f0d4e0;
    --shadow:0 12px 36px rgba(200,120,160,.14);
  }

  /* ================= Base ================= */
  * { box-sizing:border-box; }
  html,body { height:100%; }
  body {
    margin:0; font-family:-apple-system,"Segoe UI",Roboto,"Microsoft YaHei",sans-serif;
    background:var(--bg); color:var(--fg);
  }
  .app { display:flex; flex-direction:column; height:100vh; }

  /* ================= Header ================= */
  .hd {
    display:flex; align-items:center; gap:14px; padding:12px 22px;
    border-bottom:1px solid var(--border);
    background:linear-gradient(180deg,var(--bg2),var(--bg));
  }
  .hd .logo { font-size:20px; margin:0; white-space:nowrap; }
  .hd .tag { font-size:12px; color:var(--muted); white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
  .hd .sp { flex:1; }
  .festchip {
    display:none; align-items:center; gap:4px; padding:4px 12px; border-radius:999px;
    border:1px solid var(--accent); color:var(--accent); font-size:12px; font-weight:700;
    background:var(--accent-soft); animation:festPulse 2s infinite; white-space:nowrap;
  }
  .lanchip {
    display:none; align-items:center; gap:4px; padding:4px 12px; border-radius:999px;
    border:1px solid var(--accent); color:var(--accent); font-size:12px; font-weight:700;
    background:var(--accent-soft); white-space:nowrap; cursor:pointer;
  }
  .lanchip.show { display:inline-flex; }
  @keyframes festPulse { 0%,100%{transform:scale(1)} 50%{transform:scale(1.07)} }
  .rain { position:fixed; top:-50px; z-index:99; pointer-events:none; animation:fall 9s linear infinite; opacity:.85; }
  @keyframes fall { to { transform:translateY(115vh) rotate(360deg); } }
  .langbtn {
    background:none; border:1px solid var(--border); color:var(--fg);
    border-radius:9px; font-size:12px; padding:4px 10px; cursor:pointer; font-weight:700;
  }
  .langbtn:hover { border-color:var(--accent); }
  .catbtn { font-size:17px; padding:2px 9px; line-height:1.3; }
  .themewrap { position:relative; }
  .themes {
    position:absolute; top:calc(100% + 10px); right:0; z-index:60;
    display:none; width:300px; padding:12px; gap:7px; flex-wrap:wrap;
    background:var(--card); border:1px solid var(--border); border-radius:12px;
    box-shadow:var(--shadow);
  }
  .themes.open { display:flex; }
  .themes .tlabel { font-size:11px; color:var(--muted); width:100%; margin-bottom:2px; }
  .theme {
    width:18px; height:18px; border-radius:50%; cursor:pointer;
    border:2px solid var(--border); transition:.15s; padding:0;
  }
  .theme:hover { transform:scale(1.18); }
  .theme.active { border-color:var(--fg); box-shadow:0 0 0 2px var(--bg),0 0 0 4px var(--accent); }
  .theme.custom {
    width:100%; border:1px dashed var(--border); border-radius:10px; padding:7px;
    font-size:12px; color:var(--fg); cursor:pointer; display:flex; align-items:center;
    justify-content:center; gap:6px;
    background:var(--bg2);
  }
  .theme.custom:hover { border-color:var(--accent); }
  .theme.custom.active { border-color:var(--accent); border-style:solid; color:var(--accent); box-shadow:0 0 0 2px var(--accent-soft); }
  .theme.custom.active::after { content:'✓'; font-weight:700; }
  .theme-reset {
    display:none; background:none; border:1px solid var(--border); color:var(--muted);
    border-radius:10px; font-size:11px; padding:3px 9px; cursor:pointer;
    margin:0 2px; white-space:nowrap;
  }
  .theme-reset:hover { color:var(--fg); border-color:var(--accent); }
  .bgsection { width:100%; margin-top:6px; border-top:1px solid var(--border); padding-top:10px; }
  .bghead { display:flex; align-items:center; gap:8px; font-size:11px; color:var(--muted); margin-bottom:8px; flex-wrap:wrap; }
  .bghead .bgbtn { margin-left:auto; background:none; border:1px solid var(--border); color:var(--muted); border-radius:7px; font-size:11px; padding:2px 8px; cursor:pointer; }
  .bghead .bgbtn:hover, .bgbtn:hover { color:var(--fg); border-color:var(--accent); }
  .bgbtn.on { color:var(--accent); border-color:var(--accent); }
  .bgdelopt { display:flex; align-items:center; gap:4px; font-size:11px; color:var(--muted); cursor:pointer; user-select:none; }
  .bgdelopt input { accent-color:var(--accent); }
  .bgrow { display:flex; align-items:center; gap:8px; margin-top:9px; font-size:11px; color:var(--muted); flex-wrap:wrap; }
  .bgrow .bgbtn { background:none; border:1px solid var(--border); color:var(--muted); border-radius:7px; font-size:11px; padding:2px 8px; cursor:pointer; }
  .bggrid { display:grid; grid-template-columns:repeat(3,1fr); gap:8px; }
  .bgthumb { position:relative; aspect-ratio:3/4; border-radius:8px; border:2px solid var(--border); cursor:pointer; transition:.15s; background:var(--bg) center/cover no-repeat; }
  .bgthumb:hover { border-color:var(--accent); }
  .bgthumb.sel { border-color:var(--accent); box-shadow:0 0 0 2px var(--accent-soft); }
  .bgthumb.vid::after { content:'▶'; position:absolute; inset:0; display:flex; align-items:center; justify-content:center; color:#fff; font-size:20px; text-shadow:0 1px 6px rgba(0,0,0,.7); }
  .bgthumb .x { position:absolute; top:2px; right:2px; background:rgba(0,0,0,.55); color:#fff; border:none; border-radius:6px; font-size:10px; line-height:1; padding:3px 5px; cursor:pointer; display:none; z-index:2; }
  .bgthumb:hover .x { display:block; }
  .bgthumb .ck { position:absolute; top:3px; left:3px; width:14px; height:14px; border-radius:4px; background:rgba(0,0,0,.55); border:1px solid #fff; display:none; align-items:center; justify-content:center; font-size:10px; color:#fff; z-index:2; }
  .bgsection.selmode .bgthumb .ck { display:flex; }
  .bgsection.selmode .bgthumb .x { display:none; }
  .bgsection.selmode .bgthumb.pick .ck { background:var(--accent); }
  .bgthumb.vid::after { pointer-events:none; }
  .bgempty { font-size:11px; color:var(--muted); padding:6px 0; }
  .bgbatchbar { display:flex; gap:8px; margin-top:8px; }
  .bgimg { position:fixed; inset:0; z-index:-2; background:center/cover no-repeat fixed; display:none; }
  .bgvideo { position:fixed; inset:0; z-index:-2; width:100%; height:100%; object-fit:cover; display:none; }
  .bgvideo.show { display:block; }
  .bgveil { position:fixed; inset:0; z-index:-1; display:none; }
  html.hasbg .bgimg, html.hasbg .bgveil { display:block; }
  html.hasbg .bgveil { background:color-mix(in srgb, var(--bg) 58%, transparent); }
  html.hasbg .hd, html.hasbg .col-left {
    background:color-mix(in srgb, var(--bg2) 68%, transparent);
    backdrop-filter:blur(10px); -webkit-backdrop-filter:blur(10px);
  }
  html.hasbg .drop, html.hasbg .fname, html.hasbg .fitem, html.hasbg .hitem,
  html.hasbg .fmt span, html.hasbg .splitrow input, html.hasbg .status.err,
  html.hasbg .bar, html.hasbg .warn, html.hasbg .pv, html.hasbg .adv, html.hasbg .batch {
    background:color-mix(in srgb, var(--card2) 55%, transparent);
  }
  .resizer {
    width:7px; flex-shrink:0; cursor:col-resize; position:relative;
    background:transparent; transition:background .15s; z-index:3;
  }
  .resizer::after { content:''; position:absolute; left:3px; top:0; bottom:0; width:1px; background:var(--border); }
  .resizer:hover, .resizer.drag { background:var(--accent); }
  .resizer:hover::after, .resizer.drag::after { background:var(--accent); }
  @media (max-width:980px) { .resizer { display:none; } }

  /* ================= Layout ================= */
  .main { flex:1; display:flex; min-height:0; }
  .col { padding:18px 22px 30px; overflow-y:auto; }
  .col-left {
    width:440px; min-width:370px; flex-shrink:0;
    border-right:1px solid var(--border); background:var(--bg2);
  }
  .col-right { flex:1; }
  @media (max-width:980px) {
    .main { flex-direction:column; }
    .col-left { width:auto !important; min-width:0; border-right:none; border-bottom:1px solid var(--border); }
    .hd .tag { display:none; }
  }
  .step {
    font-size:12px; font-weight:700; color:var(--accent); letter-spacing:1.5px;
    margin:20px 0 10px; display:flex; align-items:center; gap:10px; user-select:none;
  }
  .step:first-child { margin-top:0; }
  .step::after { content:''; flex:1; height:1px; background:var(--border); }

  /* ================= Drop / files ================= */
  .drop {
    border:2px dashed var(--border); border-radius:14px; padding:30px 16px;
    text-align:center; cursor:pointer; transition:.15s; background:var(--card2);
  }
  .drop:hover, .drop.drag { border-color:var(--accent); background:var(--card); }
  .drop input { display:none; }
  .drop .icon { font-size:40px; line-height:1; }
  .drop .txt { margin-top:10px; font-size:14px; color:var(--muted); }
  .drop .hint2 { margin-top:6px; font-size:11px; color:var(--muted); line-height:1.45; }
  .fname { margin-top:12px; font-size:14px; word-break:break-all; display:none; background:var(--card2); border:1px solid var(--border); border-radius:10px; padding:10px 12px; }
  .fname.show { display:block; }
  .flist { margin-top:12px; display:none; }
  .flist.show { display:block; }
  .flist .hint { font-size:12px; color:var(--muted); margin-bottom:8px; line-height:1.5; }
  .fitem {
    display:flex; align-items:center; gap:8px; padding:9px 10px; margin-bottom:6px;
    background:var(--card2); border:1px solid var(--border); border-radius:10px;
    font-size:13px; cursor:grab;
  }
  .fitem.dragging { opacity:.4; }
  .fitem .grip { color:var(--muted); cursor:grab; }
  .fitem .idx { color:var(--accent); font-weight:700; min-width:20px; text-align:center; }
  .fitem .nm { flex:1; word-break:break-all; }
  .fitem .sz { color:var(--muted); font-size:11px; white-space:nowrap; }
  .fitem .mv { display:flex; flex-direction:column; gap:1px; }
  .fitem .mv button, .fitem .rm { background:none; border:none; color:var(--muted); cursor:pointer; font-size:12px; padding:0 4px; line-height:1.1; }
  .fitem .mv button:hover, .fitem .rm:hover { color:var(--fg); }

  /* ================= Batch ================= */
  .batch { margin-top:12px; background:var(--card2); border:1px solid var(--border); border-radius:12px; padding:12px; }
  .batchhead { display:flex; align-items:center; gap:8px; font-size:13px; font-weight:700; margin-bottom:8px; }
  .batchhead button { margin-left:auto; background:none; border:1px solid var(--border); color:var(--muted); border-radius:7px; font-size:11px; padding:2px 8px; cursor:pointer; }
  .batchhead button:hover { color:var(--fg); border-color:var(--accent); }
  .batchlist { max-height:130px; overflow-y:auto; font-size:11px; color:var(--muted); margin-bottom:10px; line-height:1.7; word-break:break-all; }

  /* ================= Options ================= */
  .formats { display:grid; grid-template-columns:repeat(2,1fr); gap:10px; }
  .fmt { position:relative; }
  .fmt input { position:absolute; opacity:0; }
  .fmt span {
    display:block; text-align:center; padding:11px 8px; border:1px solid var(--border);
    border-radius:12px; cursor:pointer; transition:.15s; background:var(--card2);
  }
  .fmt span:hover { border-color:var(--accent); }
  .fmt input:checked + span { border-color:var(--accent); background:var(--accent-soft); color:var(--accent); }
  .fmt b { display:block; font-size:14px; font-weight:700; }
  .fmt em { display:block; font-size:10.5px; font-style:normal; font-weight:400; color:var(--muted); margin-top:3px; line-height:1.35; }
  .fmt input:checked + span em { color:var(--accent); opacity:.85; }
  .fmt span.disabled { opacity:.45; cursor:not-allowed; }

  .opt { display:flex; align-items:flex-start; gap:9px; margin-top:16px; font-size:13px; cursor:pointer; user-select:none; }
  .opt > span { flex:1; }
  .opt input { width:16px; height:16px; accent-color:var(--accent); cursor:pointer; margin-top:3px; }
  .opt b { display:block; font-size:13px; }
  .opt .note { display:block; color:var(--muted); font-size:11px; margin-top:2px; line-height:1.45; }
  .opt.disabled { opacity:.45; pointer-events:none; }

  .splitrow { display:flex; align-items:center; gap:8px; margin-top:10px; font-size:13px; color:var(--muted); }
  .splitrow input {
    width:70px; padding:6px 8px; border:1px solid var(--border); border-radius:8px;
    background:var(--card2); color:var(--fg); font-size:13px;
  }
  .splitrow.disabled { opacity:.45; pointer-events:none; }

  .coverrow { display:flex; align-items:center; gap:10px; flex-wrap:wrap; margin-top:12px; }
  .coverinfo { display:flex; align-items:center; gap:8px; font-size:12px; color:var(--muted); }
  .coverinfo img { height:64px; border-radius:8px; border:1px solid var(--border); background:var(--bg); }
  .coverinfo button { background:none; border:none; color:var(--err); cursor:pointer; font-size:15px; padding:0 2px; }

  /* ================= Advanced ================= */
  .adv { margin-top:16px; border:1px solid var(--border); border-radius:12px; background:var(--card2); padding:10px 12px; }
  .adv summary { cursor:pointer; font-size:13px; font-weight:700; color:var(--muted); user-select:none; }
  .adv summary:hover { color:var(--fg); }
  .advrow { display:flex; align-items:center; gap:8px; margin-top:10px; font-size:12px; color:var(--muted); flex-wrap:wrap; }
  .advrow label { min-width:90px; }
  .advrow input[type=text] {
    flex:1; min-width:160px; padding:6px 8px; border:1px solid var(--border); border-radius:8px;
    background:var(--bg); color:var(--fg); font-size:12px; font-family:Consolas,monospace;
  }
  .advrow select {
    padding:5px 8px; border:1px solid var(--border); border-radius:8px;
    background:var(--bg); color:var(--fg); font-size:12px;
  }
  .advrow button {
    background:none; border:1px solid var(--border); color:var(--muted);
    border-radius:7px; font-size:11px; padding:3px 10px; cursor:pointer;
  }
  .advrow button:hover { color:var(--fg); border-color:var(--accent); }
  .advrow .note { font-size:11px; color:var(--muted); width:100%; }
  .advrow input[type=checkbox] { accent-color:var(--accent); }

  /* ================= Preview ================= */
  .pv { display:none; margin-top:18px; background:var(--card2); border:1px solid var(--border); border-radius:12px; padding:14px; }
  .pv.show { display:block; }
  .pvhead { display:flex; align-items:center; gap:8px; font-size:13px; font-weight:700; margin-bottom:10px; }
  .pvhead .pvref {
    margin-left:auto; background:none; border:1px solid var(--border); color:var(--muted);
    border-radius:7px; font-size:11px; padding:2px 10px; cursor:pointer;
  }
  .pvhead .pvref:hover { color:var(--fg); border-color:var(--accent); }
  .pvrow { display:flex; align-items:center; gap:8px; margin-bottom:8px; font-size:13px; }
  .pvrow label { color:var(--muted); width:44px; flex-shrink:0; }
  .pvrow input {
    flex:1; padding:7px 9px; border:1px solid var(--border); border-radius:8px;
    background:var(--bg); color:var(--fg); font-size:13px;
  }
  .pvstats { display:flex; flex-wrap:wrap; gap:6px 14px; font-size:12px; color:var(--muted); margin-top:4px; }
  .pvstats b { color:var(--accent); }
  .pvintro { margin-top:8px; font-size:11px; color:var(--muted); line-height:1.5; max-height:54px; overflow-y:auto; }
  .pvnote { font-size:12px; color:var(--muted); }

  /* ================= Buttons ================= */
  .btn {
    width:100%; padding:14px; border:none; border-radius:12px; margin-top:14px;
    background:linear-gradient(135deg,var(--accent),var(--accent2)); color:#fff;
    font-size:15px; font-weight:700; cursor:pointer; transition:.15s;
    box-shadow:0 4px 14px rgba(0,0,0,.25);
  }
  .btn:hover { filter:brightness(1.1); transform:translateY(-1px); }
  .btn:disabled { opacity:.5; cursor:not-allowed; transform:none; }
  .btn.sec {
    background:transparent; border:1px solid var(--border); color:var(--fg);
    box-shadow:none; padding:12px; line-height:1.45;
  }
  .btn.sec:hover { border-color:var(--accent); background:var(--card2); }
  .btn.sec .note { display:block; font-size:11px; font-weight:400; color:var(--muted); margin-top:3px; }
  .btn.tiny {
    width:auto; margin-top:10px; padding:9px 16px; background:transparent;
    border:1px solid var(--border); color:var(--fg); border-radius:10px; font-size:13px;
    box-shadow:none; font-weight:600;
  }
  .btn.tiny:hover { border-color:var(--accent); background:var(--card2); }
  .btn.tiny.danger { border-color:var(--err); color:var(--err); }
  .btn.tiny.danger:hover { background:var(--accent-soft); border-color:var(--err); }
  .btn.cancel {
    display:none; width:auto; margin-top:12px; padding:8px 22px; font-size:13px;
    background:transparent; border:1px solid var(--err); color:var(--err);
    border-radius:10px; cursor:pointer;
  }
  .btn.cancel.show { display:inline-block; }
  .btn.cancel:hover { background:var(--accent-soft); }
  .cancelwrap { text-align:center; }

  /* ================= Progress / status ================= */
  .progwrap { margin-top:18px; display:none; }
  .progwrap.show { display:block; }
  .bar { height:10px; background:var(--card2); border:1px solid var(--border); border-radius:6px; overflow:hidden; }
  .bar > i {
    display:block; height:100%; width:0%;
    background:linear-gradient(90deg,var(--accent),var(--accent2)); transition:width .4s;
  }
  .pmeta { display:flex; justify-content:space-between; margin-top:8px; font-size:12px; color:var(--muted); gap:10px; }
  .pmeta #pmsg { flex:1; }
  .warn {
    display:none; margin-top:14px; font-size:12px; color:#ff9f0a;
    background:var(--card2); border:1px solid rgba(255,159,10,.4);
    border-radius:10px; padding:10px 12px; line-height:1.5;
  }
  .warn.show { display:block; }
  .status { margin-top:16px; font-size:14px; min-height:20px; }
  .status.ok { color:var(--ok); }
  .status.err {
    color:var(--err); white-space:pre-wrap; text-align:left; font-size:12px;
    background:var(--card2); border:1px solid var(--border); border-radius:10px; padding:12px;
  }
  .dl {
    display:inline-block; margin-top:12px; padding:11px 22px; background:var(--ok);
    color:#fff; text-decoration:none; border-radius:10px; font-weight:700; font-size:14px;
  }
  .dl:hover { filter:brightness(1.1); }

  /* ================= History ================= */
  .hist { margin-top:26px; border-top:1px solid var(--border); padding-top:14px; }
  .histhead { font-size:13px; font-weight:700; margin-bottom:10px; display:flex; align-items:center; gap:8px; }
  .histhead .sp2 { flex:1; }
  .hclear {
    background:none; border:1px solid var(--border); color:var(--muted);
    border-radius:8px; font-size:11px; padding:3px 10px; cursor:pointer; font-weight:400;
  }
  .hclear:hover { color:var(--fg); border-color:var(--accent); }
  .clearmenu {
    display:flex; gap:8px; margin-bottom:10px; padding:10px;
    border:1px solid var(--border); border-radius:10px; background:var(--card2); flex-wrap:wrap;
  }
  .hitem {
    display:flex; align-items:center; gap:8px; padding:9px 12px; margin-bottom:7px;
    border:1px solid var(--border); border-radius:10px; font-size:12px;
    background:var(--card2); flex-wrap:wrap;
  }
  .hitem .hmsg { color:var(--muted); font-size:11px; flex:1; min-width:140px; }
  .hitem .hprog { flex-basis:100%; height:4px; background:var(--bg); border-radius:3px; overflow:hidden; }
  .hitem .hprog i { display:block; height:100%; background:var(--accent); transition:width .4s; }
  .hitem .hcancel { background:none; border:1px solid var(--err); color:var(--err); border-radius:7px; cursor:pointer; font-size:11px; padding:2px 8px; }
  .hitem .hcancel:hover { background:var(--accent-soft); }
  .hitem .expired { color:var(--muted); }
  .hitem a { color:var(--ok); text-decoration:none; font-weight:700; }
  .hitem.running { border-color:var(--accent); }
  .hitem.error { border-color:var(--err); }
  .hitem.cancelled { opacity:.6; }
  .hitem.queued { opacity:.7; }

  .foot { margin-top:24px; font-size:11px; color:var(--muted); text-align:center; }

  /* ================= Reader ================= */
  .reader {
    position:fixed; left:0; top:0; bottom:0; z-index:200; display:none;
    width:440px; background:var(--bg); color:var(--fg); flex-direction:column;
    border-right:1px solid var(--border);
    box-shadow:8px 0 30px rgba(0,0,0,.35);
  }
  .reader.show { display:flex; }
  body.pure .reader { background:var(--bg); }
  .rsep { height:1px; background:var(--border); margin:16px 0; }
  .rtoolbar {
    display:flex; align-items:center; gap:8px; padding:10px 16px;
    border-bottom:1px solid var(--border); background:var(--bg2); flex-wrap:wrap;
  }
  .rtitle { font-size:13px; font-weight:700; max-width:40vw; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
  .rbtn {
    background:none; border:1px solid var(--border); color:var(--fg);
    border-radius:8px; font-size:12px; padding:5px 12px; cursor:pointer;
  }
  .rbtn:hover { border-color:var(--accent); color:var(--accent); }
  .rsel {
    padding:5px 8px; border:1px solid var(--border); border-radius:8px;
    background:var(--bg); color:var(--fg); font-size:12px; max-width:34vw;
  }
  .ropen { flex:1; min-height:0; display:flex; overflow-y:auto; padding:24px; }
  .ropenbox {
    width:100%; max-width:520px; margin:0 auto;
    display:flex; flex-direction:column; min-height:100%;
  }
  .ropenbox .rrecent { margin-top:auto; }
  .ropenbox h2 { margin:0 0 8px; font-size:20px; }
  .rnote { color:var(--muted); font-size:13px; margin:0 0 18px; line-height:1.6; }
  .rrecent { margin-top:16px; }
  .rrecenthead { font-size:12px; font-weight:700; color:var(--muted); margin-bottom:8px; }
  .rbook { display:flex; align-items:center; gap:8px; padding:8px 10px; margin-bottom:6px; background:var(--card2); border:1px solid var(--border); border-radius:9px; font-size:12px; }
  .rbook .bt { flex:1; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; cursor:pointer; }
  .rbook .bt:hover { color:var(--accent); }
  .rbook .bmeta { color:var(--muted); font-size:11px; }
  .rbook .bx { background:none; border:1px solid var(--border); color:var(--muted); border-radius:6px; font-size:11px; padding:1px 7px; cursor:pointer; }
  .rbook .bx:hover { color:var(--err); border-color:var(--err); }
  .rbmlist { position:absolute; top:52px; right:14px; z-index:210; width:280px; max-height:50vh; overflow-y:auto; background:var(--card); border:1px solid var(--border); border-radius:12px; padding:10px; box-shadow:var(--shadow); }
  .rbmitem { padding:7px 9px; border-radius:7px; font-size:12px; cursor:pointer; }
  .rbmitem:hover { background:var(--accent-soft); color:var(--accent); }
  .rbtn.on { border-color:var(--accent); color:var(--accent); }
  .rstatus { margin-top:14px; font-size:13px; min-height:20px; }
  .rstatus.err { color:var(--err); }
  .rstatus.ok { color:var(--ok); }
  .rcontent {
    display:none; flex:1; overflow-y:auto; padding:26px 22px 60px;
    font-size:18px; line-height:1.9; max-width:900px; width:100%;
    margin:0 auto; box-sizing:border-box;
  }
  .rcontent p { margin:0 0 1em; word-break:break-word; }
  .rcontent .rhead {
    font-size:1.15em; font-weight:700; text-align:center;
    margin:0 0 1.4em; color:var(--accent);
  }
  .rloading { color:var(--muted); text-align:center; padding:40px 0; }
  /* theme dropdown floats above the reader while reading */
  body:has(.reader.show) .themes { position:fixed; top:64px; right:20px; z-index:300; }
  body.pure .rcontent { max-width:860px; margin:0 auto; font-size:20px; padding-top:38px; }
  @media (max-width:760px) {
    .reader { width:100vw; border-right:none; box-shadow:none; }
    .rcontent { padding:18px 14px 50px; }
    .rtitle { max-width:30vw; }
  }
  /* ================= web novel grabber ================= */
  .webgrab {
    margin-top:16px; padding:12px; border:1px dashed var(--border);
    border-radius:12px; background:var(--bg2);
  }
  .webgrab .webhead { font-weight:700; font-size:13px; margin-bottom:10px; display:flex; align-items:center; gap:6px; }
  .webgrab .webhead .note { font-weight:400; font-size:11px; color:var(--muted); }
  .webrow { display:flex; gap:8px; margin-bottom:8px; align-items:center; flex-wrap:wrap; }
  .webrow input[type=url] {
    flex:1; min-width:180px; padding:8px 10px; border-radius:9px;
    border:1px solid var(--border); background:var(--card); color:var(--fg); font-size:13px;
  }
  .webrow input[type=url]:focus { outline:none; border-color:var(--accent); }
  .webrow select {
    padding:7px 8px; border-radius:9px; border:1px solid var(--border);
    background:var(--card); color:var(--fg); font-size:12px; max-width:46%;
  }
  .webrow label { display:flex; align-items:center; gap:5px; font-size:12px; color:var(--muted); cursor:pointer; }
  .grabstatus { font-size:12px; margin-top:8px; word-break:break-all; }
  .grabstatus.ok { color:var(--ok); }
  .grabstatus.err { color:var(--err); }
  .grabstatus a { color:var(--accent); }
  .grabprog { display:none; margin-top:10px; align-items:center; gap:8px; }
  .grabprog.show { display:flex; }
  .grabprog .bar { flex:1; height:8px; border-radius:999px; background:var(--card); overflow:hidden; }
  .grabprog .bar i { display:block; height:100%; width:0; background:var(--accent); border-radius:999px; transition:width .3s; }
  .grabprog span { font-size:11px; color:var(--muted); white-space:nowrap; }
  .grabLog {
    display:none; margin-top:8px; max-height:150px; overflow-y:auto;
    font-size:11px; line-height:1.6; color:var(--muted);
    border:1px solid var(--border); border-radius:8px; padding:6px 8px;
    white-space:pre-wrap; word-break:break-all;
  }
  .grabLog.show { display:block; }
  .srcmgr { margin-top:10px; }
  .srcpanel { margin-top:8px; border:1px solid var(--border); border-radius:10px; padding:10px; background:var(--card); }
  .srclist .srcitem {
    display:flex; align-items:center; gap:8px; padding:6px 8px; margin-bottom:5px;
    background:var(--bg2); border:1px solid var(--border); border-radius:8px; font-size:12px;
  }
  .srcitem .snm { flex:1; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
  .srcitem .smeta { color:var(--muted); font-size:11px; white-space:nowrap; }
  .srcitem .sdel { background:none; border:1px solid var(--border); color:var(--muted); border-radius:6px; font-size:11px; padding:1px 7px; cursor:pointer; }
  .srcitem .sdel:hover { color:var(--err); border-color:var(--err); }
  .srcadd { margin-top:8px; }
  .srcadd summary, .srcadv summary { font-size:12px; color:var(--accent); cursor:pointer; padding:4px 0; }
  .srcrow { display:flex; gap:6px; align-items:center; margin:6px 0; flex-wrap:wrap; }
  .srcrow label { font-size:12px; color:var(--muted); min-width:64px; }
  .srcrow input[type=text], .srcrow input[type=url], .srcrow select, .srcadv textarea {
    padding:6px 8px; border-radius:7px; border:1px solid var(--border);
    background:var(--bg2); color:var(--fg); font-size:12px; flex:1; min-width:120px;
  }
  .srcadv textarea { width:100%; font-family:monospace; }
  .srcmsg { font-size:11px; }
  .srcmsg.ok { color:var(--ok); } .srcmsg.err { color:var(--err); }
  .rurlrow { margin:12px 0; }
  .rurlrow input {
    width:100%; box-sizing:border-box; padding:11px 13px; border-radius:10px;
    border:1px solid var(--border); background:var(--card); color:var(--fg);
    font-size:14px; min-height:44px;
  }
  .rurlrow input:focus { outline:none; border-color:var(--accent); }
  .rrecenthead { display:flex; align-items:center; justify-content:space-between; gap:8px; }
  .rclear {
    background:none; border:1px solid var(--border); color:var(--muted);
    border-radius:8px; font-size:11px; padding:2px 10px; cursor:pointer;
  }
  .rclear:hover { color:var(--err); border-color:var(--err); }
</style>
</head>
<body>
<div class="bgimg" id="bgimg"></div>
<video class="bgvideo" id="bgvideo" autoplay muted loop playsinline></video>
<div class="bgveil" id="bgveil"></div>
<div class="app">
  <header class="hd">
    <h1 class="logo">📚 TXT → 电子书</h1>
    <span class="tag" data-i18n="tag"></span>
    <span class="festchip" id="festChip"></span>
    <span class="lanchip" id="lanChip" title=""></span>
    <span class="sp"></span>
    <button class="langbtn" id="readerBtn" title="阅读" data-tip="tipReaderBtn">📖</button>
    <div class="themewrap">
      <button class="langbtn catbtn" id="themeBtn" title="主题与自定义背景">🐱</button>
      <div class="themes" id="themesBox">
        <span class="tlabel" data-i18n="theme"></span>
        <button class="theme" data-theme="default" style="background:#4f8cff" title="深空蓝(默认)"></button>
      <button class="theme" data-theme="aurora" style="background:#2dd4a7" title="极光绿"></button>
      <button class="theme" data-theme="sakura" style="background:#ff7ab8" title="樱花粉"></button>
      <button class="theme" data-theme="sunset" style="background:#ffb454" title="暖阳橙"></button>
      <button class="theme" data-theme="violet" style="background:#a78bfa" title="紫罗兰"></button>
      <button class="theme" data-theme="paper" style="background:#2f6f4f" title="纸张(浅色)"></button>
      <button class="theme" data-theme="ocean" style="background:#38bdf8" title="深海蓝"></button>
      <button class="theme" data-theme="graphite" style="background:#d4d4d8" title="石墨灰"></button>
      <button class="theme" data-theme="wine" style="background:#ef5f7a" title="酒红"></button>
      <button class="theme" data-theme="lime" style="background:#a3e635" title="青柠"></button>
      <button class="theme" data-theme="ice" style="background:#0e7490" title="冰川(浅色)"></button>
      <button class="theme" data-theme="sand" style="background:#c2703d" title="沙漠(浅色)"></button>
      <button class="theme" data-theme="miku" style="background:#39c5bb" title="初音绿(二次元)"></button>
      <button class="theme" data-theme="cyber" style="background:#ff2a6d" title="赛博朋克"></button>
      <button class="theme" data-theme="shinkai" style="background:#ff9d5c" title="新海诚(动漫)"></button>
      <button class="theme" data-theme="retro" style="background:#39ff14" title="复古游戏"></button>
      <button class="theme" data-theme="starry" style="background:#ffd76e" title="星空"></button>
      <button class="theme" data-theme="pastel" style="background:#ff8fb3" title="粉彩(二次元浅色)"></button>
      <button class="theme custom" data-theme="custom"><span data-i18n="customBg"></span></button>
      <input type="file" id="bgFile" accept="image/*,video/*" hidden>
      <div class="bgsection">
        <div class="bghead">
          <span data-i18n="bgTitle"></span>
          <label class="bgdelopt"><input type="checkbox" id="bgDelFiles" checked> <span data-i18n="bgDelFiles"></span></label>
          <button class="theme-reset" id="themeReset" title="取消应用(图片保留)"><span data-i18n="bgUnapply"></span></button>
          <button class="bgbtn" id="bgBatchBtn" data-i18n="bgBatch"></button>
        </div>
        <div class="bggrid" id="bgGrid"></div>
        <div class="bgbatchbar" id="bgBatchBar" style="display:none">
          <button class="btn tiny danger" id="bgDelSel" data-i18n="bgDelSel"></button>
          <button class="btn tiny" id="bgBatchCancel" data-i18n="bgCancel"></button>
        </div>
        <div class="bgrow">
          <label class="bgdelopt"><input type="checkbox" id="bgmCb"> <span data-i18n="bgmLabel"></span></label>
        </div>
        <div class="bgrow">
          <span data-i18n="langLabel"></span>
          <button class="bgbtn" id="langZh">中文</button>
          <button class="bgbtn" id="langEn">English</button>
        </div>
      </div>
      </div>
    </div>
  </header>

  <div class="main">
    <!-- ================= Left: input ================= -->
    <section class="col col-left">
      <div class="step" data-i18n="step1"></div>
      <div class="drop" id="drop">
        <input type="file" id="file" accept=".txt,.epub,.mobi,.azw,.azw3,.zip,.rar,.7z,text/plain" multiple>
        <div class="icon">📄</div>
        <div class="txt" data-i18n="dropTxt"></div>
        <div class="hint2" data-i18n="dropHint"></div>
        <div class="hint2" data-i18n="dropHint2"></div>
        <div class="fname" id="fname"></div>
      </div>
      <div class="flist" id="flist">
        <div class="hint" id="flistHint"></div>
        <div id="flistItems"></div>
      </div>
      <button class="btn tiny" id="folderBtn" style="margin-top:12px">📁 <span data-i18n="folder"></span></button>
      <input type="file" id="folderInput" webkitdirectory multiple hidden>
      <div class="batch" id="batchBox" style="display:none">
        <div class="batchhead"><span data-i18n="batchHead"></span><button id="batchClear">✕</button></div>
        <div class="batchlist" id="batchList"></div>
        <button class="btn" id="batchGo">⚡ <span data-i18n="batchGo"></span></button>
      </div>

      <div class="step" data-i18n="step2"></div>
      <div class="formats">
        <label class="fmt"><input type="radio" name="format" value="mobi" checked><span><b>MOBI</b><em data-i18n="fmtMobi"></em></span></label>
        <label class="fmt"><input type="radio" name="format" value="azw"><span><b>AZW</b><em data-i18n="fmtAzw"></em></span></label>
        <label class="fmt"><input type="radio" name="format" value="azw3"><span><b>AZW3</b><em data-i18n="fmtAzw3"></em></span></label>
        <label class="fmt"><input type="radio" name="format" value="epub"><span><b>EPUB</b><em data-i18n="fmtEpub"></em></span></label>
        <label class="fmt"><input type="radio" name="format" value="kfx"><span><b>KFX</b><em data-i18n="fmtKfx"></em></span></label>
        <label class="fmt"><input type="radio" name="format" value="txt"><span><b>TXT</b><em data-i18n="fmtTxt"></em></span></label>
      </div>

      <label class="opt" id="splitOpt">
        <input type="checkbox" id="split" checked>
        <span><b data-i18n="split"></b><span class="note" data-i18n="splitNote"></span></span>
      </label>
      <div class="splitrow" id="splitRow">
        <label for="splitMb" data-i18n="splitMb"></label>
        <input type="number" id="splitMb" min="1" max="500" step="1" value="5">
        <span data-i18n="splitMbNote"></span>
      </div>

      <label class="opt" id="autoOpt">
        <input type="checkbox" id="autoCb" checked>
        <span><b data-i18n="autoSec"></b><span class="note" data-i18n="autoSecNote"></span></span>
      </label>

      <label class="opt" id="adsOpt">
        <input type="checkbox" id="adsCb" checked>
        <span><b data-i18n="cleanAds"></b><span class="note" data-i18n="cleanAdsNote"></span></span>
      </label>

      <div class="coverrow" id="coverRow">
        <button type="button" class="btn tiny" id="coverBtn" data-tip="tipCover"><span data-i18n="coverBtn"></span></button>
        <input type="file" id="coverFile" accept="image/*" hidden>
        <span class="coverinfo" id="coverInfo" style="display:none">
          <img id="coverPrev" alt="cover">
          <span id="coverName"></span>
          <button type="button" id="coverRm" title="remove">✕</button>
        </span>
      </div>

      <details class="adv" id="adv">
        <summary>⚙ <span data-i18n="adv"></span></summary>
        <div class="advrow">
          <label data-i18n="advOutDir"></label>
          <input type="text" id="outDirInput" placeholder="D:\Books\输出">
          <button id="outDirSave" data-i18n="advOutSave"></button>
          <span class="note" data-i18n="advOutDirNote"></span>
        </div>
        <div class="advrow">
          <label data-i18n="advRe"></label>
          <input type="text" id="chapterRe" placeholder="e.g. ^第.+[章节回卷]">
          <button id="reReset" data-i18n="advReReset"></button>
          <span class="note" data-i18n="advReNote"></span>
        </div>
        <div class="advrow">
          <label data-i18n="advRetention"></label>
          <select id="retentionSel">
            <option value="">默认(6小时)</option>
            <option value="1">1 小时</option>
            <option value="24">24 小时</option>
            <option value="168">7 天</option>
          </select>
        </div>
        <div class="advrow">
          <label style="display:flex;align-items:center;gap:6px;cursor:pointer">
            <input type="checkbox" id="remindCb"> <span data-i18n="advRemind"></span>
          </label>
        </div>
        <div class="advrow">
          <label style="display:flex;align-items:center;gap:6px;cursor:pointer">
            <input type="checkbox" id="lanCb"> <span data-i18n="advLan"></span>
          </label>
          <span class="note" data-i18n="advLanNote" id="lanNote"></span>
        </div>
      </details>
    </section>

    <div class="resizer" id="resizer" title="drag"></div>

    <!-- ================= Right: preview / action / results ================= -->
    <section class="col col-right">
      <div class="pv" id="previewBox">
        <div class="pvhead">🔍 <span data-i18n="pvHead"></span><button class="pvref" id="pvRefresh" data-i18n="pvRefresh" data-tip="tipPvRefresh"></button></div>
        <div class="pvrow"><label data-i18n="pvTitle"></label><input id="titleInput" placeholder=""></div>
        <div class="pvrow"><label data-i18n="pvAuthor"></label><input id="authorInput" placeholder=""></div>
        <div class="pvstats" id="pvStats"></div>
        <div class="pvintro" id="pvIntro"></div>
      </div>

      <div class="step" data-i18n="step3"></div>
      <button class="btn" id="go" data-tip="tipGo">⚡ <span data-i18n="go"></span></button>
      <button class="btn sec" id="mergeBtn" data-tip="tipMerge"><span data-i18n="merge"></span><span class="note" data-i18n="mergeNote"></span></button>

      <div class="progwrap" id="progwrap">
        <div class="bar"><i id="barfill"></i></div>
        <div class="pmeta"><span id="pmsg">...</span><span id="peta"></span><span id="ppct">0%</span></div>
        <div class="cancelwrap"><button class="btn cancel" id="cancelBtn" data-i18n="cancel" data-tip="tipCancel"></button></div>
      </div>

      <div class="warn" id="warn"></div>
      <div class="status" id="status"></div>

      <div class="hist" id="hist">
        <div class="histhead"><span data-i18n="histHead"></span><span class="histcount" id="histCount"></span>
          <span class="sp2"></span>
          <button class="hclear" id="histClearBtn" title="clear">🗑 <span data-i18n="histClear"></span></button>
        </div>
        <div class="clearmenu" id="clearMenu" style="display:none">
          <button class="btn tiny" id="clearRecords" data-i18n="clearRecords"></button>
          <button class="btn tiny danger" id="clearAll" data-i18n="clearAll"></button>
          <button class="btn tiny" id="clearCancel" data-i18n="clearCancel"></button>
        </div>
        <div id="histList"></div>
      </div>

      <div class="foot" data-i18n="foot"></div>
    </section>
  </div>
</div>

<div class="reader" id="reader">
  <div class="rtoolbar" id="rtoolbar">
    <button class="rbtn" id="rBack" data-i18n="rBack"></button>
    <span class="rtitle" id="rTitle"></span>
    <span class="sp"></span>
    <select class="rsel" id="rChapter"></select>
    <button class="rbtn" id="rPrev" data-i18n="rPrev"></button>
    <button class="rbtn" id="rNext" data-i18n="rNext"></button>
    <button class="rbtn" id="rFontMinus">A−</button>
    <button class="rbtn" id="rFontPlus">A+</button>
    <button class="rbtn" id="rTheme" title="主题">🎨</button>
    <button class="rbtn" id="rBookmark" title="书签">🔖</button>
    <button class="rbtn" id="rBookmarks" title="书签列表">📑</button>
    <button class="rbtn" id="rMode" data-i18n="rPure"></button>
    <button class="rbtn" id="rClose">✕</button>
  </div>
  <div class="rbmlist" id="rbmlist" style="display:none"></div>
  <div class="ropen" id="ropen">
    <div class="ropenbox">
      <h2 data-i18n="rOpenTitle"></h2>
      <p class="rnote" data-i18n="rNote"></p>
      <button class="btn" id="rLocal" data-i18n="rLocalBtn"></button>
      <input type="file" id="rFile" accept=".txt,.zip" hidden>
      <div class="rsep"></div>
      <div class="webgrab">
        <div class="webhead">🌐 <span data-i18n="webHead"></span></div>
        <div class="webrow">
          <input type="url" id="grabUrl" placeholder="" autocomplete="off" spellcheck="false">
        </div>
        <div class="webrow">
          <select id="grabSource"></select>
          <select id="grabMode">
            <option value="auto" data-i18n="webModeAuto"></option>
            <option value="chapter" data-i18n="webModeChapter"></option>
            <option value="toc" data-i18n="webModeToc"></option>
          </select>
          <label data-tip="tipRender"><input type="checkbox" id="grabRender"> <span data-i18n="webRender"></span></label>
          <label><input type="checkbox" id="grabAds" checked> <span data-i18n="webAds"></span></label>
        </div>
        <div class="webrow">
          <button class="btn" id="grabBtn" data-tip="tipGrab">📥 <span data-i18n="webGo"></span></button>
          <button class="btn cancel" id="grabCancelBtn" style="display:none" data-tip="tipGrabCancel">✕ <span data-i18n="webCancel"></span></button>
        </div>
        <div class="grabstatus" id="grabStatus"></div>
        <div class="grabprog" id="grabProg"><div class="bar"><i id="grabBar"></i></div><span id="grabPct">0%</span></div>
        <div class="grabLog" id="grabLog"></div>
        <div class="srcmgr">
          <button class="btn tiny" id="srcMgrBtn" data-tip="tipSrcMgr">⚙️ <span data-i18n="srcMgr"></span></button>
          <div class="srcpanel" id="srcPanel" style="display:none">
            <div class="srcrow">
              <input type="url" id="srcProbeUrl" placeholder="">
              <button class="btn" id="srcProbeBtn" data-tip="tipProbe">🪄 <span data-i18n="srcProbe"></span></button>
            </div>
            <div class="srcmsg" id="srcProbeMsg"></div>
            <div class="srclist" id="srcList"></div>
            <details class="srcadd">
              <summary data-i18n="srcAdd"></summary>
              <div class="srcrow"><label data-i18n="srcName"></label><input type="text" id="srcName"></div>
              <div class="srcrow"><label data-i18n="srcHome"></label><input type="url" id="srcHome" placeholder="https://..."></div>
              <div class="srcrow"><label data-i18n="srcEnc"></label>
                <select id="srcEnc">
                  <option value="utf-8">UTF-8</option>
                  <option value="gbk">GBK</option>
                  <option value="">自动</option>
                </select>
              </div>
              <div class="srcrow"><label data-i18n="srcCont"></label>
                <input type="text" id="srcContTag" value="div" style="width:56px">
                <select id="srcContAttr"><option value="id">id</option><option value="class">class</option></select>
                <input type="text" id="srcContVal" placeholder="content">
              </div>
              <div class="srcrow"><label data-i18n="srcRe"></label><input type="text" id="srcTocRe" placeholder="第\\s*[0-9零一二三四五六七八九十百千两万]+\\s*[章节回卷]"></div>
              <details class="srcadv">
                <summary data-i18n="srcAdv"></summary>
                <textarea id="srcJson" rows="10" spellcheck="false"></textarea>
              </details>
              <div class="srcrow">
                <button class="btn" id="srcSave" data-i18n="srcSave"></button>
                <span class="srcmsg" id="srcMsg"></span>
              </div>
            </details>
          </div>
        </div>
      </div>
      <div class="rrecent">
        <div class="rrecenthead"><span data-i18n="rRecent"></span><button class="rclear" id="rClearRecent" data-i18n="rClearRecent" data-tip="tipClearRecent"></button></div>
        <div id="rRecentList"></div>
      </div>
      <div class="rstatus" id="rStatus"></div>
    </div>
  </div>
  <div class="rcontent" id="rContent"></div>
</div>

<script>
'use strict';
const fileInput = document.getElementById('file');
const drop = document.getElementById('drop');
const fname = document.getElementById('fname');
const flist = document.getElementById('flist');
const flistHint = document.getElementById('flistHint');
const flistItems = document.getElementById('flistItems');
const splitCb = document.getElementById('split');
const splitMb = document.getElementById('splitMb');
const splitRow = document.getElementById('splitRow');
const autoCb = document.getElementById('autoCb');
const adsCb = document.getElementById('adsCb');
const coverBtn = document.getElementById('coverBtn');
const coverFile = document.getElementById('coverFile');
const coverInfo = document.getElementById('coverInfo');
const coverPrev = document.getElementById('coverPrev');
const coverName = document.getElementById('coverName');
const coverRm = document.getElementById('coverRm');
const chapterRe = document.getElementById('chapterRe');
const outDirInput = document.getElementById('outDirInput');
const outDirSave = document.getElementById('outDirSave');
const reReset = document.getElementById('reReset');
const retentionSel = document.getElementById('retentionSel');
const remindCb = document.getElementById('remindCb');
const folderBtn = document.getElementById('folderBtn');
const folderInput = document.getElementById('folderInput');
const batchBox = document.getElementById('batchBox');
const batchList = document.getElementById('batchList');
const batchClear = document.getElementById('batchClear');
const batchGo = document.getElementById('batchGo');
const previewBox = document.getElementById('previewBox');
const pvRefresh = document.getElementById('pvRefresh');
const titleInput = document.getElementById('titleInput');
const authorInput = document.getElementById('authorInput');
const pvStats = document.getElementById('pvStats');
const pvIntro = document.getElementById('pvIntro');
const go = document.getElementById('go');
const mergeBtn = document.getElementById('mergeBtn');
const status = document.getElementById('status');
const warn = document.getElementById('warn');
const progwrap = document.getElementById('progwrap');
const barfill = document.getElementById('barfill');
const pmsg = document.getElementById('pmsg');
const peta = document.getElementById('peta');
const ppct = document.getElementById('ppct');
const cancelBtn = document.getElementById('cancelBtn');
const hist = document.getElementById('hist');
const histCount = document.getElementById('histCount');
const histList = document.getElementById('histList');
const langZh = document.getElementById('langZh');
const langEn = document.getElementById('langEn');
const grabUrl = document.getElementById('grabUrl');
const grabSource = document.getElementById('grabSource');
const grabMode = document.getElementById('grabMode');
const grabAds = document.getElementById('grabAds');
const grabRender = document.getElementById('grabRender');
const grabBtn = document.getElementById('grabBtn');
const grabCancelBtn = document.getElementById('grabCancelBtn');
const grabStatus = document.getElementById('grabStatus');
const grabProg = document.getElementById('grabProg');
const grabBar = document.getElementById('grabBar');
const grabPct = document.getElementById('grabPct');
const grabLog = document.getElementById('grabLog');
const bgmCb = document.getElementById('bgmCb');
const bgDelFiles = document.getElementById('bgDelFiles');
const themeBtn = document.getElementById('themeBtn');
const themesBox = document.getElementById('themesBox');
const festChip = document.getElementById('festChip');
const MAX_BYTES = 500 * 1024 * 1024;
const PREVIEW_MAX = 20 * 1024 * 1024;
const INPUT_EXTS = ['.txt', '.epub', '.mobi', '.azw', '.azw3', '.zip', '.rar', '.7z'];

// ================= i18n =================
const I18N = {
  zh: {
    tag: '本地转换 · 文件不上传 · MOBI / AZW / AZW3 / EPUB / KFX',
    theme: '主题', bgTitle: '自定义背景', customBg: '🌈 自定义背景', bgBatch: '批量删除', bgUnapply: '✕ 取消应用', bgDelSel: '删除选中', bgCancel: '取消', bgEmpty: '还没有保存的背景,点「自定义背景」上传;动态背景支持 GIF / MP4 / WEBM', rOpenTitle: '📖 阅读', rNote: '打开本地小说自动净化广告,记住阅读进度与书签,支持全屏纯净阅读。', rRecent: '📚 最近阅读', rResume: '已续读', rBookmark: '🔖 书签', rBookmarks: '📑 书签列表', rNoBookmarks: '暂无书签', rOpen: '打开', rDel: '删除', rEmptyRecent: '还没有阅读记录,打开一本书试试', rChN: '第', rChUnit: '章', rLocalBtn: '📂 打开本地小说(TXT / ZIP)', rUrlGoBtn: '📥 抓取网页', rUrlPh: '粘贴小说网页地址', rBack: '← 返回', rPrev: '上一章', rNext: '下一章', rPure: '⛶ 纯净', rNormal: '⛶ 普通', rLoading: '加载中...', rExpired: '会话已过期,请重新打开', rEmpty: '正文为空(网页可能需登录或为动态页面)', rBtn: '阅读', rOpened: '已打开', rChapters: '章', bgDelFiles: '同时删除本地文件', bgmLabel: '背景音乐(视频背景)', langLabel: '界面语言', step1: '① 选择文件(可多选)', step2: '② 格式与拆分', step3: '③ 开始转换',
    dropTxt: '点击或拖拽文件到这里', dropHint: 'TXT 可多选(如 1-100、101-200 章各一卷),自动按文件名数字排序合并',
    dropHint2: '也支持 EPUB / MOBI / AZW / AZW3 直接转格式(单文件);ZIP / RAR / 7Z 压缩包自动解压识别内部书籍',
    folder: '批量导入文件夹', batchHead: '📦 批量导入', batchGo: '批量转换',
    flistHint: '将按此顺序合并为一本,自动去除各卷重复的书名/作者头。拖拽或用 ↑↓ 调整:',
    fmtMobi: '所有 Kindle 都能读<br>最稳妥 · 推荐', fmtAzw: '老款 Kindle 专用<br>内容与 MOBI 相同', fmtAzw3: '新版 Kindle 专用<br>排版效果最好', fmtEpub: '通用格式<br>手机/平板/阅读器都能看', fmtKfx: '新版 Kindle 最佳<br>需装 Kindle Previewer', fmtTxt: '纯文本<br>电子书转回 TXT / 清洗下载',
    split: '自动拆分大文件', splitNote: '超过设定大小自动按章节拆成多本、打包 ZIP;不勾选则转成一整本',
    splitMb: '每本拆分大小', splitMbNote: 'MB(超过才拆分,在章节边界切分)',
    autoSec: '自动补充分节', autoSecNote: 'TXT 没有章节标题时,每约 1 万字插入"第N节",自动生成目录并支持拆分',
    cleanAds: '清理广告/水印行', cleanAdsNote: '自动删除"请收藏本站"、网址等常见小说网站垃圾行',
    coverBtn: '🖼 自定义封面(可选)', adv: '高级设置', advOutDir: '输出目录', advOutSave: '保存', advOutDirNote: '留空 = 程序目录下 output;保存后新任务输出到该目录(旧文件仍在原处可下载)', advRe: '章节规则(正则)', advReNote: '留空=自动识别;示例: ^第.+[章节回卷]', advReReset: '默认', advRetention: '文件保留', advRemind: '完成提醒(提示音+系统通知)', advLan: '允许手机/平板访问(同一WiFi)', advLanNote: '开启后需重启服务;手机打开下方地址即可', advLanNeedRestart: '已开启,重启服务后生效', advLanOff: '已关闭,仅本机可访问', lanChipTip: '点击复制手机访问地址', lanCopied: '已复制',
    pvHead: '转换预览', pvRefresh: '🔄 重新分析', pvTitle: '书名', pvAuthor: '作者',
    pvBig: '文件较大,已跳过自动预览(可直接转换)', pvLoading: '分析中...', pvNonTxt: '电子书输入,将直接转换格式:',
    pvChars: '字数', pvChapters: '章节', pvParts: '预计', pvAds: '清理广告', pvAutoSec: '自动分节', pvIntro: '简介',
    go: '开始转换', merge: '📄 仅合并 TXT,不转电子书', mergeNote: '把多个 TXT 合并成一个文件下载,可先预览或自行排版',
    cancel: '✕ 取消', histHead: '转换记录', histClear: '清理', clearRecords: '仅清理记录(保留文件)', clearAll: '记录 + 文件一并删除', clearCancel: '取消',
    foot: '本地转换 · 文件不上传外部服务器 · 基于 Calibre',
    notTxt: '仅支持 TXT / EPUB / MOBI / AZW / AZW3 / ZIP / RAR / 7Z 文件:', tooBig: '文件过大(上限 500 MB):', totalBig: '总大小超过 500 MB 上限,已取消添加:',
    ebookOnly: 'EPUB / MOBI 等电子书格式仅支持单文件,已切换为单文件模式',
    expired: '文件已过期', noFiles: '请先选择文件',
    needCalibre: '⚠️ 未检测到 Calibre(ebook-convert),无法转换。请安装 Calibre 或通过 start.bat 启动。',
    done: '转换完成', failed: '转换失败', cancelled: '已取消', jobLost: '任务已失效(服务可能重启过)', pack: '打包中…',
    upload: '上传中...', merging: '合并中…', merged: '已合并', download: '下载',
    webHead: '网页小说导入', webSub: '粘贴目录页或章节页 URL · 仅自用',
    webUrlPh: 'https://… 小说目录页或章节页', webMode: '模式',
    webModeAuto: '自动', webModeChapter: '章节页', webModeToc: '目录页',
    webSourceAuto: '自动识别书源', webGo: '抓取', webCancel: '取消',
    webNoUrl: '请先输入网址', webStart: '开始抓取...', webRead: '阅读',
    webConvert: '转电子书', webDone: '抓取完成', webAds: '清理广告', rUrlLabel: '网址', rClearRecent: '清空',
    webRender: '浏览器渲染', tipRender: 'JS 动态加载的站请勾选:用无头浏览器渲染后抓取(更慢但能抓动态页;不勾选时检测到 JS 页会自动启用)',
    srcMgr: '书源管理', srcAdd: '➕ 添加书源', srcName: '书源名称', srcHome: '站点主页', srcEnc: '编码',
    srcCont: '正文容器', srcRe: '目录章节正则', srcAdv: '高级:直接编辑 JSON',
    srcSave: '保存书源', srcDel: '删除', srcEmpty: '暂无自定义书源',
    srcSaved: '已保存,立即生效', srcErr: '保存失败', srcNeedName: '请填写书源名称和主页',
    srcNeedCont: '请填正文容器(或点上方🪄自动识别,最省事)',
    srcProbe: '自动识别', srcProbePh: '粘贴这本书的任意章节页 URL,自动识别书源配置',
    srcProbing: '识别中...', srcProbeOk: '已识别,检查下方配置后点保存', srcProbeFail: '识别失败',
    srcProbeDet: '识别到', tipProbe: '粘贴一个章节页,自动探测编码/正文容器/分页,生成书源配置',
    tipSrcMgr: '管理书源:增删改查,保存即生效',
    tipGo: '按当前设置转换所选文件(可先看右侧预览)', tipMerge: '只合并 TXT,不调用 Calibre 转换',
    tipGrab: '粘贴小说目录页或章节页网址后点击,抓取结果自动存入书库', tipGrabCancel: '取消正在进行的抓取',
    tipCancel: '取消当前正在转换/抓取的任务', tipReaderUrl: '粘贴小说网址(目录页/章节页),抓取后自动打开阅读',
    tipClearRecent: '清空全部阅读记录与本地书库', tipPvRefresh: '重新分析书名/字数/章节',
    tipCover: '上传自定义封面图片(不传则自动生成文字封面)', tipReaderBtn: '打开阅读器:本地小说或网页抓取',
    tipLan: '开启后手机/平板同一 WiFi 可访问(需重启服务生效)',
  },
  en: {
    tag: 'Local · files never leave your PC · MOBI / AZW / AZW3 / EPUB / KFX',
    theme: 'Theme', bgTitle: 'Custom backgrounds', customBg: '🌈 Custom bg', bgBatch: 'Batch delete', bgUnapply: '✕ Un-apply', bgDelSel: 'Delete selected', bgCancel: 'Cancel', bgEmpty: 'No saved backgrounds - click "Custom bg" to upload (animated: GIF / MP4 / WEBM)', rOpenTitle: '📖 Reader', rNote: 'Open local novels - ads auto-cleaned, progress & bookmarks remembered, fullscreen clean mode.', rRecent: '📚 Recent', rResume: 'resumed at', rBookmark: '🔖 Bookmark', rBookmarks: '📑 Bookmarks', rNoBookmarks: 'no bookmarks yet', rOpen: 'Open', rDel: 'Delete', rEmptyRecent: 'no reading history yet - open a book', rChN: 'ch. ', rChUnit: '', rLocalBtn: '📂 Open local novel (TXT / ZIP)', rUrlGoBtn: '📥 Fetch page', rUrlPh: 'paste a novel page URL', rBack: '← Back', rPrev: 'Prev', rNext: 'Next', rPure: '⛶ Clean', rNormal: '⛶ Normal', rLoading: 'Loading...', rExpired: 'session expired, reopen it', rEmpty: 'empty content (page may need login or JS)', rBtn: 'Reader', rOpened: 'opened', rChapters: 'ch', bgDelFiles: 'also delete file', bgmLabel: 'background music (video bg)', langLabel: 'Language', step1: '① Add files (multi-select)', step2: '② Format & splitting', step3: '③ Convert',
    dropTxt: 'Click or drag files here', dropHint: 'TXT: select several volumes (e.g. 1-100, 101-200); auto-sorted by number and merged',
    dropHint2: 'EPUB / MOBI / AZW / AZW3 inputs also supported (single file); ZIP / RAR / 7Z archives are auto-extracted',
    folder: 'Import folder (batch)', batchHead: '📦 Batch import', batchGo: 'Batch convert',
    flistHint: 'Merged in this order; repeated title/author headers removed. Drag or use ▲▼:',
    fmtMobi: 'Works on all Kindles<br>safest · recommended', fmtAzw: 'Older Kindles<br>same as MOBI', fmtAzw3: 'Newer Kindles<br>best typesetting', fmtEpub: 'Universal<br>phones/tablets/readers', fmtKfx: 'Newest Kindles<br>needs Previewer', fmtTxt: 'Plain text<br>convert ebooks back to TXT',
    split: 'Auto-split big files', splitNote: 'Split by chapter into volumes (ZIP) when larger than the size below; uncheck = one book',
    splitMb: 'Volume size', splitMbNote: 'MB (split only when larger; cuts at chapter edges)',
    autoSec: 'Auto-add sections', autoSecNote: 'No chapter headings? Insert "Section N" every ~10k chars for a TOC and splitting',
    cleanAds: 'Remove ad/watermark lines', cleanAdsNote: 'Strips "please bookmark" lines, URLs and other site junk',
    coverBtn: '🖼 Custom cover (optional)', adv: 'Advanced', advOutDir: 'Output folder', advOutSave: 'Save', advOutDirNote: 'leave empty = output/ next to the program; new jobs go there (old files stay downloadable)', advRe: 'Chapter rule (regex)', advReNote: 'leave empty = auto; e.g. ^Chapter [0-9]+', advReReset: 'Reset', advRetention: 'Keep files', advRemind: 'Completion alert (sound + notification)', advLan: 'Allow phone/tablet access (same WiFi)', advLanNote: 'restart required; open the address below on your phone', advLanNeedRestart: 'Enabled - works after restart', advLanOff: 'Disabled - local only', lanChipTip: 'Click to copy the phone URL', lanCopied: 'copied',
    pvHead: 'Preview', pvRefresh: '🔄 Re-analyze', pvTitle: 'Title', pvAuthor: 'Author',
    pvBig: 'Large file: preview skipped (convert directly)', pvLoading: 'Analyzing...', pvNonTxt: 'Ebook input, direct conversion:',
    pvChars: 'chars', pvChapters: 'chapters', pvParts: '≈', pvAds: 'ads removed', pvAutoSec: 'auto sections', pvIntro: 'Intro',
    go: 'Convert', merge: '📄 Merge TXT only', mergeNote: 'Merge multiple TXT into one downloadable file',
    cancel: '✕ Cancel', histHead: 'History', histClear: 'Clear', clearRecords: 'Clear records only (keep files)', clearAll: 'Clear records + delete files', clearCancel: 'Cancel',
    foot: 'Local conversion · files never uploaded · powered by Calibre',
    notTxt: 'Only TXT / EPUB / MOBI / AZW / AZW3 / ZIP / RAR / 7Z files:', tooBig: 'File too large (max 500 MB):', totalBig: 'Total exceeds 500 MB, file not added:',
    ebookOnly: 'Ebook formats allow a single file only',
    expired: 'expired', noFiles: 'Select files first',
    needCalibre: '⚠️ Calibre (ebook-convert) not found. Install Calibre or use start.bat.',
    done: 'Done', failed: 'Failed', cancelled: 'Cancelled', jobLost: 'Job lost (server restarted?)', pack: 'Packaging…',
    upload: 'Uploading...', merging: 'Merging…', merged: 'Merged', download: 'Download',
    webHead: 'Web novel import', webSub: 'paste a TOC or chapter URL · personal use only',
    webUrlPh: 'https://… novel TOC or chapter page', webMode: 'Mode',
    webModeAuto: 'Auto', webModeChapter: 'Chapter', webModeToc: 'TOC',
    webSourceAuto: 'Auto-detect source', webGo: 'Fetch', webCancel: 'Cancel',
    webNoUrl: 'Enter a URL first', webStart: 'Fetching...', webRead: 'Read',
    webConvert: 'Convert', webDone: 'Fetched', webAds: 'Clean ads', rUrlLabel: 'URL', rClearRecent: 'Clear',
    webRender: 'Browser render', tipRender: 'For JS-loaded sites: render with headless browser before scraping (slower, but fetches dynamic pages; auto-enables when a JS-only page is detected)',
    srcMgr: 'Source manager', srcAdd: '➕ Add source', srcName: 'Name', srcHome: 'Site home', srcEnc: 'Encoding',
    srcCont: 'Content container', srcRe: 'TOC chapter regex', srcAdv: 'Advanced: edit JSON',
    srcSave: 'Save source', srcDel: 'Delete', srcEmpty: 'no custom sources yet',
    srcSaved: 'saved - active now', srcErr: 'save failed', srcNeedName: 'fill in name and home URL',
    srcNeedCont: 'fill the content container - or just use 🪄 Auto-detect',
    srcProbe: 'Auto-detect', srcProbePh: 'paste any chapter URL of the book - auto-detect source config',
    srcProbing: 'detecting...', srcProbeOk: 'detected - check the config below, then Save', srcProbeFail: 'detection failed',
    srcProbeDet: 'detected', tipProbe: 'Paste a chapter URL to auto-detect encoding/container/paging',
    tipSrcMgr: 'Manage sources: add/remove, active immediately',
    tipGo: 'Convert selected files with current settings (see preview first)', tipMerge: 'Merge TXT only, no Calibre conversion',
    tipGrab: 'Paste a novel TOC/chapter URL, result is saved to the library', tipGrabCancel: 'Cancel the running grab',
    tipCancel: 'Cancel the running conversion/grab job', tipReaderUrl: 'Paste a novel URL (TOC/chapter), auto-opens in the reader after fetching',
    tipClearRecent: 'Clear all reading history and the local library', tipPvRefresh: 'Re-analyze title/chars/chapters',
    tipCover: 'Upload a custom cover (auto-generated if omitted)', tipReaderBtn: 'Reader: local novels or web fetching',
    tipLan: 'Allow phone/tablet access on the same WiFi (restart required)',
  },
};
let lang = 'zh';
try { lang = localStorage.getItem('txt2ebook_lang') || 'zh'; } catch (e) {}
function t(k) { return (I18N[lang] && I18N[lang][k]) || I18N.zh[k] || k; }
function setLang(l) {
  lang = l;
  try { localStorage.setItem('txt2ebook_lang', lang); } catch (e) {}
  applyLang();
  renderList();
  renderBatch();
}
function applyLang() {
  document.documentElement.setAttribute('lang', lang === 'zh' ? 'zh-CN' : 'en');
  langZh.classList.toggle('on', lang === 'zh');
  langEn.classList.toggle('on', lang === 'en');
  document.querySelectorAll('[data-i18n]').forEach(el => { el.innerHTML = t(el.dataset.i18n); });
  document.querySelectorAll('[data-tip]').forEach(el => { el.title = t(el.dataset.tip); });
  grabUrl.placeholder = t('webUrlPh');
  const sa = grabSource.options[0];
  if (sa) sa.textContent = t('webSourceAuto');
}
langZh.addEventListener('click', () => setLang('zh'));
langEn.addEventListener('click', () => setLang('en'));

// ================= theme / custom bg / resizer =================
const bgimg = document.getElementById('bgimg');
const bgFile = document.getElementById('bgFile');
const themeReset = document.getElementById('themeReset');
const resizer = document.getElementById('resizer');
const colLeft = document.querySelector('.col-left');
let bgList = [];
let bgSelected = null;
let bgSelMode = false;
const selectedIds = new Set();
let bgmOn = false;
try { bgmOn = localStorage.getItem('txt2ebook_bgm') === 'on'; } catch (e) {}
function isVideoBg() {
  const b = bgList.find(x => x.id === bgSelected);
  return !!b && b.kind === 'video';
}
function syncMusicBtn() {
  bgmCb.checked = bgmOn;
  bgmCb.disabled = !isVideoBg();
}
const CUSTOM_VARS = ['--bg','--bg2','--card','--card2','--accent','--accent2','--accent-soft','--border'];
function clearCustomVars() {
  const st = document.documentElement.style;
  CUSTOM_VARS.forEach(v => st.removeProperty(v));
}
function setTheme(t) {
  document.documentElement.setAttribute('data-theme', t);
  try { localStorage.setItem('txt2ebook_theme', t); } catch (e) {}
  document.querySelectorAll('.theme').forEach(b => b.classList.toggle('active', b.dataset.theme === t));
  const isCustom = (t === 'custom' && !!bgSelected);
  document.documentElement.classList.toggle('hasbg', isCustom);
  themeReset.style.display = bgSelected ? 'inline-block' : 'none';
  if (!isCustom) {
    bgimg.style.backgroundImage = '';
    bgvideo.pause();
    bgvideo.removeAttribute('src');
    bgvideo.load();
    bgvideo.classList.remove('show');
    clearCustomVars();
    return;
  }
  if (isVideoBg()) {
    bgimg.style.backgroundImage = '';
    bgvideo.classList.add('show');
    bgvideo.muted = !bgmOn;
    if (bgvideo.getAttribute('src') !== '/backgrounds/' + bgSelected) {
      bgvideo.src = '/backgrounds/' + bgSelected;
    }
    bgvideo.play().catch(() => {});
    syncMusicBtn();
  } else {
    bgvideo.pause();
    bgvideo.classList.remove('show');
    bgimg.style.backgroundImage = 'url(/backgrounds/' + bgSelected + ')';
  }
  applyBgPalette();
}
function applyBgPalette() {
  if (isVideoBg()) {
    const v = bgvideo;
    const draw = () => {
      try {
        const c = document.createElement('canvas');
        c.width = v.videoWidth || 320;
        c.height = v.videoHeight || 240;
        c.getContext('2d').drawImage(v, 0, 0, c.width, c.height);
        applyCustomVars(c);
      } catch (e) {}
    };
    if (v.readyState >= 2) draw();
    else v.addEventListener('loadeddata', draw, { once: true });
    return;
  }
  const img = new Image();
  img.onload = () => { applyCustomVars(img); };
  img.src = '/backgrounds/' + bgSelected;
}
bgmCb.addEventListener('change', () => {
  bgmOn = bgmCb.checked;
  bgvideo.muted = !bgmOn;
  try { localStorage.setItem('txt2ebook_bgm', bgmOn ? 'on' : 'off'); } catch (e) {}
  syncMusicBtn();
});
function rgbToHsl(r, g, b) {
  r /= 255; g /= 255; b /= 255;
  const mx = Math.max(r, g, b), mn = Math.min(r, g, b);
  let h = 0, s = 0; const l = (mx + mn) / 2;
  if (mx !== mn) {
    const d = mx - mn;
    s = l > 0.5 ? d / (2 - mx - mn) : d / (mx + mn);
    if (mx === r) h = (g - b) / d + (g < b ? 6 : 0);
    else if (mx === g) h = (b - r) / d + 2;
    else h = (r - g) / d + 4;
    h /= 6;
  }
  return [h * 360, s, l];
}
function hslToRgb(h, s, l) {
  h /= 360; let r, g, b;
  if (s === 0) { r = g = b = l; }
  else {
    const hue2 = (p, q, t2) => {
      if (t2 < 0) t2 += 1; if (t2 > 1) t2 -= 1;
      if (t2 < 1/6) return p + (q - p) * 6 * t2;
      if (t2 < 1/2) return q;
      if (t2 < 2/3) return p + (q - p) * (2/3 - t2) * 6;
      return p;
    };
    const q = l < 0.5 ? l * (1 + s) : l + s - l * s;
    const p = 2 * l - q;
    r = hue2(p, q, h + 1/3); g = hue2(p, q, h); b = hue2(p, q, h - 1/3);
  }
  return [Math.round(r * 255), Math.round(g * 255), Math.round(b * 255)];
}
function applyCustomVars(img) {
  const c = document.createElement('canvas');
  c.width = 48; c.height = 48;
  const ctx = c.getContext('2d');
  ctx.drawImage(img, 0, 0, 48, 48);
  const d = ctx.getImageData(0, 0, 48, 48).data;
  let r = 0, g = 0, b = 0, n = 0, sr = 0, sg = 0, sb = 0, sn = 0;
  for (let i = 0; i < d.length; i += 4) {
    const rr = d[i], gg = d[i+1], bb = d[i+2];
    r += rr; g += gg; b += bb; n++;
    const mx = Math.max(rr, gg, bb), mn = Math.min(rr, gg, bb);
    if (mx > 40 && (mx - mn) / mx > 0.25) { sr += rr; sg += gg; sb += bb; sn++; }
  }
  const avg = [r / n | 0, g / n | 0, b / n | 0];
  const sat = [sn ? sr / sn | 0 : avg[0], sn ? sg / sn | 0 : avg[1], sn ? sb / sn | 0 : avg[2]];
  const [h, s] = rgbToHsl(sat[0], sat[1], sat[2]);
  const ac = hslToRgb(h, Math.min(0.8, Math.max(0.45, s)), 0.62);
  const ac2 = hslToRgb(h, Math.min(0.8, Math.max(0.45, s)), 0.45);
  const v = {
    '--bg': 'rgb(' + hslToRgb(h, Math.min(0.30, s * 0.4), 0.10).join(',') + ')',
    '--bg2': 'rgb(' + hslToRgb(h, Math.min(0.32, s * 0.45), 0.13).join(',') + ')',
    '--card': 'rgb(' + hslToRgb(h, Math.min(0.28, s * 0.35), 0.17).join(',') + ')',
    '--card2': 'rgb(' + hslToRgb(h, Math.min(0.25, s * 0.32), 0.12).join(',') + ')',
    '--accent': 'rgb(' + ac.join(',') + ')',
    '--accent2': 'rgb(' + ac2.join(',') + ')',
    '--accent-soft': 'rgba(' + ac.join(',') + ',.16)',
    '--border': 'rgb(' + hslToRgb(h, Math.min(0.2, s * 0.25), 0.24).join(',') + ')',
  };
  const st = document.documentElement.style;
  for (const k in v) st.setProperty(k, v[k]);
}
document.querySelectorAll('.theme').forEach(b => b.addEventListener('click', () => {
  if (b.dataset.theme === 'custom') { bgFile.click(); return; }
  setTheme(b.dataset.theme);
}));
themeBtn.addEventListener('click', (e) => {
  e.stopPropagation();
  themesBox.classList.toggle('open');
});
document.addEventListener('click', (e) => {
  if (!themesBox.contains(e.target)) themesBox.classList.remove('open');
});
bgFile.addEventListener('change', () => {
  const f = bgFile.files[0];
  bgFile.value = '';
  if (!f) return;
  const isVid = /^video\//.test(f.type) || /\.(mp4|webm)$/i.test(f.name);
  const isGif = /^image\/gif$/i.test(f.type) || /\.gif$/i.test(f.name);
  if (!/^image\//.test(f.type) && !isVid) {
    status.className = 'status err';
    status.textContent = '❌ ' + (lang === 'zh' ? '仅支持图片或视频' : 'images/videos only');
    return;
  }
  if (isVid || isGif) {
    // animated media: upload the original untouched (canvas would flatten it)
    uploadBg(f, f.name);
    return;
  }
  // static image: downscale to 1920px wide first, then upload
  const rd = new FileReader();
  rd.onload = () => {
    const img = new Image();
    img.onload = async () => {
      const scale = Math.min(1, 1920 / img.width);
      const w = Math.max(1, Math.round(img.width * scale));
      const h = Math.max(1, Math.round(img.height * scale));
      const c = document.createElement('canvas');
      c.width = w; c.height = h;
      c.getContext('2d').drawImage(img, 0, 0, w, h);
      let out = null;
      try { out = c.toDataURL('image/jpeg', 0.82); } catch (e) { out = null; }
      if (!out) { status.className = 'status err'; status.textContent = '❌ image processing failed'; return; }
      uploadBg(dataUrlToBlob(out), (f.name.replace(/\.[^.]+$/, '') || 'bg') + '.jpg');
    };
    img.onerror = () => { status.className = 'status err'; status.textContent = '❌ bad image'; };
    img.src = rd.result;
  };
  rd.readAsDataURL(f);
});
async function uploadBg(blob, filename) {
  const fd = new FormData();
  fd.append('bg', blob, filename);
  try {
    const r = await fetch('/bg/upload', { method: 'POST', body: fd });
    const j = await r.json();
    if (!j.ok) throw new Error(j.error || '');
    bgList = j.bgs || [];
    bgSelected = j.id;
    renderBgGallery();
    setTheme('custom');
    status.className = 'status ok';
    status.textContent = '✅ ' + (lang === 'zh' ? '背景已保存并应用' : 'background saved & applied');
  } catch (e) {
    status.className = 'status err';
    status.textContent = '❌ ' + e.message;
  }
}
function dataUrlToBlob(dataUrl) {
  const i = dataUrl.indexOf(',');
  const meta = dataUrl.slice(5, i);
  const bin = atob(dataUrl.slice(i + 1));
  const arr = new Uint8Array(bin.length);
  for (let k = 0; k < bin.length; k++) arr[k] = bin.charCodeAt(k);
  return new Blob([arr], { type: (meta.split(';')[0] || 'image/jpeg') });
}
themeReset.addEventListener('click', async () => {
  // un-apply only; saved images are kept (delete via the gallery)
  try {
    await fetch('/bg/select', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ id: '' }) });
  } catch (e) {}
  bgSelected = null;
  renderBgGallery();
  setTheme('default');
  status.className = 'status';
  status.textContent = lang === 'zh' ? '已取消应用背景(图片仍保留,可在主题面板删除)' : 'background un-applied (image kept) - delete it in the theme panel';
});

// ---- background gallery ----
const bgGrid = document.getElementById('bgGrid');
const bgBatchBtn = document.getElementById('bgBatchBtn');
const bgBatchBar = document.getElementById('bgBatchBar');
const bgDelSel = document.getElementById('bgDelSel');
const bgBatchCancel = document.getElementById('bgBatchCancel');

function renderBgGallery() {
  const section = document.querySelector('.bgsection');
  if (!bgList.length) {
    bgGrid.innerHTML = '<div class="bgempty">' + t('bgEmpty') + '</div>';
    section.classList.remove('selmode');
    bgBatchBar.style.display = 'none';
    bgBatchBtn.textContent = t('bgBatch');
    return;
  }
  bgGrid.innerHTML = bgList.map(b => {
    const sel = (!bgSelMode && b.id === bgSelected) ? ' sel' : '';
    const pick = (bgSelMode && selectedIds.has(b.id)) ? ' pick' : '';
    const vid = b.kind === 'video' ? ' vid' : '';
    return '<div class="bgthumb' + sel + pick + vid + '" data-id="' + b.id + '" title="' + esc(b.name) + '">'
      + '<span class="x" data-id="' + b.id + '">✕</span>'
      + '<span class="ck">✓</span></div>';
  }).join('');
  bgGrid.querySelectorAll('.bgthumb').forEach(td => {
    if (td.classList.contains('vid')) td.style.background = '#000';
    else td.style.backgroundImage = 'url(/backgrounds/' + td.dataset.id + ')';
  });
}

function toggleSelMode(on) {
  bgSelMode = on;
  selectedIds.clear();
  document.querySelector('.bgsection').classList.toggle('selmode', on);
  bgBatchBar.style.display = on ? 'flex' : 'none';
  bgBatchBtn.textContent = on ? t('bgCancel') : t('bgBatch');
  renderBgGallery();
}

async function deleteBgs(ids) {
  try {
    const r = await fetch('/bg/delete', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ ids: ids, delete_files: bgDelFiles.checked }) });
    const j = await r.json();
    if (!j.ok) throw new Error(j.error || '');
    bgList = j.bgs || [];
    if (bgSelected && !bgList.some(b => b.id === bgSelected)) {
      bgSelected = null;
      setTheme('default');
    }
    renderBgGallery();
    status.className = 'status ok';
    status.textContent = '✅ ' + (lang === 'zh' ? '已删除 ' : 'deleted ') + ids.length;
  } catch (e) {
    status.className = 'status err';
    status.textContent = '❌ ' + e.message;
  }
}

async function selectBg(id) {
  try {
    const r = await fetch('/bg/select', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ id: id }) });
    const j = await r.json();
    if (!j.ok) throw new Error(j.error || '');
    bgSelected = id;
    renderBgGallery();
    setTheme('custom');
  } catch (e) {
    status.className = 'status err';
    status.textContent = '❌ ' + e.message;
  }
}

async function loadBgList() {
  try {
    const r = await fetch('/bg/list');
    const j = await r.json();
    if (!j.ok) return;
    bgList = j.bgs || [];
    // only trust the saved selection if the image still exists
    bgSelected = (j.selected && bgList.some(b => b.id === j.selected)) ? j.selected : null;
    renderBgGallery();
  } catch (e) {}
}

bgGrid.addEventListener('click', async (ev) => {
  const x = ev.target.closest('.x');
  if (x) {
    ev.stopPropagation();
    if (!confirm(lang === 'zh' ? '删除这张背景?' : 'Delete this background?')) return;
    await deleteBgs([x.dataset.id]);
    return;
  }
  const thumb = ev.target.closest('.bgthumb');
  if (!thumb) return;
  if (bgSelMode) {
    const id = thumb.dataset.id;
    if (selectedIds.has(id)) selectedIds.delete(id); else selectedIds.add(id);
    thumb.classList.toggle('pick', selectedIds.has(id));
    bgDelSel.textContent = t('bgDelSel') + ' (' + selectedIds.size + ')';
    return;
  }
  await selectBg(thumb.dataset.id);
});
bgBatchBtn.addEventListener('click', () => toggleSelMode(!bgSelMode));
bgBatchCancel.addEventListener('click', () => toggleSelMode(false));
bgDelSel.addEventListener('click', async () => {
  if (!selectedIds.size) return;
  if (!confirm(lang === 'zh' ? ('删除 ' + selectedIds.size + ' 张背景?')
                             : ('Delete ' + selectedIds.size + ' background(s)?'))) return;
  await deleteBgs([...selectedIds]);
  toggleSelMode(false);
});
let colW = 440;
try { colW = parseInt(localStorage.getItem('txt2ebook_colw') || '440', 10); } catch (e) {}
colW = Math.min(820, Math.max(300, colW));
colLeft.style.width = colW + 'px';
resizer.addEventListener('mousedown', (e) => {
  e.preventDefault();
  const startX = e.clientX, startW = colLeft.getBoundingClientRect().width;
  resizer.classList.add('drag');
  document.body.style.cursor = 'col-resize';
  document.body.style.userSelect = 'none';
  const onMove = (ev) => {
    colW = Math.min(820, Math.max(300, startW + ev.clientX - startX));
    colLeft.style.width = colW + 'px';
  };
  const onUp = () => {
    document.removeEventListener('mousemove', onMove);
    document.removeEventListener('mouseup', onUp);
    resizer.classList.remove('drag');
    document.body.style.cursor = '';
    document.body.style.userSelect = '';
    try { localStorage.setItem('txt2ebook_colw', String(colW)); } catch (e) {}
  };
  document.addEventListener('mousemove', onMove);
  document.addEventListener('mouseup', onUp);
});

// ================= festival easter egg =================
function detectFestival(d) {
  const solar = {
    '1-1':['元旦','🎆'], '2-14':['情人节','💘'], '3-8':['妇女节','🌷'], '4-1':['愚人节','🤡'],
    '5-1':['劳动节','🛠️'], '6-1':['儿童节','🎈'], '10-1':['国庆节','🎉'], '10-31':['万圣节','🎃'],
    '12-24':['平安夜','🎄'], '12-25':['圣诞节','🎄'], '12-31':['跨年夜','🎇'],
  };
  const key = (d.getMonth() + 1) + '-' + d.getDate();
  if (solar[key]) return solar[key];
  try {
    const fmt = new Intl.DateTimeFormat('zh-CN-u-ca-chinese', { month: 'numeric', day: 'numeric' });
    const p = fmt.formatToParts(d);
    const m = p.find(x => x.type === 'month'), dd = p.find(x => x.type === 'day');
    if (m && dd) {
      const mon = parseInt(String(m.value).replace(/\D/g, ''), 10);
      const day = parseInt(String(dd.value).replace(/\D/g, ''), 10);
      const lunar = {
        '1-1':['春节','🧧'], '1-15':['元宵节','🏮'], '5-5':['端午节','🐲'],
        '7-7':['七夕','🌌'], '8-15':['中秋节','🌕'], '9-9':['重阳节','🍂'],
      };
      if (lunar[mon + '-' + day]) return lunar[mon + '-' + day];
    }
  } catch (e) {}
  return null;
}
const FEST = detectFestival(new Date());
if (FEST) {
  festChip.style.display = 'inline-flex';
  festChip.textContent = FEST.emoji + ' ' + FEST.name;
  festChip.title = (lang === 'zh' ? FEST.name + '快乐!' : 'Happy ' + FEST.name + '!');
  for (let i = 0; i < 12; i++) {
    const s = document.createElement('span');
    s.className = 'rain';
    s.textContent = FEST.emoji;
    s.style.left = (Math.random() * 100) + 'vw';
    s.style.animationDelay = (Math.random() * 8) + 's';
    s.style.fontSize = (14 + Math.random() * 18) + 'px';
    document.body.appendChild(s);
  }
}

// ================= health =================
fetch('/health').then(r => r.json()).then(h => {
  if (!h.ok) {
    status.className = 'status err';
    status.textContent = t('needCalibre');
  } else if (!h.kindle_previewer) {
    const kfx = document.querySelector('input[name=format][value=kfx]');
    if (kfx) {
      kfx.disabled = true;
      if (kfx.nextElementSibling) kfx.nextElementSibling.classList.add('disabled');
      if (kfx.checked) {
        document.querySelector('input[name=format][value=mobi]').checked = true;
        savePrefs();
      }
    }
  }
}).catch(() => {});

// ================= prefs =================
let prefs = { format: 'mobi', split: true, splitMb: 5, auto: true, ads: true, re: '', retention: '', remind: true };
try { prefs = Object.assign(prefs, JSON.parse(localStorage.getItem('txt2ebook_prefs') || '{}')); } catch (e) {}
document.querySelectorAll('input[name=format]').forEach(r => { r.checked = (r.value === prefs.format); });
splitCb.checked = prefs.split !== false;
splitMb.value = Math.min(500, Math.max(1, parseInt(prefs.splitMb, 10) || 5));
autoCb.checked = prefs.auto !== false;
adsCb.checked = prefs.ads !== false;
chapterRe.value = prefs.re || '';
retentionSel.value = prefs.retention || '';
remindCb.checked = prefs.remind !== false;
if (!document.querySelector('input[name=format]:checked')) {
  document.querySelector('input[name=format][value=mobi]').checked = true;
}
function savePrefs() {
  try {
    localStorage.setItem('txt2ebook_prefs', JSON.stringify({
      format: document.querySelector('input[name=format]:checked').value,
      split: splitCb.checked,
      splitMb: parseInt(splitMb.value, 10) || 5,
      auto: autoCb.checked,
      ads: adsCb.checked,
      re: chapterRe.value.trim(),
      retention: retentionSel.value,
      remind: remindCb.checked,
    }));
  } catch (e) {}
}
function syncSplitRow() { splitRow.classList.toggle('disabled', !splitCb.checked); }
document.querySelectorAll('input[name=format]').forEach(r => r.addEventListener('change', () => { savePrefs(); schedulePreview(); }));
splitCb.addEventListener('change', () => { syncSplitRow(); savePrefs(); schedulePreview(); });
splitMb.addEventListener('change', () => { savePrefs(); schedulePreview(); });
autoCb.addEventListener('change', () => { savePrefs(); schedulePreview(); });
adsCb.addEventListener('change', () => { savePrefs(); schedulePreview(); });
chapterRe.addEventListener('change', () => { savePrefs(); schedulePreview(); });
retentionSel.addEventListener('change', savePrefs);
remindCb.addEventListener('change', savePrefs);
reReset.addEventListener('click', () => { chapterRe.value = ''; savePrefs(); schedulePreview(); });
syncSplitRow();

// ---- output directory + LAN access ----
const lanCb = document.getElementById('lanCb');
const lanNote = document.getElementById('lanNote');
const lanChip = document.getElementById('lanChip');
function showLanChip(ip) {
  const url = ip ? ('http://' + ip + ':' + (window.location.port || '8765')) : '';
  if (!url) { lanChip.classList.remove('show'); return; }
  lanChip.textContent = '📱 ' + url;
  lanChip.title = t('lanChipTip');
  lanChip.classList.add('show');
}
lanChip.addEventListener('click', async () => {
  const url = lanChip.textContent.replace(/^📱\s*/, '').trim();
  if (!url) return;
  try { await navigator.clipboard.writeText(url); } catch (e) {}
  const old = lanChip.textContent;
  lanChip.textContent = '✅ ' + t('lanCopied');
  setTimeout(() => { lanChip.textContent = old; }, 1500);
});
fetch('/config').then(r => r.json()).then(c => {
  if (c.ok) outDirInput.value = c.output_dir || '';
  if (lanCb && c.lan !== undefined) lanCb.checked = !!c.lan;
  showLanChip(c.lan ? (c.lan_ip || '') : '');
}).catch(() => {});
outDirSave.addEventListener('click', async () => {
  outDirSave.disabled = true;
  try {
    const r = await fetch('/config', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ output_dir: outDirInput.value.trim() }),
    });
    const c = await r.json();
    if (!c.ok) throw new Error(c.error || '');
    status.className = 'status ok';
    status.textContent = '✅ ' + (lang === 'zh' ? '输出目录已保存: ' : 'output dir saved: ') + c.effective;
  } catch (e) {
    status.className = 'status err';
    status.textContent = '❌ ' + e.message;
  } finally {
    outDirSave.disabled = false;
  }
});
if (lanCb) lanCb.addEventListener('change', async () => {
  lanCb.disabled = true;
  try {
    const r = await fetch('/config', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ lan: lanCb.checked }),
    });
    const c = await r.json();
    if (!c.ok) throw new Error(c.error || '');
    if (c.restart) {
      status.className = 'status ok';
      if (c.lan) {
        const ip = c.lan_ip || window.location.hostname;
        status.innerHTML = '✅ ' + t('advLanNeedRestart')
          + ' <b>http://' + esc(ip) + ':8765</b>';
        showLanChip(ip);
      } else {
        status.className = 'status ok';
        status.textContent = '✅ ' + t('advLanOff');
        showLanChip('');
      }
    }
  } catch (e) {
    lanCb.checked = !lanCb.checked;
    status.className = 'status err';
    status.textContent = '❌ ' + e.message;
  } finally {
    lanCb.disabled = false;
  }
});

// ================= cover =================
coverBtn.addEventListener('click', () => coverFile.click());
coverFile.addEventListener('change', () => {
  const f = coverFile.files[0];
  if (!f) return;
  if (!/^image\//.test(f.type)) {
    status.className = 'status err'; status.textContent = '❌ image only';
    coverFile.value = '';
    return;
  }
  const rd = new FileReader();
  rd.onload = () => { coverPrev.src = rd.result; };
  rd.readAsDataURL(f);
  coverName.textContent = f.name;
  coverInfo.style.display = 'flex';
});
coverRm.addEventListener('click', () => { coverFile.value = ''; coverInfo.style.display = 'none'; coverPrev.src = ''; });

// ================= files =================
let files = [];
let batchFiles = [];
let pollTimer = null;
let dragIdx = null;
let titleEdited = false;
let authorEdited = false;

function isTxtName(n) { return n.toLowerCase().endsWith('.txt'); }
function isEbookName(n) {
  const e = '.' + n.split('.').pop().toLowerCase();
  return ['.epub', '.mobi', '.azw', '.azw3'].includes(e);
}
function isArchiveName(n) {
  const e = '.' + n.split('.').pop().toLowerCase();
  return ['.zip', '.rar', '.7z'].includes(e);
}

drop.addEventListener('click', () => fileInput.click());
['dragover','dragenter'].forEach(ev => drop.addEventListener(ev, e => { e.preventDefault(); drop.classList.add('drag'); }));
['dragleave','drop'].forEach(ev => drop.addEventListener(ev, e => { e.preventDefault(); drop.classList.remove('drag'); }));
drop.addEventListener('drop', e => { if (e.dataTransfer.files.length) addFiles(e.dataTransfer.files); });
fileInput.addEventListener('change', () => { if (fileInput.files.length) addFiles(fileInput.files); fileInput.value = ''; });

function addFiles(fileList) {
  status.className = 'status'; status.textContent = '';
  warn.classList.remove('show'); warn.textContent = '';
  progwrap.classList.remove('show');
  for (const f of fileList) {
    const ext = '.' + (f.name.split('.').pop() || '').toLowerCase();
    if (!INPUT_EXTS.includes(ext)) {
      status.className = 'status err'; status.textContent = t('notTxt') + ' ' + f.name;
      continue;
    }
    if (f.size > MAX_BYTES) {
      status.className = 'status err'; status.textContent = t('tooBig') + ' ' + f.name;
      continue;
    }
    if (isEbookName(f.name) && fileList.length > 1) {
      status.className = 'status err'; status.textContent = t('ebookOnly');
      continue;
    }
    if (isEbookName(f.name)) {
      files = [f]; // ebook input: single file mode
      continue;
    }
    if (!files.some(x => x.name === f.name && x.size === f.size)) files.push(f);
    if (files.reduce((s, x) => s + x.size, 0) > MAX_BYTES) {
      files.pop();
      status.className = 'status err'; status.textContent = t('totalBig') + ' ' + f.name;
    }
  }
  if (files.length > 1 && files.some(f => isEbookName(f.name))) {
    files = files.filter(f => !isEbookName(f.name));
    status.className = 'status err'; status.textContent = t('ebookOnly');
  }
  files.sort((a, b) => naturalCmp(a.name, b.name));
  renderList();
  syncTxtOnly();
  schedulePreview();
}

function naturalCmp(a, b) {
  // Must match the backend natural_sort_key: numbered files sort first,
  // then by EVERY number in the name (not just the first), then by name.
  const numsOf = s => (s.match(/\d+/g) || []).map(Number);
  const na = numsOf(a), nb = numsOf(b);
  const pa = na.length ? 0 : 1, pb = nb.length ? 0 : 1;
  if (pa !== pb) return pa - pb;
  if (na.length) {
    for (let i = 0; i < Math.max(na.length, nb.length); i++) {
      const x = na[i] === undefined ? -1 : na[i];
      const y = nb[i] === undefined ? -1 : nb[i];
      if (x !== y) return x - y;
    }
  }
  return a.localeCompare(b, 'zh');
}

function totalMB() { return (files.reduce((s, f) => s + f.size, 0) / 1024 / 1024).toFixed(2); }

function syncTxtOnly() {
  const nonTxt = files.length === 1 && isEbookName(files[0].name);
  [splitRow, document.getElementById('splitOpt'), document.getElementById('autoOpt'),
   document.getElementById('adsOpt')].forEach(el => el.classList.toggle('disabled', nonTxt));
  [splitCb, autoCb, adsCb].forEach(cb => { cb.disabled = nonTxt; });
}

function renderList() {
  if (files.length === 0) {
    flist.classList.remove('show'); fname.classList.remove('show');
    return;
  }
  if (files.length === 1) {
    flist.classList.remove('show');
    fname.classList.add('show');
    const f = files[0];
    const extra = isTxtName(f.name) ? '' : (' (' + f.name.split('.').pop().toUpperCase() + ')');
    fname.innerHTML = '✓ ' + esc(f.name) + extra + '  (' + (f.size / 1024 / 1024).toFixed(2) + ' MB) '
      + '<a href="#" data-rm="0" style="color:var(--err);margin-left:6px">✕</a>';
    fname.querySelector('[data-rm]').onclick = (e) => { e.preventDefault(); files = []; renderList(); syncTxtOnly(); schedulePreview(); };
    return;
  }
  fname.classList.remove('show');
  flist.classList.add('show');
  flistHint.textContent = t('flistHint');
  flistItems.innerHTML = '';
  files.forEach((f, i) => {
    const row = document.createElement('div');
    row.className = 'fitem';
    row.draggable = true;
    row.dataset.i = i;
    row.innerHTML =
      '<span class="grip">☰</span>' +
      '<span class="idx">' + (i + 1) + '</span>' +
      '<span class="nm">' + esc(f.name) + '</span>' +
      '<span class="sz">' + (f.size / 1024 / 1024).toFixed(2) + ' MB</span>' +
      '<span class="mv"><button data-up="' + i + '">▲</button><button data-down="' + i + '">▼</button></span>' +
      '<button class="rm" data-rm="' + i + '">✕</button>';
    row.addEventListener('dragstart', () => { dragIdx = i; row.classList.add('dragging'); });
    row.addEventListener('dragend', () => { dragIdx = null; row.classList.remove('dragging'); });
    row.addEventListener('dragover', e => e.preventDefault());
    row.addEventListener('drop', e => {
      e.preventDefault();
      if (dragIdx === null || dragIdx === i) return;
      const moved = files.splice(dragIdx, 1)[0];
      files.splice(i, 0, moved);
      renderList();
      schedulePreview();
    });
    flistItems.appendChild(row);
  });
  flistItems.querySelectorAll('[data-up]').forEach(b => b.onclick = () => move(+b.dataset.up, -1));
  flistItems.querySelectorAll('[data-down]').forEach(b => b.onclick = () => move(+b.dataset.down, 1));
  flistItems.querySelectorAll('[data-rm]').forEach(b => b.onclick = () => {
    files.splice(+b.dataset.rm, 1);
    renderList();
    syncTxtOnly();
    schedulePreview();
  });
  const total = files.reduce((s, f) => s + f.size, 0);
  flistHint.textContent = t('flistHint');
}
function move(i, dir) {
  const j = i + dir;
  if (j < 0 || j >= files.length) return;
  [files[i], files[j]] = [files[j], files[i]];
  renderList();
  schedulePreview();
}
function esc(s) { return s.replace(/[&<>"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c])); }
function fmtNum(n) { try { return n.toLocaleString(lang === 'zh' ? 'zh-CN' : 'en-US'); } catch (e) { return String(n); } }

// ================= preview =================
let previewTimer = null;
function schedulePreview() {
  clearTimeout(previewTimer);
  previewTimer = setTimeout(doPreview, 700);
}
titleInput.addEventListener('input', () => { titleEdited = true; });
authorInput.addEventListener('input', () => { authorEdited = true; });
pvRefresh.addEventListener('click', doPreview);

async function doPreview() {
  if (!files.length) { previewBox.classList.remove('show'); return; }
  const total = files.reduce((s, f) => s + f.size, 0);
  if (total > PREVIEW_MAX) {
    previewBox.classList.add('show');
    pvStats.innerHTML = '<div class="pvnote">' + t('pvBig') + '</div>';
    pvIntro.textContent = '';
    return;
  }
  previewBox.classList.add('show');
  pvStats.innerHTML = '<div class="pvnote">' + t('pvLoading') + '</div>';
  pvIntro.textContent = '';
  try {
    const r = await fetch('/preview', { method: 'POST', body: buildForm(files) });
    const j = await r.json();
    if (!j.ok) throw new Error(j.error || '');
    if (j.non_txt) {
      pvStats.innerHTML = '<div class="pvnote">' + t('pvNonTxt') + ' <b>' + esc(j.title) + ' (' + esc(j.ext) + ')</b></div>';
      pvIntro.textContent = '';
      return;
    }
    if (!titleEdited) titleInput.value = j.title || '';
    if (!authorEdited) authorInput.value = j.author || '';
    const bits = [
      t('pvChars') + ': <b>' + fmtNum(j.chars) + '</b>',
      t('pvChapters') + ': <b>' + fmtNum(j.chapters) + '</b>',
      t('pvParts') + ': <b>' + j.parts + '</b> ' + (lang === 'zh' ? '本' : 'vol'),
    ];
    if (j.ads_removed) bits.push('🧹 ' + t('pvAds') + ': <b>' + j.ads_removed + '</b>');
    if (j.auto_sections) bits.push('🔖 ' + t('pvAutoSec'));
    pvStats.innerHTML = bits.join('<span style="opacity:.3">|</span> ');
    pvIntro.textContent = j.intro ? (t('pvIntro') + ': ' + j.intro) : '';
  } catch (e) {
    pvStats.innerHTML = '<div class="pvnote">' + t('pvErr') + ': ' + esc(e.message) + '</div>';
  }
}

// ================= form builder =================
function buildForm(list) {
  const fmt = document.querySelector('input[name=format]:checked').value;
  const fd = new FormData();
  list.forEach(f => fd.append('file', f));
  fd.append('order', list.map((_, i) => i).join(','));
  fd.append('format', fmt);
  const nonTxt = list.some(f => isEbookName(f.name));
  if (!nonTxt) {
    if (!splitCb.checked) {
      fd.append('nosplit', '1');
    } else {
      fd.append('split_mb', Math.min(500, Math.max(1, parseInt(splitMb.value, 10) || 5)));
    }
    fd.append('auto_chapters', autoCb.checked ? '1' : '0');
    fd.append('clean_ads', adsCb.checked ? '1' : '0');
  } else {
    fd.append('nosplit', '1');
  }
  if (coverFile.files[0]) fd.append('cover', coverFile.files[0]);
  const t2 = titleInput.value.trim();
  if (t2) fd.append('title', t2);
  const a = authorInput.value.trim();
  if (a) fd.append('author', a);
  const re = chapterRe.value.trim();
  if (re) fd.append('chapter_re', re);
  const rt = retentionSel.value;
  if (rt) fd.append('retention_hours', rt);
  return fd;
}

// ================= batch folder import =================
folderBtn.addEventListener('click', () => folderInput.click());
folderInput.addEventListener('change', () => {
  if (!folderInput.files.length) return;
  for (const f of folderInput.files) {
    if (!isTxtName(f.name)) continue;
    if (batchFiles.some(x => x.name === f.name && x.size === f.size)) continue;
    if (batchFiles.length >= 300) break;
    batchFiles.push(f);
  }
  folderInput.value = '';
  renderBatch();
});
batchClear.addEventListener('click', () => { batchFiles = []; renderBatch(); });
function renderBatch() {
  if (!batchFiles.length) { batchBox.style.display = 'none'; return; }
  batchBox.style.display = 'block';
  const shown = batchFiles.slice(0, 12).map(f => esc(f.name)).join('<br>');
  const more = batchFiles.length > 12 ? '<br>… +' + (batchFiles.length - 12) : '';
  batchList.innerHTML = shown + more;
  batchGo.textContent = '⚡ ' + t('batchGo') + ' (' + batchFiles.length + ')';
}
batchGo.addEventListener('click', async () => {
  if (!batchFiles.length) return;
  batchGo.disabled = true;
  const list = batchFiles.slice();
  batchFiles = [];
  renderBatch();
  status.className = 'status';
  status.textContent = lang === 'zh' ? ('批量提交 ' + list.length + ' 本...') : ('Submitting ' + list.length + ' books...');
  for (const f of list) {
    try {
      const r = await fetch('/convert', { method: 'POST', body: buildForm([f]) });
      const j = await r.json();
      if (j.ok) trackJob(j.job_id);
      else { status.className = 'status err'; status.textContent = '❌ ' + esc(j.error || ''); }
    } catch (e) { /* ignore per-book failures */ }
  }
  batchGo.disabled = false;
  refreshHistory();
  status.className = 'status ok';
  status.textContent = lang === 'zh' ? ('✅ 已提交 ' + list.length + ' 本,详见转换记录') : ('✅ submitted ' + list.length + ' books');
});

// ================= progress helpers =================
function setProgress(pct, msg) {
  progwrap.classList.add('show');
  barfill.style.width = pct + '%';
  ppct.textContent = pct + '%';
  peta.textContent = '';
  if (msg) pmsg.textContent = msg;
}
function fmtDur(s) {
  s = Math.max(1, Math.round(s));
  if (s < 60) return s + (lang === 'zh' ? ' 秒' : 's');
  if (s < 3600) return Math.round(s / 60) + (lang === 'zh' ? ' 分钟' : 'min');
  return (s / 3600).toFixed(1) + 'h';
}
function beep() {
  try {
    const ctx = new (window.AudioContext || window.webkitAudioContext)();
    const o = ctx.createOscillator(), g = ctx.createGain();
    o.connect(g); g.connect(ctx.destination);
    o.frequency.value = 880; g.gain.value = 0.12;
    o.start(); o.stop(ctx.currentTime + 0.22);
  } catch (e) {}
}
function notifyDone(name) {
  if (!remindCb.checked) return;
  beep();
  if ('Notification' in window && Notification.permission === 'granted') {
    try { new Notification(t('done') + ' ✓', { body: name }); } catch (e) {}
  }
}

// ================= convert / merge =================
go.addEventListener('click', async () => {
  if (!files.length) { status.className = 'status err'; status.textContent = t('noFiles'); return; }
  if (remindCb.checked && 'Notification' in window && Notification.permission === 'default') {
    Notification.requestPermission();
  }
  const fd = buildForm(files);
  go.disabled = true;
  status.className = 'status'; status.textContent = '';
  warn.classList.remove('show'); warn.textContent = '';
  cancelBtn.classList.remove('show');
  setProgress(0, files.length > 1 ? (lang === 'zh' ? '上传 ' + files.length + ' 个文件中...' : 'Uploading ' + files.length + ' files...') : t('upload'));
  let job;
  try {
    const r = await fetch('/convert', { method: 'POST', body: fd });
    job = await r.json();
    if (!job.ok) throw new Error(job.error || '');
  } catch (e) {
    status.className = 'status err'; status.textContent = '❌ ' + e.message;
    go.disabled = false;
    return;
  }
  go.disabled = false;
  trackJob(job.job_id);
  refreshHistory();
});

mergeBtn.addEventListener('click', async () => {
  if (!files.length) { status.className = 'status err'; status.textContent = t('noFiles'); return; }
  // only true ebook inputs block merging; TXT and archives (which may hold
  // TXT files) are fine
  if (files.some(f => isEbookName(f.name))) {
    status.className = 'status err'; status.textContent = t('ebookOnly');
    return;
  }
  const fd = buildForm(files);
  go.disabled = true; mergeBtn.disabled = true;
  status.className = 'status'; status.textContent = '';
  warn.classList.remove('show'); warn.textContent = '';
  cancelBtn.classList.remove('show');
  setProgress(0, t('merging'));
  try {
    const r = await fetch('/merge', { method: 'POST', body: fd });
    const j = await r.json();
    if (!j.ok) throw new Error(j.error || '');
    setProgress(100, t('merged'));
    const mb = (j.size / 1024 / 1024).toFixed(2);
    status.className = 'status ok';
    status.innerHTML = '✅ ' + t('merged') + ' ' + j.merged_from + ' (' + mb + ' MB)<br>'
      + '<a class="dl" href="' + esc(j.download) + '">⬇ ' + t('download') + ' ' + esc(j.filename) + '</a>';
    refreshHistory();
  } catch (e) {
    status.className = 'status err'; status.textContent = '❌ ' + e.message;
  } finally {
    go.disabled = false; mergeBtn.disabled = false;
  }
});

// ================= web novel grabber =================
let grabJobId = null;

async function loadSources() {
  try {
    const r = await fetch('/sources');
    const j = await r.json();
    if (!j.ok) return;
    grabSource.innerHTML = '<option value="">' + esc(t('webSourceAuto')) + '</option>'
      + (j.sources || []).map(s => '<option value="' + esc(s.id) + '">' + esc(s.name) + '</option>').join('');
  } catch (e) {}
}

function renderGrabLog(log) {
  if (!log || !log.length) { grabLog.classList.remove('show'); return; }
  grabLog.classList.add('show');
  grabLog.textContent = log.join('\n');
  grabLog.scrollTop = grabLog.scrollHeight;
}

function setGrabProg(pct) {
  grabProg.classList.add('show');
  grabBar.style.width = pct + '%';
  grabPct.textContent = pct + '%';
}

async function reopenGrabBook(bookId) {
  rSetStatus(t('rLoading'), true);
  try {
    const r = await fetch('/read/reopen', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ book_id: bookId }),
    });
    const j = await r.json();
    if (!j.ok) throw new Error(j.error || '');
    startSession(j);
  } catch (err) {
    rSetStatus('❌ ' + err.message, false);
  }
}

async function convertGrabBook(bookId) {
  const fmt = document.querySelector('input[name=format]:checked').value;
  const body = { book_id: bookId, format: fmt };
  if (splitCb.checked) body.split_mb = splitMb.value; else body.nosplit = '1';
  body.auto_chapters = autoCb.checked ? '1' : '0';
  body.clean_ads = adsCb.checked ? '1' : '0';
  const re = chapterRe.value.trim();
  if (re) body.chapter_re = re;
  const rt = retentionSel.value;
  if (rt) body.retention_hours = rt;
  status.className = 'status'; status.textContent = '';
  warn.classList.remove('show'); warn.textContent = '';
  cancelBtn.classList.remove('show');
  setProgress(0, t('webConvert') + '...');
  try {
    const r = await fetch('/convert_lib', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    const j = await r.json();
    if (!j.ok) throw new Error(j.error || '');
    trackJob(j.job_id);
    refreshHistory();
  } catch (e) {
    status.className = 'status err';
    status.textContent = '❌ ' + e.message;
  }
}

function handleGrabUpdate(j) {
  setProgress(j.progress || 0, j.message || '');
  setGrabProg(j.progress || 0);
  if (j.log && j.log.length) renderGrabLog(j.log);
  if (j.status === 'done') {
    grabBtn.disabled = false;
    grabCancelBtn.style.display = 'none';
    cancelBtn.classList.remove('show');
    const mb = (j.size / 1048576).toFixed(2);
    const links = [];
    if (j.book_id) links.push('<a class="dl" href="#" data-act="read" data-bid="' + esc(j.book_id) + '">📖 ' + esc(t('webRead')) + '</a>');
    links.push('<a class="dl" href="' + esc(j.download) + '">⬇ ' + esc(t('download')) + ' TXT</a>');
    if (j.book_id) links.push('<a class="dl" href="#" data-act="conv" data-bid="' + esc(j.book_id) + '">⚡ ' + esc(t('webConvert')) + '</a>');
    const html = '✅ ' + esc(j.message || t('webDone')) + ' (' + mb + ' MB)<br>' + links.join(' &nbsp; ');
    grabStatus.className = 'grabstatus ok';
    grabStatus.innerHTML = html;
    status.className = 'status ok';
    status.innerHTML = html;
  } else if (j.status === 'error' || j.status === 'cancelled') {
    grabBtn.disabled = false;
    grabCancelBtn.style.display = 'none';
    cancelBtn.classList.remove('show');
    grabStatus.className = 'grabstatus err';
    grabStatus.textContent = (j.status === 'cancelled' ? '⏹ ' : '❌ ') + (j.message || t('failed'));
  }
}

async function startGrab() {
  const url = grabUrl.value.trim();
  if (!url) {
    grabStatus.className = 'grabstatus err';
    grabStatus.textContent = '❌ ' + t('webNoUrl');
    return;
  }
  grabStatus.className = 'grabstatus'; grabStatus.textContent = '';
  grabLog.classList.remove('show'); grabLog.textContent = '';
  grabBtn.disabled = true;
  grabCancelBtn.style.display = 'inline-block';
  const body = { url: url, mode: grabMode.value,
                 source_id: grabSource.value,
                 clean_ads: grabAds.checked ? '1' : '0',
                 render: grabRender.checked ? 'on' : 'auto' };
  setProgress(0, t('webStart'));
  try {
    const r = await fetch('/grab', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    const j = await r.json();
    if (!j.ok) throw new Error(j.error || '');
    grabJobId = j.job_id;
    trackJob(j.job_id);
  } catch (e) {
    grabBtn.disabled = false;
    grabCancelBtn.style.display = 'none';
    grabStatus.className = 'grabstatus err';
    grabStatus.textContent = '❌ ' + e.message;
  }
}

grabBtn.addEventListener('click', () => startGrab());

// delegate clicks on 阅读/转电子书 links (they appear in the reader panel
// AND the main status area, so a single delegated handler covers both)
document.addEventListener('click', (e) => {
  const el = e.target.closest('[data-act]');
  if (!el) return;
  e.preventDefault();
  const bid = el.dataset.bid;
  if (!bid) return;
  if (el.dataset.act === 'read') reopenGrabBook(bid);
  else if (el.dataset.act === 'conv') convertGrabBook(bid);
});

grabCancelBtn.addEventListener('click', async () => {
  if (!grabJobId) return;
  grabCancelBtn.disabled = true;
  try {
    await fetch('/cancel', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ job_id: grabJobId }),
    });
  } catch (e) {}
});
grabUrl.addEventListener('keydown', (e) => { if (e.key === 'Enter') startGrab(); });

// ================= source manager =================
const srcMgrBtn = document.getElementById('srcMgrBtn');
const srcPanel = document.getElementById('srcPanel');
const srcList = document.getElementById('srcList');
const srcName = document.getElementById('srcName');
const srcHome = document.getElementById('srcHome');
const srcEnc = document.getElementById('srcEnc');
const srcContTag = document.getElementById('srcContTag');
const srcContAttr = document.getElementById('srcContAttr');
const srcContVal = document.getElementById('srcContVal');
const srcTocRe = document.getElementById('srcTocRe');
const srcJson = document.getElementById('srcJson');
const srcSave = document.getElementById('srcSave');
const srcMsg = document.getElementById('srcMsg');

const SRC_TEMPLATE = JSON.stringify({
  id: 'my_site', name: '我的站 (UTF-8)', home: 'https://www.example.com',
  url_re: 'example\\.com', encoding: 'utf-8',
  toc: { link_re: '第\\s*[0-9零一二三四五六七八九十百千两万]+\\s*[章节回卷]', dedupe: 'keep_last' },
  chapter: { title: [{ tag: 'h1' }], content: [{ tag: 'div', id: 'content' }], pagination: true },
}, null, 2);

srcMgrBtn.addEventListener('click', () => { srcPanel.style.display = srcPanel.style.display === 'none' ? 'block' : 'none'; });

const srcProbeUrl = document.getElementById('srcProbeUrl');
const srcProbeBtn = document.getElementById('srcProbeBtn');
const srcProbeMsg = document.getElementById('srcProbeMsg');

srcProbeBtn.addEventListener('click', async () => {
  const url = srcProbeUrl.value.trim();
  if (!url) return;
  srcProbeMsg.className = 'srcmsg';
  srcProbeMsg.textContent = t('srcProbing') + '...';
  try {
    const r = await fetch('/sources/probe', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ url: url }),
    });
    const j = await r.json();
    if (!j.ok) throw new Error(j.error || '');
    const d = j.detected || {};
    const bits = [t('srcProbeDet') + ': ' + (d.encoding || 'auto') + ' · '
      + (d.chapter_pages > 1 ? (d.chapter_pages + ' 页分页') : '单页') + ' · '
      + (d.cjk_chars || 0) + ' 字'];
    if (d.container) bits.push((d.container.tag || '') + '.' + (d.container.id || d.container.class || ''));
    srcProbeMsg.className = 'srcmsg ok';
    srcProbeMsg.textContent = '✅ ' + t('srcProbeOk') + ' (' + bits.join(' · ') + ')';
    const s = j.suggest || {};
    srcName.value = s.name || '';
    srcHome.value = s.home || '';
    srcEnc.value = s.encoding || 'utf-8';
    const sel = ((s.chapter || {}).content || [])[0] || {};
    srcContTag.value = sel.tag || 'div';
    srcContAttr.value = sel.id ? 'id' : 'class';
    srcContVal.value = sel.id || sel.class || '';
    srcTocRe.value = ((s.toc || {}).link_re || '').replace(/\\/g, '\\');
    srcJson.value = JSON.stringify(s, null, 2);
  } catch (e) {
    srcProbeMsg.className = 'srcmsg err';
    srcProbeMsg.textContent = '❌ ' + t('srcProbeFail') + ': ' + e.message;
  }
});
srcProbeUrl.placeholder = t('srcProbePh');

async function renderSrcList() {
  try {
    const r = await fetch('/sources');
    const j = await r.json();
    const list = j.sources || [];
    if (!list.length) {
      srcList.innerHTML = '<div class="srcitem">' + esc(t('srcEmpty')) + '</div>';
    } else {
      srcList.innerHTML = list.map(s => {
        const builtin = ['biquge5200_cc', 'b5200_net', '88biquge', 'sudugu'].includes(s.id);
        return '<div class="srcitem"><span class="snm">' + esc(s.name) + '</span>'
          + '<span class="smeta">' + esc(s.encoding || 'auto') + '</span>'
          + '<button class="sdel" data-id="' + esc(s.id) + '"' + (builtin ? ' style="display:none"' : '') + '>' + esc(t('srcDel')) + '</button></div>';
      }).join('');
    }
    srcList.querySelectorAll('.sdel').forEach(b => {
      b.addEventListener('click', async () => {
        if (!confirm(lang === 'zh' ? ('删除书源 ' + b.dataset.id + ' ?') : ('Delete source ' + b.dataset.id + ' ?'))) return;
        try {
          const r = await fetch('/sources/delete', {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ id: b.dataset.id }),
          });
          const j = await r.json();
          if (!j.ok) throw new Error(j.error || '');
        } catch (e) { srcMsg.className = 'srcmsg err'; srcMsg.textContent = '❌ ' + e.message; }
        await Promise.all([renderSrcList(), loadSources()]);
      });
    });
  } catch (e) {}
}

srcSave.addEventListener('click', async () => {
  srcMsg.className = 'srcmsg'; srcMsg.textContent = '';
  const name = srcName.value.trim();
  const home = srcHome.value.trim();
  const enc = srcEnc.value;
  const tag = srcContTag.value.trim() || 'div';
  const attr = srcContAttr.value;
  const val = srcContVal.value.trim();
  const tocRe = srcTocRe.value.trim();
  if (!name || !home) {
    srcMsg.className = 'srcmsg err'; srcMsg.textContent = '❌ ' + t('srcNeedName');
    return;
  }
  if (!srcJson.value.trim() && !srcContVal.value.trim()) {
    srcMsg.className = 'srcmsg err';
    srcMsg.textContent = '❌ ' + t('srcNeedCont');
    return;
  }
  let src = null;
  const adv = srcJson.value.trim();
  if (adv) {
    try { src = JSON.parse(adv); } catch (e) {
      srcMsg.className = 'srcmsg err'; srcMsg.textContent = '❌ JSON: ' + e.message;
      return;
    }
  }
  if (!src) {
    const id = name.replace(/[^A-Za-z0-9_-]/g, '_').toLowerCase() || 'mysite';
    src = { id: id, name: name, home: home, encoding: enc || 'utf-8' };
    src.toc = { link_re: tocRe || '第\\s*[0-9零一二三四五六七八九十百千两万]+\\s*[章节回卷]', dedupe: 'keep_last' };
    if (val) {
      const sel = { tag: tag };
      sel[attr] = val;
      src.chapter = { title: [{ tag: 'h1' }], content: [sel], pagination: true };
    }
  }
  try {
    const r = await fetch('/sources', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ source: src }),
    });
    const j = await r.json();
    if (!j.ok) throw new Error(j.error || '');
    srcMsg.className = 'srcmsg ok';
    srcMsg.textContent = '✅ ' + t('srcSaved');
    srcJson.value = '';
    await Promise.all([renderSrcList(), loadSources()]);
  } catch (e) {
    srcMsg.className = 'srcmsg err'; srcMsg.textContent = '❌ ' + e.message;
  }
});

srcJson.addEventListener('focus', () => {
  if (!srcJson.value.trim()) srcJson.value = SRC_TEMPLATE;
});


// ================= multi-job progress =================
let activeJobs = new Set();
let lastJobId = null;
let histTick = 0;
function trackJob(jobId) {
  activeJobs.add(jobId);
  lastJobId = jobId;
  cancelBtn.disabled = false;
  cancelBtn.textContent = t('cancel');
  cancelBtn.classList.add('show');
  if (!pollTimer) pollTimer = setInterval(pollAll, 1000);
}
async function pollAll() {
  if (!activeJobs.size) { clearInterval(pollTimer); pollTimer = null; return; }
  for (const jid of [...activeJobs]) {
    let j;
    try { j = await (await fetch('/progress/' + jid)).json(); }
    catch (e) { continue; }
    if (j.error === 'job not found') {
      activeJobs.delete(jid);
      if (jid === lastJobId) {
        cancelBtn.classList.remove('show');
        status.className = 'status err';
        status.textContent = t('jobLost');
      }
      continue;
    }
    if (jid === lastJobId) updateMain(j);
    if (j.status === 'done' || j.status === 'error' || j.status === 'cancelled') {
      activeJobs.delete(jid);
    }
  }
  if (++histTick % 3 === 0 || !activeJobs.size) refreshHistory();
}
function updateMain(j) {
  if (j.kind === 'grab') { handleGrabUpdate(j); return; }
  setProgress(j.progress || 0, j.message || '');
  if (j.status === 'running' && j.progress > 0 && j.progress < 95 && j.created) {
    const elapsed = (Date.now() - j.created * 1000) / 1000;
    if (elapsed > 5) peta.textContent = 'ETA ' + fmtDur(elapsed / j.progress * (100 - j.progress));
  } else if (j.progress >= 95) {
    peta.textContent = t('pack');
  }
  if (j.warning && warn.textContent !== j.warning) {
    warn.textContent = '⚠️ ' + j.warning;
    warn.classList.add('show');
  }
  if (j.status === 'done') {
    cancelBtn.classList.remove('show');
    const mb = (j.size / 1024 / 1024).toFixed(2);
    const label = j.is_zip
      ? ('⬇ ZIP(' + j.parts_count + ')')
      : ('⬇ ' + esc(j.filename));
    status.className = 'status ok';
    status.innerHTML = '✅ ' + esc(j.message || t('done')) + ' (' + mb + ' MB)<br>'
      + '<a class="dl" href="' + esc(j.download) + '">' + label + '</a>';
    notifyDone(j.title || j.filename || '');
  } else if (j.status === 'error') {
    cancelBtn.classList.remove('show');
    status.className = 'status err';
    status.textContent = '❌ ' + (j.message || t('failed')) + (j.detail ? '\n\n' + j.detail : '');
  } else if (j.status === 'cancelled') {
    cancelBtn.classList.remove('show');
    status.className = 'status err';
    status.textContent = '⏹ ' + t('cancelled');
  }
}
cancelBtn.addEventListener('click', async () => {
  if (!lastJobId) return;
  cancelBtn.disabled = true;
  cancelBtn.textContent = '…';
  try {
    await fetch('/cancel', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ job_id: lastJobId }),
    });
  } catch (e) {}
});

// ================= history =================
async function refreshHistory() {
  try {
    const r = await fetch('/jobs');
    const d = await r.json();
    renderHistory(d.jobs || []);
  } catch (e) {}
}
function renderHistory(list) {
  if (!list.length) { hist.classList.remove('show'); histList.innerHTML = ''; return; }
  hist.classList.add('show');
  histCount.textContent = list.length + (lang === 'zh' ? ' 条' : '');
  histList.innerHTML = list.map(j => {
    const st = j.status;
    const icon = st === 'done' ? '✅' : st === 'error' ? '❌'
      : st === 'cancelled' ? '⏹️' : st === 'running' ? '⏳'
      : st === 'queued' ? '🕓' : '·';
    const nm = esc(j.title || j.filename || (j.job_id || '').slice(0, 8));
    const fmt = j.format ? ' · ' + j.format.toUpperCase() : (j.kind === 'merge' ? ' · TXT' : '');
    const size = j.size ? ' · ' + (j.size / 1048576).toFixed(1) + 'MB' : '';
    let right = '';
    if (st === 'done' && j.download) {
      right = j.downloadable
        ? ' · <a href="' + esc(j.download) + '">⬇ ' + t('download') + '</a>'
        : ' · <span class="expired">' + t('expired') + '</span>';
    } else if (st === 'running' || st === 'queued') {
      right = ' · <button class="hcancel" data-job="' + esc(j.job_id) + '">✕</button>';
    }
    const msg = j.message ? ' <span class="hmsg">' + esc(j.message) + '</span>' : '';
    const bar = (st === 'running' || st === 'queued')
      ? '<span class="hprog"><i style="width:' + (j.progress || 0) + '%"></i></span>' : '';
    return '<div class="hitem ' + st + '">' + icon + ' ' + nm + fmt + size + msg + right + bar + '</div>';
  }).join('');
}
histList.addEventListener('click', async (ev) => {
  const btn = ev.target.closest('.hcancel');
  if (!btn) return;
  btn.disabled = true;
  try {
    await fetch('/cancel', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ job_id: btn.dataset.job }),
    });
  } catch (e) {}
});

// ================= history clear =================
const clearMenu = document.getElementById('clearMenu');
const histClearBtn = document.getElementById('histClearBtn');
const clearRecords = document.getElementById('clearRecords');
const clearAll = document.getElementById('clearAll');
const clearCancel = document.getElementById('clearCancel');
histClearBtn.addEventListener('click', () => {
  clearMenu.style.display = clearMenu.style.display === 'flex' ? 'none' : 'flex';
});
clearCancel.addEventListener('click', () => { clearMenu.style.display = 'none'; });
async function doClear(mode) {
  clearMenu.style.display = 'none';
  try {
    const r = await fetch('/clear_history', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ mode: mode }),
    });
    const d = await r.json();
    if (!d.ok) throw new Error(d.error || '');
    status.className = 'status ok';
    status.textContent = '✅ ' + (lang === 'zh' ? '已清理 ' : 'cleared ') + d.cleared
      + (d.freed_mb ? (lang === 'zh' ? ' · 释放 ' : ' · freed ') + d.freed_mb + ' MB' : '');
    refreshHistory();
  } catch (e) {
    status.className = 'status err';
    status.textContent = '❌ ' + e.message;
  }
}
clearRecords.addEventListener('click', () => doClear('records'));
clearAll.addEventListener('click', () => {
  if (!confirm(lang === 'zh' ? '确定删除所有已完成任务的输出文件吗?此操作不可恢复。' : 'Delete all output files? This cannot be undone.')) return;
  doClear('all');
});

// ================= init =================
applyLang();
let savedTheme = 'default';
try { savedTheme = localStorage.getItem('txt2ebook_theme') || 'default'; } catch (e) {}
if (savedTheme !== 'custom') setTheme(savedTheme);
loadBgList().then(() => {
  if (savedTheme === 'custom' && bgSelected) setTheme('custom');
  else if (savedTheme === 'custom') setTheme('default');
});
refreshHistory();
loadSources();
renderSrcList();

// ================= reader =================
const rPrev = document.getElementById('rPrev');
const rNext = document.getElementById('rNext');
const rFontMinus = document.getElementById('rFontMinus');
const rFontPlus = document.getElementById('rFontPlus');
const rMode = document.getElementById('rMode');
const rClose = document.getElementById('rClose');
const rBack = document.getElementById('rBack');
const rLocal = document.getElementById('rLocal');
const rTheme = document.getElementById('rTheme');
const state = { sid: null, n: 0, pure: false, font: 18, bookId: null, bookmarks: [] };
const rBookmark = document.getElementById('rBookmark');
const rBookmarks = document.getElementById('rBookmarks');
const rbmlist = document.getElementById('rbmlist');
const rRecentList = document.getElementById('rRecentList');
const rClearRecent = document.getElementById('rClearRecent');
try { state.font = Math.min(30, Math.max(13, parseInt(localStorage.getItem('txt2ebook_font') || '18', 10) || 18)); } catch (e) {}
let pureTimer = null;

function rSetStatus(msg, ok) {
  rStatus.className = 'rstatus' + (ok ? ' ok' : ' err');
  rStatus.textContent = msg;
}

function openReader() {
  state.sid = null; state.n = 0; state.pure = false;
  state.bookId = null; state.bookmarks = [];
  rBookmark.classList.remove('on');
  rbmlist.style.display = 'none';
  renderRecent();
  // cover the left toolbar: match its width so the right side (conversion
  // panel / progress) stays visible; full screen on narrow screens
  if (window.innerWidth > 760) {
    const w = Math.min(Math.max(colLeft.offsetWidth || 440, 300),
                       window.innerWidth - 60);
    reader.style.width = w + 'px';
  } else {
    reader.style.width = '';
  }
  reader.classList.add('show');
  ropen.style.display = 'flex';
  rContent.style.display = 'none';
  ropen.style.display = 'flex';
  rtoolbar.style.display = 'flex';
  rContent.innerHTML = '';
  rStatus.textContent = '';
  rMode.textContent = t('rPure');
  rChapter.innerHTML = '';
  rPrev.disabled = true;
  rNext.disabled = true;
  document.body.classList.remove('pure');
}

function closeReader() {
  if (document.fullscreenElement) document.exitFullscreen().catch(() => {});
  reader.classList.remove('show');
  document.body.classList.remove('pure');
  state.sid = null;
  clearTimeout(pureTimer);
  rContent.style.display = 'none';
}

function startSession(j) {
  state.sid = j.sid;
  state.bookId = j.book_id || null;
  state.bookmarks = j.bookmarks ? j.bookmarks.map(b => b.chapter) : [];
  ropen.style.display = 'none';
  rContent.style.display = 'block';
  rTitle.textContent = j.title;
  rChapter.innerHTML = '';
  j.chapter_titles.forEach((ct, i) => {
    const o = document.createElement('option');
    o.value = i;
    o.textContent = ct;
    rChapter.appendChild(o);
  });
  rContent.innerHTML = '<div class="rloading">' + t('rLoading') + '</div>';
  const resume = j.resume_chapter || 0;
  loadChapter(resume);
  if (resume > 0 && j.chapter_titles[resume]) {
    rStatus.textContent = '↩ ' + t('rResume') + ': ' + j.chapter_titles[resume];
    rStatus.className = 'rstatus ok';
    setTimeout(() => { if (!rStatus.textContent.startsWith('↩')) return; rStatus.textContent = ''; }, 4000);
  }
}

function renderText(title, text) {
  const blocks = text.split(/\n{2,}/);
  const head = '<div class="rhead">' + esc(title) + '</div>';
  const body = blocks.map(b => '<p>' + esc(b).replace(/\n/g, '<br>') + '</p>').join('');
  rContent.innerHTML = head + body;
  rContent.scrollTop = 0;
}

async function loadChapter(n) {
  if (!state.sid) return;
  state.n = n;
  rContent.innerHTML = '<div class="rloading">' + t('rLoading') + '</div>';
  try {
    const r = await fetch('/read/chapter?s=' + state.sid + '&n=' + n);
    const j = await r.json();
    if (!j.ok) throw new Error(j.error || '');
    rChapter.value = String(n);
    rPrev.disabled = n <= 0;
    rNext.disabled = n >= j.total - 1;
    rBookmark.classList.toggle('on', state.bookmarks.includes(n));
    renderText(j.chapter_title, j.text);
  } catch (e) {
    rContent.innerHTML = '<div class="rloading">❌ ' + esc(e.message || t('rExpired')) + '</div>';
  }
}


function setPure(on) {
  state.pure = on;
  document.body.classList.toggle('pure', on);
  rMode.textContent = on ? t('rNormal') : t('rPure');
  clearTimeout(pureTimer);
  if (on) {
    if (reader.requestFullscreen) reader.requestFullscreen().catch(() => {});
    rtoolbar.style.display = 'flex';
    pureTimer = setTimeout(() => { if (state.pure) rtoolbar.style.display = 'none'; }, 2500);
  } else {
    if (document.fullscreenElement) document.exitFullscreen().catch(() => {});
    rtoolbar.style.display = 'flex';
  }
}

reader.addEventListener('mousemove', () => {
  if (!state.pure) return;
  rtoolbar.style.display = 'flex';
  clearTimeout(pureTimer);
  pureTimer = setTimeout(() => { if (state.pure) rtoolbar.style.display = 'none'; }, 2500);
});
document.addEventListener('fullscreenchange', () => {
  if (!document.fullscreenElement && state.pure && reader.classList.contains('show')) {
    state.pure = false;
    document.body.classList.remove('pure');
    rMode.textContent = t('rPure');
    rtoolbar.style.display = 'flex';
  }
});

document.addEventListener('keydown', (e) => {
  if (!reader.classList.contains('show')) return;
  const typing = e.target && (e.target.tagName === 'INPUT' || e.target.tagName === 'SELECT');
  if (e.key === 'Escape') { closeReader(); return; }
  if (typing) return;
  if (e.key === 'ArrowRight') { if (!rNext.disabled) navNext(); }
  else if (e.key === 'ArrowLeft') { if (!rPrev.disabled) navPrev(); }
});
function navPrev() {
  if (!rPrev.disabled) loadChapter(state.n - 1);
}
function navNext() {
  if (!rNext.disabled) loadChapter(state.n + 1);
}

readerBtn.addEventListener('click', openReader);
rClose.addEventListener('click', closeReader);
rBack.addEventListener('click', closeReader);
rTheme.addEventListener('click', (e) => {
  e.stopPropagation();
  themesBox.classList.toggle('open');
});
rChapter.addEventListener('change', () => {
  loadChapter(parseInt(rChapter.value, 10) || 0);
});
rPrev.addEventListener('click', navPrev);
rNext.addEventListener('click', navNext);
rFontMinus.addEventListener('click', () => { state.font = Math.max(13, state.font - 1); applyFont(); });
rFontPlus.addEventListener('click', () => { state.font = Math.min(30, state.font + 1); applyFont(); });
function applyFont() {
  rContent.style.fontSize = state.font + 'px';
  try { localStorage.setItem('txt2ebook_font', String(state.font)); } catch (e) {}
}
rMode.addEventListener('click', () => setPure(!state.pure));

rBookmark.addEventListener('click', async () => {
  if (!state.sid || !state.bookId) return;
  const has = state.bookmarks.includes(state.n);
  try {
    await fetch('/read/' + (has ? 'bookmark_remove' : 'bookmark'), {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ book_id: state.bookId, chapter: state.n }),
    });
    if (has) state.bookmarks = state.bookmarks.filter(c => c !== state.n);
    else state.bookmarks.push(state.n);
    rBookmark.classList.toggle('on', !has);
    renderRecent();
  } catch (e) {}
});

rBookmarks.addEventListener('click', () => {
  if (rbmlist.style.display === 'block') { rbmlist.style.display = 'none'; return; }
  if (!state.bookmarks.length) {
    rbmlist.innerHTML = '<div class="rbmitem">' + t('rNoBookmarks') + '</div>';
  } else {
    rbmlist.innerHTML = state.bookmarks.map(c => {
      const title = (rChapter.options[c] && rChapter.options[c].textContent) || (t('rChN') + (c + 1) + t('rChUnit'));
      return '<div class="rbmitem" data-c="' + c + '">🔖 ' + esc(title) + '</div>';
    }).join('');
  }
  rbmlist.style.display = 'block';
});
rbmlist.addEventListener('click', (e) => {
  const item = e.target.closest('.rbmitem');
  if (!item || !item.dataset.c) return;
  rbmlist.style.display = 'none';
  loadChapter(parseInt(item.dataset.c, 10) || 0);
});

async function renderRecent() {
  try {
    const r = await fetch('/read/recent');
    const j = await r.json();
    const list = (j.books || []);
    if (!list.length) {
      rRecentList.innerHTML = '<div class="rbook bmeta">' + t('rEmptyRecent') + '</div>';
      return;
    }
    rRecentList.innerHTML = list.map(b => {
      const ch = b.chapter > 0 ? ' · ' + t('rChN') + b.chapter + t('rChUnit') : '';
      const bm = b.bookmarks.length ? ' · 🔖' + b.bookmarks.length : '';
      return '<div class="rbook"><span class="bt" data-bid="' + b.book_id + '">📖 ' + esc(b.title) + '</span>'
        + '<span class="bmeta">' + ch + bm + '</span>'
        + '<button class="bx" data-del="' + b.book_id + '">' + t('rDel') + '</button></div>';
    }).join('');
  } catch (e) {}
}
rRecentList.addEventListener('click', async (e) => {
  const open = e.target.closest('.bt');
  const del = e.target.closest('.bx');
  if (del) {
    if (!confirm(lang === 'zh' ? '删除这本书的本地阅读记录?' : "Delete this book's reading record?")) return;
    try {
      await fetch('/read/delete', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ book_id: del.dataset.del }) });
    } catch (err) {}
    renderRecent();
    return;
  }
  if (!open) return;
  rSetStatus(t('rLoading'), true);
  try {
    const r = await fetch('/read/reopen', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ book_id: open.dataset.bid }) });
    const j = await r.json();
    if (!j.ok) throw new Error(j.error || '');
    startSession(j);
  } catch (err) {
    rSetStatus('❌ ' + err.message, false);
  }
});
rClearRecent.addEventListener('click', async () => {
  if (!confirm(lang === 'zh' ? '清空全部阅读记录?本地书库中的书也会一并删除。' : 'Clear all reading history? Books in the local library will also be deleted.')) return;
  try {
    await fetch('/read/clear_recent', { method: 'POST' });
  } catch (e) {}
  renderRecent();
  if (state.sid) closeReader();
});

rLocal.addEventListener('click', () => rFile.click());
rFile.addEventListener('change', async () => {
  const f = rFile.files[0];
  rFile.value = '';
  if (!f) return;
  rSetStatus(t('rLoading'), true);
  const fd = new FormData();
  fd.append('file', f);
  try {
    const r = await fetch('/read/open', { method: 'POST', body: fd });
    const j = await r.json();
    if (!j.ok) throw new Error(j.error || '');
    state.web = false;
    startSession(j);
  } catch (e) {
    rSetStatus('❌ ' + e.message, false);
  }
});
applyFont();
</script>
</body>
</html>
"""

def _is_job_dir_name(name):
    """True when a directory name looks like a job dir (uuid4().hex = 32 hex
    chars). Guards the output sweep against deleting unrelated user folders
    when the custom output directory is a shared/existing directory."""
    return bool(re.fullmatch(r"[0-9a-f]{32}", name))


def prune_stale_outputs():
    """Startup sweep: remove job output dirs left over from previous sessions
    (crashed/interrupted runs, TTL-expired jobs). Only touches dirs whose mtime
    is older than the per-job TTL (from HISTORY; default JOB_TTL), so anything
    recent or in progress survives. Only job dirs (32-hex names) are considered
    - unrelated user folders in a custom output directory are never touched.
    """
    now = time.time()
    for base in output_dirs():
        for d in base.iterdir():
            if not d.is_dir() or not _is_job_dir_name(d.name):
                continue
            # Only real job dirs are touched: ours carry a marker file, older
            # ones are tracked in HISTORY. A user folder that merely happens
            # to be named like a job id is never deleted.
            if not (d / ".txt2ebook_job").is_file() and d.name not in HISTORY:
                continue
            try:
                age = now - d.stat().st_mtime
            except OSError:
                continue
            # Respect the per-job retention the user chose (falls back to the
            # default TTL for jobs from before the ttl was persisted).
            ttl = HISTORY.get(d.name, {}).get("ttl") or JOB_TTL
            if age > ttl:
                shutil.rmtree(d, ignore_errors=True)
                print(f"[prune] removed stale job dir {d.name} "
                      f"({age / 3600:.1f} h old)")


def main():
    load_config()
    HISTORY.update(_load_history())
    prune_stale_outputs()
    if EBOOK_CONVERT is None:
        print("WARNING: ebook-convert (Calibre) not found.")
    else:
        print(f"Using ebook-convert: {EBOOK_CONVERT}")

    class Txt2EbookServer(ThreadingHTTPServer):
        # Windows SO_REUSEADDR allows a second instance to silently co-bind the
        # same port, which load-balances requests across old and new code. Fail
        # loudly instead so a duplicate server can't cause confusing behavior.
        allow_reuse_address = False

    host = effective_host()
    srv = Txt2EbookServer((host, PORT), Handler)
    print(f"txt2ebook running at http://{HOST}:{PORT}")
    if host != HOST:
        ip = lan_ip()
        print(f"局域网访问已开启: 手机/平板连接同一 WiFi 后打开")
        print(f"  http://{ip or '<本机IP>'}:{PORT}")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping...")
        srv.shutdown()


if __name__ == "__main__":
    main()
