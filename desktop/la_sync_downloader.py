#!/usr/bin/env python3
"""LA Sync Downloader for Windows.

Runs on your own PC (residential IP), logs into Let's Approach, and downloads a
whole course (videos at up to 720p + PDFs) to D:\\LA-Downloads by default.
Nothing is uploaded anywhere. Already-downloaded files are skipped.
"""

import base64
import json
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import urljoin

import requests
from Crypto.Cipher import AES
from Crypto.Util.Padding import unpad

import tkinter as tk
from tkinter import ttk, filedialog, messagebox, scrolledtext

API = "https://letsapproachapi.akamai.net.in"
SITE = "https://letsapproach.akamai.net.in"
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
AES_KEY = b"638udh3829162018"
AES_IV = b"fedcba9876543210"
QUALITIES = ("1080", "720")
SEG_WORKERS = 12
DL_HEADERS = {
    "Referer": "https://appx-play.akamai.net.in/",
    "Origin": "https://appx-play.akamai.net.in",
    "User-Agent": UA,
}

APP_NAME = "LA-Sync-Downloader"
BASE_DIR = Path(sys.executable).parent if getattr(sys, "frozen", False) else Path(__file__).parent
DATA_DIR = Path(os.environ.get("APPDATA", str(Path.home()))) / APP_NAME
DATA_DIR.mkdir(parents=True, exist_ok=True)
CONF_PATH = DATA_DIR / "config.json"
CACHE_PATH = DATA_DIR / "details-cache.json"


def load_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def save_json(path: Path, data) -> None:
    try:
        path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass


def sanitize(name: str, limit: int = 90) -> str:
    """Make a single path component valid on Windows (colons, dots, length)."""
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", str(name or "Untitled"))
    name = re.sub(r"\s+", " ", name).strip().strip(".").strip()
    return (name[:limit] or "Untitled")


def winpath(path) -> str:
    """Long-path-safe absolute path for Windows file operations."""
    s = os.path.abspath(str(path))
    if os.name == "nt" and not s.startswith("\\\\?\\") and len(s) > 230:
        s = "\\\\?\\" + s
    return s


def file_ready(path: Path) -> bool:
    try:
        return os.path.exists(winpath(path)) and os.path.getsize(winpath(path)) > 1024
    except OSError:
        return False


def ffmpeg_bin() -> str:
    local = BASE_DIR / "ffmpeg.exe"
    if local.exists():
        return str(local)
    return "ffmpeg"


def decrypt_url(value) -> str | None:
    if not value or not isinstance(value, str):
        return None
    if value.startswith("http"):
        return value
    try:
        b64 = value.split(":")[0]
        raw = base64.b64decode(b64 + "==")
        dec = AES.new(AES_KEY, AES.MODE_CBC, AES_IV).decrypt(raw)
        try:
            dec = unpad(dec, 16)
        except Exception:
            dec = dec.rstrip(b"\x00")
        url = dec.decode("utf-8", errors="ignore").strip()
        return url if url.startswith("http") else None
    except Exception:
        return None


def decrypt_video_key(value) -> bytes | None:
    if not value or not isinstance(value, str):
        return None
    try:
        b64 = value.split(":")[0]
        raw = base64.b64decode(b64 + "==")
        if len(raw) < 16:
            return None
        return AES.new(AES_KEY, AES.MODE_CBC, AES_IV).decrypt(raw[:16])
    except Exception:
        return None


def item_id(it: dict):
    for key in ("id", "folder_id", "video_id", "file_id", "item_id"):
        if it.get(key):
            return str(it[key])
    return None


def item_title(it: dict) -> str:
    for key in (
        "Title", "title", "name", "folder_name", "video_name", "file_name", "contents",
        "content_name", "topic", "chapter_name", "video_title", "class_name", "label",
        "heading", "lecture_title", "batch_name", "section_name",
    ):
        value = it.get(key)
        if value and isinstance(value, str) and value.strip():
            return value.strip()
    return f"item_{item_id(it) or 'unknown'}"


def is_folder(it: dict) -> bool:
    if not isinstance(it, dict):
        return False
    if str(it.get("material_type", "")).upper() == "FOLDER":
        return True
    return bool(
        it.get("is_folder") in (True, 1, "1", "true")
        or str(it.get("content_type", "")).lower() == "folder"
        or (it.get("folder_id") and not it.get("file_link") and not it.get("pdf_link"))
    )


def classify(it: dict) -> str:
    material = str(it.get("material_type", "")).upper()
    if material in ("PDF", "FILE", "DOCUMENT", "IMAGE", "STUDY_MATERIAL"):
        return "pdf"
    if material in ("VIDEO", "RECORDED", "LIVE", "LECTURE", "CLASS", "RECORDED_VIDEO", "LIVE_CLASS", "HLS"):
        return "video"
    if it.get("pdf_link") or it.get("download_link"):
        return "pdf"
    if it.get("download_links") or it.get("encrypted_links") or it.get("file_link"):
        return "video"
    return "other"


def parse_contents(payload) -> dict:
    inner = payload.get("data", payload) if isinstance(payload, dict) else payload
    folders, items = [], []
    if isinstance(inner, list):
        for entry in inner:
            (folders if is_folder(entry) else items).append(entry)
    elif isinstance(inner, dict):
        for entry in inner.get("folders", []) or []:
            (folders if is_folder(entry) else items).append(entry)
        for key in ("videos", "files", "study_material", "pdfs"):
            items.extend(inner.get(key, []) or [])
    return {"folders": folders, "items": items}


def norm_q(value) -> str:
    return str(value).rstrip("pP").strip()


def video_candidates(det: dict) -> list[dict]:
    cands, seen = [], set()
    dl = det.get("download_links") or []
    enc = det.get("encrypted_links") or []

    def add(url, quality, vtype, aes_key=None):
        if url and url not in seen:
            seen.add(url)
            cands.append({"url": url, "quality": quality, "type": vtype, "key": aes_key})

    present = {
        norm_q(link.get("quality", link.get("bitrate", "")))
        for link in (dl + enc)
        if link.get("path")
    }
    targets = [q for q in QUALITIES if q in present]
    if not targets and present:
        numeric = [q for q in present if q.isdigit()]
        if numeric:
            targets = [max(numeric, key=int)]
    for wanted in targets:
        for link in dl:
            if norm_q(link.get("quality", link.get("bitrate", ""))) == wanted and link.get("path"):
                url = decrypt_url(link["path"])
                if url:
                    vtype = "hls" if (".m3u8" in url or "hls" in url or "master" in url) else "direct"
                    add(url, wanted + "p", vtype)
        for link in enc:
            if norm_q(link.get("quality", "")) == wanted and link.get("path"):
                url = decrypt_url(link["path"])
                if url:
                    add(url, wanted + "p", "encrypted_mkv", decrypt_video_key(link.get("key", "")))
    if not cands and det.get("file_link"):
        url = decrypt_url(det["file_link"])
        if url:
            add(url, "auto", "hls" if (".m3u8" in url or "hls" in url) else "direct")
    return cands


def pdf_url(det: dict) -> str | None:
    for field in ("pdf_link", "file_link", "download_link", "pdf_link2", "study_material_link"):
        value = det.get(field)
        if value:
            url = value if str(value).startswith("http") else decrypt_url(value)
            if url:
                return url
    return None


class LaApi:
    def __init__(self, email: str = "", password: str = "", token: str = "", uid: str = ""):
        self.email = email
        self.password = password
        self.token = token
        self.uid = str(uid or "")
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": UA})
        self.min_gap = 3.0
        self._pace_lock = threading.Lock()
        self._last = 0.0
        self.details_cache = load_json(CACHE_PATH, {})
        self._cache_lock = threading.Lock()

    def _pace(self):
        with self._pace_lock:
            gap = self.min_gap - (time.monotonic() - self._last)
            if gap > 0:
                time.sleep(gap)
            self._last = time.monotonic()

    def _headers(self) -> dict:
        return {
            "auth-key": "appxapi", "client-service": "Appx", "source": "website",
            "device-type": "", "user-id": self.uid, "authorization": self.token,
            "accept": "application/json, text/plain, */*",
            "accept-language": "en-US,en;q=0.9",
            "origin": SITE, "referer": SITE + "/",
        }

    def login(self) -> dict:
        self._pace()
        resp = self.session.post(
            f"{API}/post/userLogin?extra_details=1",
            headers={
                "auth-key": "appxapi", "client-service": "Appx", "source": "website",
                "accept": "application/json, text/plain, */*",
                "origin": SITE, "referer": SITE + "/",
            },
            data={"source": "website", "email": self.email, "password": self.password, "extra_details": "1"},
            timeout=60,
        )
        try:
            payload = resp.json()
        except Exception:
            raise RuntimeError(f"Login failed (HTTP {resp.status_code}) — {resp.text[:120]}")
        if str(payload.get("status")) != "200":
            raise RuntimeError(payload.get("message") or "Login failed")
        data = payload.get("data") or {}
        self.token = data.get("token") or ""
        self.uid = str(data.get("userid") or "")
        if not self.token:
            raise RuntimeError("Login succeeded but no token returned")
        return data

    def get(self, path: str, retries: int = 5) -> dict:
        last = "unknown"
        for attempt in range(retries):
            self._pace()
            try:
                resp = self.session.get(API + path, headers=self._headers(), timeout=90)
            except requests.RequestException as exc:
                last = str(exc)
                time.sleep(min(10 * (attempt + 1), 40))
                continue
            if resp.status_code == 429:
                wait = min(30 * (attempt + 1), 180)
                ra = resp.headers.get("retry-after")
                if ra and ra.isdigit():
                    wait = min(max(wait, int(ra)), 240)
                time.sleep(wait)
                last = "rate limited (429)"
                continue
            if resp.status_code == 401 and self.email and self.password:
                try:
                    self.login()
                    continue
                except Exception as exc:
                    raise RuntimeError(f"Session expired and relogin failed: {exc}")
            if resp.status_code != 200:
                raise RuntimeError(f"HTTP {resp.status_code}: {resp.text[:160]}")
            try:
                return resp.json()
            except Exception:
                raise RuntimeError(f"Invalid JSON: {resp.text[:160]}")
        raise RuntimeError(f"Request failed after retries: {last}")

    def details(self, course_id: str, video_id: str) -> dict:
        with self._cache_lock:
            cached = self.details_cache.get(video_id)
        if cached:
            return cached
        data = self.get(
            f"/get/fetchVideoDetailsById?course_id={course_id}&video_id={video_id}"
            "&ytflag=0&folder_wise_course=1&lc_app_api_url="
        )
        det = data.get("data", data) if isinstance(data, dict) else data
        if not isinstance(det, dict):
            raise RuntimeError("Unexpected details response")
        with self._cache_lock:
            self.details_cache[video_id] = det
            save_json(CACHE_PATH, self.details_cache)
        return det

    def course_root(self, course_id: str) -> dict:
        root = parse_contents(self.get(
            f"/get/folder_contentsv3?course_id={course_id}&parent_id=-1&windowsapp=false&start=0"
        ))
        if len(root["folders"]) == 1 and not root["items"]:
            container = root["folders"][0]
            cid = item_id(container)
            title = item_title(container)
            if cid and (cid == course_id or title.startswith("item_") or not title):
                inner = parse_contents(self.get(
                    f"/get/folder_contentsv3?course_id={course_id}&parent_id={cid}&windowsapp=false&start=0"
                ))
                if inner["folders"] or inner["items"]:
                    return inner
        return root

    def folder(self, course_id: str, parent_id: str) -> dict:
        folders, items = [], []
        start = 0
        while True:
            page = parse_contents(self.get(
                f"/get/folder_contentsv3?course_id={course_id}&parent_id={parent_id}"
                f"&windowsapp=false&start={start}"
            ))
            folders.extend(page["folders"])
            items.extend(page["items"])
            got = len(page["folders"]) + len(page["items"])
            if got < 10:
                break
            start += got
        return {"folders": folders, "items": items}

    def purchases(self) -> list[dict]:
        data = self.get(f"/get/get_all_purchasesv2?userid={self.uid}&item_type=10")
        rows = data.get("data") or []
        courses = []
        for row in rows:
            det = (row.get("coursedt") or [{}])[0]
            cid = str(row.get("itemid") or "")
            if cid:
                courses.append({
                    "id": cid,
                    "name": det.get("course_name") or f"Course {cid}",
                    "thumb": det.get("course_thumbnail") or "",
                })
        return courses


class Cancelled(Exception):
    pass


class Downloader(threading.Thread):
    def __init__(self, api: LaApi, course_id: str, course_name: str, base_dir: Path,
                 out_queue: queue.Queue, seg_workers: int = 48, parallel_videos: int = 2,
                 skip_history: bool = True, min_free_gb: float = 4.0):
        super().__init__(daemon=True)
        self.api = api
        self.course_id = course_id
        self.course_name = course_name
        self.base_dir = base_dir
        self.q = out_queue
        self.seg_workers = max(4, min(int(seg_workers), 96))
        self.parallel_videos = max(1, min(int(parallel_videos), 4))
        self.stop_event = threading.Event()
        self.lock = threading.Lock()
        self.skip_history = skip_history
        self.min_free_bytes = int(min_free_gb * (1 << 30))
        self.history_path = base_dir / ".la-sync" / f"{course_id}.json"
        self.history_backup = DATA_DIR / f"completed-{course_id}.json"
        self.completed: dict = {}
        self._load_history()
        self.counts = {"done": 0, "failed": 0, "skipped": 0}
        self.executor = None
        self._bytes = 0
        self._speed_lock = threading.Lock()
        self._speed_t0 = time.monotonic()
        self._speed_bytes0 = 0
        self._active = 0

    # ── logging / counters ─────────────────────────────────────────────────
    def log(self, msg: str, level: str = "info"):
        self.q.put(("log", f"[{time.strftime('%H:%M:%S')}] {msg}", level))

    def status(self, msg: str):
        self.q.put(("status", msg, ""))

    def add_bytes(self, n: int):
        with self._speed_lock:
            self._bytes += n
        now = time.monotonic()
        if now - self._speed_t0 >= 1.0:
            with self._speed_lock:
                rate = (self._bytes - self._speed_bytes0) / max(0.2, now - self._speed_t0)
                self._speed_t0 = now
                self._speed_bytes0 = self._bytes
            self.q.put(("speed", rate, ""))

    def bump(self, field: str, n: int = 1):
        with self.lock:
            self.counts[field] += n
            snapshot = (self.counts["done"], self.counts["failed"], self.counts["skipped"])
        self.q.put(("counts", snapshot, ""))

    # ── history (survives deleted files) ───────────────────────────────────
    def _load_history(self):
        merged = {}
        for path in (self.history_backup, self.history_path):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                items = data.get("items", data)
                if isinstance(items, dict):
                    merged.update(items)
            except Exception:
                continue
        self.completed = merged
        if merged:
            self.log(f"History: {len(merged)} item(s) already downloaded in previous sessions")

    def _save_history(self):
        payload = json.dumps({"items": self.completed, "updated": int(time.time())}, ensure_ascii=False)
        for path in (self.history_backup, self.history_path):
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(payload, encoding="utf-8")
            except Exception:
                pass

    def _mark_done(self, la_id: str, title: str, size: int, path: str):
        if not la_id:
            return
        with self.lock:
            self.completed[la_id] = {"title": title, "size": size, "path": path, "at": int(time.time())}
        self._save_history()

    def wait_for_space(self):
        anchor = str(Path(self.base_dir).anchor or self.base_dir)
        while True:
            if self.stop_event.is_set():
                raise Cancelled()
            try:
                free = shutil.disk_usage(anchor).free
            except Exception:
                return
            self.q.put(("disk_free", free, ""))
            if free >= self.min_free_bytes:
                return
            self.q.put(("log", f"[{time.strftime('%H:%M:%S')}] PAUSED — only {free / (1 << 30):.1f} GB free on {anchor}. Upload/delete some files; it will continue automatically.", "warn"))
            self.status(f"Paused — low disk space ({free / (1 << 30):.1f} GB free)")
            time.sleep(10)

    # ── main ────────────────────────────────────────────────────────────────
    def run(self):
        try:
            self.log(f"Course: {self.course_name}  →  {self.base_dir}")
            self.log(f"Turbo: {self.seg_workers} connections per video · {self.parallel_videos} videos at once")
            os.makedirs(winpath(self.base_dir / sanitize(self.course_name, 80)), exist_ok=True)
            self.executor = ThreadPoolExecutor(max_workers=self.parallel_videos)
            self.walk("-1", "course root", "", 0, is_root=True)
            self.executor.shutdown(wait=True)
            self.q.put(("finished", (self.counts["done"], self.counts["failed"], self.counts["skipped"]), ""))
        except Cancelled:
            if self.executor:
                self.executor.shutdown(wait=True, cancel_futures=True)
            self.log("Download stopped by user", "warn")
            self.q.put(("stopped", "", ""))
        except Exception as exc:
            if self.executor:
                self.executor.shutdown(wait=True, cancel_futures=True)
            self.log(f"Fatal: {exc}", "error")
            self.log(traceback.format_exc(), "error")
            self.q.put(("error", str(exc), ""))

    def walk(self, folder_id: str, name: str, parent_path: str, depth: int, is_root: bool = False):
        if self.stop_event.is_set():
            raise Cancelled()
        if depth > 10:
            return
        safe_name = sanitize(name, 70)
        path = "" if is_root else ((parent_path + "/" + safe_name).strip("/") if parent_path else safe_name)
        try:
            contents = self.api.course_root(self.course_id) if is_root else self.api.folder(self.course_id, folder_id)
        except Exception as exc:
            self.log(f"Could not list \"{name}\": {exc}", "error")
            self.bump("failed")
            return
        self.log(f"Folder: {name or 'root'} — {len(contents['items'])} file(s), {len(contents['folders'])} sub-folder(s)")
        for raw in contents["items"]:
            if self.stop_event.is_set():
                raise Cancelled()
            self.executor.submit(self.safe_item, raw, path)
        for sub in contents["folders"]:
            if self.stop_event.is_set():
                raise Cancelled()
            sid = item_id(sub)
            if sid:
                self.walk(sid, item_title(sub), path, depth + 1)

    def safe_item(self, raw: dict, folder_path: str):
        if self.stop_event.is_set():
            return
        try:
            self.download_item(raw, folder_path)
        except Cancelled:
            return
        except Exception as exc:
            self.bump("failed")
            self.log(f"FAILED \"{item_title(raw)[:55]}\": {exc}", "error")

    # ── items ───────────────────────────────────────────────────────────────
    def target_dir(self, folder_path: str) -> Path:
        out = self.base_dir / sanitize(self.course_name, 80)
        for part in str(folder_path or "").split("/"):
            if part.strip():
                out = out / sanitize(part, 70)
        os.makedirs(winpath(out), exist_ok=True)
        return out

    def download_item(self, raw: dict, folder_path: str):
        title = sanitize(item_title(raw))
        kind = classify(raw)
        target_dir = self.target_dir(folder_path)
        self.status(f"{title}")
        la_id = item_id(raw) or ""
        if kind == "pdf":
            dest = target_dir / f"{title}.pdf"
            if file_ready(dest):
                self._mark_done(la_id, title, os.path.getsize(winpath(dest)), str(dest))
                self.bump("skipped")
                self.log(f"skip (exists) {title[:60]}")
                return
            if self.skip_history and la_id and la_id in self.completed:
                self.bump("skipped")
                self.log(f"skip (history — already downloaded before) {title[:60]}")
                return
            self.wait_for_space()
            url = pdf_url(raw)
            if not url:
                try:
                    url = pdf_url(self.api.details(self.course_id, item_id(raw) or ""))
                except Exception:
                    url = None
            if not url:
                self.log(f"skip (no PDF link) {title[:60]}", "warn")
                self.bump("skipped")
                return
            self.download_file(url, dest)
            self._mark_done(la_id, title, os.path.getsize(winpath(dest)), str(dest))
            self.bump("done")
            self.log(f"PDF saved: {dest.name}")
            return
        if kind != "video":
            self.bump("skipped")
            self.log(f"skip (unsupported) {title[:60]}", "warn")
            return

        dest = target_dir / f"{title}.mp4"
        if file_ready(dest):
            self._mark_done(la_id, title, os.path.getsize(winpath(dest)), str(dest))
            self.bump("skipped")
            self.log(f"skip (exists) {title[:60]}")
            return
        if self.skip_history and la_id and la_id in self.completed:
            self.bump("skipped")
            self.log(f"skip (history — already downloaded before) {title[:60]}")
            return
        self.wait_for_space()

        # Fresh details first — the listing links are frequently stale (404).
        detail = None
        try:
            detail = self.api.details(self.course_id, item_id(raw) or "")
        except Exception as exc:
            self.log(f"details unavailable ({exc}) — trying listing links", "warn")
        candidates = video_candidates(detail or {})
        if not candidates:
            candidates = video_candidates(raw)
        if not candidates:
            raise RuntimeError("no 720p source found")

        last_error = None
        tried = set()
        refreshed = detail is not None
        while True:
            pending = [c for c in candidates if c["url"] not in tried]
            if not pending and not refreshed:
                refreshed = True
                try:
                    candidates = video_candidates(self.api.details(self.course_id, item_id(raw) or ""))
                except Exception:
                    candidates = []
                continue
            if not pending:
                break
            for cand in pending:
                if self.stop_event.is_set():
                    raise Cancelled()
                tried.add(cand["url"])
                try:
                    self.log(f"Downloading {title[:55]} ({cand['quality']})")
                    if cand["type"] == "hls":
                        self.hls(cand["url"], dest)
                    elif cand["type"] == "encrypted_mkv":
                        self.encrypted(cand, dest)
                    else:
                        self.download_file(cand["url"], dest)
                    if not os.path.exists(winpath(dest)) or os.path.getsize(winpath(dest)) < 1024:
                        raise RuntimeError("empty output")
                    size = os.path.getsize(winpath(dest))
                    self._mark_done(la_id, title, size, str(dest))
                    self.bump("done")
                    self.log(f"Saved: {dest.name} ({size / 1e6:.1f} MB)", "success")
                    return
                except Cancelled:
                    raise
                except Exception as exc:
                    last_error = exc
                    self.log(f"Link {cand['quality']} failed: {exc}", "warn")
                    continue
        raise RuntimeError(str(last_error) if last_error else "no working link")

    # ── media primitives ───────────────────────────────────────────────────
    def download_file(self, url: str, dest: Path):
        tmp = dest.with_suffix(dest.suffix + ".part")
        with requests.get(url, headers=DL_HEADERS, stream=True, timeout=180) as resp:
            resp.raise_for_status()
            total = int(resp.headers.get("content-length") or 0)
            received = 0
            with open(winpath(tmp), "wb") as fh:
                for chunk in resp.iter_content(chunk_size=1 << 20):
                    if self.stop_event.is_set():
                        raise Cancelled()
                    if not chunk:
                        continue
                    fh.write(chunk)
                    received += len(chunk)
                    self.add_bytes(len(chunk))
                    if total:
                        pct = min(99, int(received * 100 / total))
                        self.q.put(("progress", pct, f"{received / 1e6:.1f}/{total / 1e6:.1f} MB"))
        os.replace(winpath(tmp), winpath(dest))

    def ffmpeg(self, args, timeout=7200):
        proc = subprocess.Popen(
            [ffmpeg_bin(), "-y", "-loglevel", "error"] + args,
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        while True:
            try:
                rc = proc.wait(timeout=15)
                break
            except subprocess.TimeoutExpired:
                if self.stop_event.is_set():
                    proc.kill()
                    raise Cancelled()
        if rc != 0:
            err = (proc.stderr.read() or b"")[-400:].decode("utf-8", "ignore")
            raise RuntimeError(f"ffmpeg failed: {err}")

    def ffmpeg_hls_stream(self, url: str, dest: Path):
        headers = (
            f"Referer: {DL_HEADERS['Referer']}\r\n"
            f"Origin: {DL_HEADERS['Origin']}\r\n"
            f"User-Agent: {UA}\r\n"
        )
        self.ffmpeg([
            "-headers", headers, "-i", url,
            "-c", "copy", "-bsf:a", "aac_adtstoasc", winpath(dest),
        ])

    def hls(self, url: str, dest: Path):
        try:
            self.hls_parallel(url, dest)
        except Cancelled:
            raise
        except Exception as exc:
            text = str(exc)
            if "404" in text:
                raise RuntimeError("source link is 404 (stale or deleted)")
            self.log(f"Parallel HLS failed ({exc}) — using ffmpeg", "warn")
            self.ffmpeg_hls_stream(url, dest)

    def hls_parallel(self, url: str, dest: Path):
        sess = requests.Session()
        sess.mount("https://", requests.adapters.HTTPAdapter(
            pool_connections=self.seg_workers + 8, pool_maxsize=self.seg_workers + 8, max_retries=0))
        sess.mount("http://", requests.adapters.HTTPAdapter(
            pool_connections=self.seg_workers + 8, pool_maxsize=self.seg_workers + 8, max_retries=0))
        sess.headers.update({"User-Agent": UA})

        def text(u):
            r = sess.get(u, headers=DL_HEADERS, timeout=45)
            r.raise_for_status()
            return r.text

        playlist = text(url)
        if "#EXT-X-STREAM-INF" in playlist:
            lines = playlist.splitlines()
            best, bw_best = None, -1
            for i, line in enumerate(lines):
                if not line.startswith("#EXT-X-STREAM-INF"):
                    continue
                m = re.search(r"BANDWIDTH=(\d+)", line)
                bw = int(m.group(1)) if m else 0
                nxt = next((x.strip() for x in lines[i + 1:] if x.strip() and not x.startswith("#")), None)
                if nxt and bw > bw_best:
                    bw_best = bw
                    best = urljoin(url, nxt)
            if best:
                url = best
                playlist = text(url)
        if "#EXT-X-KEY" in playlist:
            raise RuntimeError("encrypted HLS")
        if "#EXT-X-ENDLIST" not in playlist:
            raise RuntimeError("live playlist")
        segs = [urljoin(url, l.strip()) for l in playlist.splitlines() if l.strip() and not l.startswith("#")]
        if not segs:
            raise RuntimeError("empty playlist")

        parts = dest.parent / (sanitize(dest.stem, 40) + "_segments")
        shutil.rmtree(winpath(parts), ignore_errors=True)
        os.makedirs(winpath(parts), exist_ok=True)
        done = [0]
        total = len(segs)

        def fetch(pair):
            idx, seg = pair
            target = parts / f"seg_{idx:06d}.ts"
            target_str = winpath(target)
            for attempt in range(3):
                try:
                    with sess.get(seg, headers=DL_HEADERS, stream=True, timeout=120) as r:
                        r.raise_for_status()
                        with open(target_str, "wb") as fh:
                            for chunk in r.iter_content(1 << 16):
                                if self.stop_event.is_set():
                                    raise Cancelled()
                                if not chunk:
                                    continue
                                fh.write(chunk)
                                self.add_bytes(len(chunk))
                    done[0] += 1
                    self.q.put(("progress", int(done[0] * 100 / total), f"{done[0]}/{total} segments"))
                    return
                except Cancelled:
                    raise
                except Exception:
                    if attempt == 2:
                        raise
                    time.sleep(0.8 * (attempt + 1))

        with ThreadPoolExecutor(max_workers=self.seg_workers) as pool:
            list(pool.map(fetch, enumerate(segs)))

        local = parts / "index.m3u8"
        out_lines, idx = ["#EXTM3U", "#EXT-X-VERSION:3"], 0
        for line in playlist.splitlines():
            s = line.strip()
            if not s or s.startswith("#EXT-X-ENDLIST"):
                continue
            if s.startswith("#"):
                out_lines.append(s)
            else:
                out_lines.append(f"seg_{idx:06d}.ts")
                idx += 1
        out_lines.append("#EXT-X-ENDLIST")
        with open(winpath(local), "w", encoding="utf-8") as fh:
            fh.write("\n".join(out_lines) + "\n")
        self.ffmpeg(["-allowed_extensions", "ALL", "-i", winpath(local), "-c", "copy",
                     "-bsf:a", "aac_adtstoasc", "-threads", "0", winpath(dest)])
        shutil.rmtree(winpath(parts), ignore_errors=True)

    def encrypted(self, cand: dict, dest: Path):
        raw = dest.parent / (sanitize(dest.stem, 60) + ".enc.mkv")
        self.download_file(cand["url"], raw)
        key = cand.get("key")
        if not key or len(key) != 16:
            raise RuntimeError("missing AES key")
        dec = dest.parent / (sanitize(dest.stem, 60) + ".dec.mkv")
        for mode, iv in ((AES.MODE_ECB, None), (AES.MODE_CBC, b"\x00" * 16)):
            tmp = dest.parent / (sanitize(dest.stem, 60) + ".try.mkv")
            try:
                cipher = AES.new(key, mode) if iv is None else AES.new(key, mode, iv)
                with open(winpath(raw), "rb") as fin, open(winpath(tmp), "wb") as fout:
                    while True:
                        chunk = fin.read(1 << 20)
                        if not chunk:
                            break
                        pad = len(chunk) % 16
                        data = chunk + (b"\x00" * (16 - pad) if pad else b"")
                        out = cipher.decrypt(data)
                        if iv is not None and pad == 0:
                            cipher = AES.new(key, AES.MODE_CBC, out[-16:])
                        fout.write(out[: len(chunk)])
                with open(winpath(tmp), "rb") as fh:
                    magic = fh.read(4)
                if magic == b"\x1a\x45\xdf\xa3":
                    shutil.move(winpath(tmp), winpath(dec))
                    break
            finally:
                if os.path.exists(winpath(tmp)):
                    os.unlink(winpath(tmp))
        if not os.path.exists(winpath(dec)):
            os.unlink(winpath(raw)) if os.path.exists(winpath(raw)) else None
            raise RuntimeError("decrypt failed")
        self.ffmpeg(["-i", winpath(dec), "-c", "copy", winpath(dest)])
        for f in (raw, dec):
            try:
                os.unlink(winpath(f))
            except OSError:
                pass


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("LA Sync Downloader")
        self.geometry("860x620")
        self.configure(bg="#0b0f16")
        self.conf = load_json(CONF_PATH, {})
        self.api: LaApi | None = None
        self.downloader: Downloader | None = None
        self.q = queue.Queue()
        self.courses: list[dict] = []
        self._build_ui()
        self.after(150, self.poll)
        if self.conf.get("email"):
            self.email_var.set(self.conf["email"])
        if self.conf.get("save_dir"):
            self.dir_var.set(self.conf["save_dir"])
        if self.conf.get("token"):
            self.log("Saved session found — verifying…")
            self.api = LaApi(self.conf.get("email", ""), "", self.conf["token"], self.conf.get("uid", ""))
            threading.Thread(target=self._verify_saved, daemon=True).start()

    def _build_ui(self):
        style = ttk.Style(self)
        try:
            style.theme_use("clam")
        except Exception:
            pass
        style.configure("TButton", padding=6)
        style.configure("Accent.TButton", padding=7, font=("Segoe UI", 10, "bold"))
        style.configure("TLabel", background="#0b0f16", foreground="#dbe6f5")
        style.configure("TTFrame", background="#0b0f16")

        pad = {"padx": 10, "pady": 6}
        top = tk.Frame(self, bg="#0b0f16")
        top.pack(fill="x", **pad)
        tk.Label(top, text="LA Sync Downloader", bg="#0b0f16", fg="#7fb0ff",
                 font=("Segoe UI", 16, "bold")).pack(side="left")
        tk.Label(top, text="  720p · saves to D:\\ · no uploads", bg="#0b0f16", fg="#64748b",
                 font=("Segoe UI", 9)).pack(side="left")

        form = tk.Frame(self, bg="#0b0f16")
        form.pack(fill="x", **pad)
        tk.Label(form, text="Email", bg="#0b0f16", fg="#93a1b3").grid(row=0, column=0, sticky="w")
        self.email_var = tk.StringVar()
        tk.Entry(form, textvariable=self.email_var, width=32).grid(row=0, column=1, padx=(6, 14))
        tk.Label(form, text="Password", bg="#0b0f16", fg="#93a1b3").grid(row=0, column=2, sticky="w")
        self.pass_var = tk.StringVar()
        tk.Entry(form, textvariable=self.pass_var, width=24, show="•").grid(row=0, column=3, padx=(6, 14))
        self.login_btn = ttk.Button(form, text="Login", command=self.login)
        self.login_btn.grid(row=0, column=4, padx=4)
        self.login_status = tk.Label(form, text="not logged in", bg="#0b0f16", fg="#fbbf24")
        self.login_status.grid(row=0, column=5, padx=10, sticky="w")

        tok = tk.Frame(self, bg="#0b0f16")
        tok.pack(fill="x", **pad)
        tk.Label(tok, text="Advanced (optional): token", bg="#0b0f16", fg="#64748b", font=("Segoe UI", 8)).pack(side="left")
        self.token_var = tk.StringVar()
        tk.Entry(tok, textvariable=self.token_var, width=58).pack(side="left", padx=6)
        self.uid_var = tk.StringVar()
        tk.Entry(tok, textvariable=self.uid_var, width=10).pack(side="left", padx=6)
        ttk.Button(tok, text="Use token", command=self.use_token).pack(side="left", padx=4)

        mid = tk.Frame(self, bg="#0b0f16")
        mid.pack(fill="x", **pad)
        tk.Label(mid, text="Course", bg="#0b0f16", fg="#93a1b3").pack(side="left")
        self.course_var = tk.StringVar()
        self.course_box = ttk.Combobox(mid, textvariable=self.course_var, width=52, state="readonly")
        self.course_box.pack(side="left", padx=8)
        ttk.Button(mid, text="Refresh courses", command=self.refresh_courses).pack(side="left", padx=4)

        out = tk.Frame(self, bg="#0b0f16")
        out.pack(fill="x", **pad)
        tk.Label(out, text="Save to", bg="#0b0f16", fg="#93a1b3").pack(side="left")
        self.dir_var = tk.StringVar(value="D:\\LA-Downloads")
        tk.Entry(out, textvariable=self.dir_var, width=58).pack(side="left", padx=8)
        ttk.Button(out, text="Browse…", command=self.choose_dir).pack(side="left")

        turbo = tk.Frame(self, bg="#0b0f16")
        turbo.pack(fill="x", **pad)
        tk.Label(turbo, text="Connections / video", bg="#0b0f16", fg="#93a1b3").pack(side="left")
        self.seg_var = tk.StringVar(value=str(self.conf.get("seg_workers", 48)))
        ttk.Combobox(turbo, textvariable=self.seg_var, values=["24", "32", "48", "64", "96"],
                     width=5, state="readonly").pack(side="left", padx=6)
        tk.Label(turbo, text="   Videos at once", bg="#0b0f16", fg="#93a1b3").pack(side="left")
        self.par_var = tk.StringVar(value=str(self.conf.get("parallel_videos", 2)))
        ttk.Combobox(turbo, textvariable=self.par_var, values=["1", "2", "3", "4"],
                     width=4, state="readonly").pack(side="left", padx=6)
        tk.Label(turbo, text="   (higher = faster on fast lines)", bg="#0b0f16", fg="#64748b",
                 font=("Segoe UI", 8)).pack(side="left")

        hist = tk.Frame(self, bg="#0b0f16")
        hist.pack(fill="x", **pad)
        self.resume_var = tk.BooleanVar(value=bool(self.conf.get("resume_history", True)))
        tk.Checkbutton(hist, text="Skip already-downloaded items even if files were moved/deleted",
                       variable=self.resume_var, bg="#0b0f16", fg="#dbe6f5",
                       selectcolor="#1a2332", activebackground="#0b0f16",
                       activeforeground="#dbe6f5").pack(side="left")
        self.disk_label = tk.Label(hist, text="", bg="#0b0f16", fg="#93a1b3")
        self.disk_label.pack(side="right")
        ttk.Button(hist, text="Reset progress", command=self.reset_progress).pack(side="right", padx=8)

        actions = tk.Frame(self, bg="#0b0f16")
        actions.pack(fill="x", **pad)
        self.start_btn = ttk.Button(actions, text="⬇  Download whole course", style="Accent.TButton",
                                    command=self.start_download)
        self.start_btn.pack(side="left")
        self.stop_btn = ttk.Button(actions, text="Stop", command=self.stop_download, state="disabled")
        self.stop_btn.pack(side="left", padx=8)
        self.progress = ttk.Progressbar(actions, length=320, mode="determinate")
        self.progress.pack(side="left", padx=12)
        self.speed_label = tk.Label(actions, text="", bg="#0b0f16", fg="#22d3ee", font=("Consolas", 9))
        self.speed_label.pack(side="left", padx=6)

        self.status_label = tk.Label(self, text="Ready.", bg="#0b0f16", fg="#93a1b3",
                                     anchor="w", font=("Segoe UI", 9))
        self.status_label.pack(fill="x", padx=12)

        self.logbox = scrolledtext.ScrolledText(self, height=18, bg="#05070a", fg="#9fb3c8",
                                                insertbackground="#9fb3c8", font=("Consolas", 9),
                                                relief="flat")
        self.logbox.pack(fill="both", expand=True, padx=10, pady=8)
        self.logbox.configure(state="disabled")

        self.counts_label = tk.Label(self, text="", bg="#0b0f16", fg="#64748b", anchor="w")
        self.counts_label.pack(fill="x", padx=12, pady=(0, 8))

    # ── helpers ─────────────────────────────────────────────────────────────
    def log(self, msg: str, level: str = "info"):
        self.logbox.configure(state="normal")
        self.logbox.insert("end", msg + "\n")
        self.logbox.see("end")
        self.logbox.configure(state="disabled")

    def choose_dir(self):
        folder = filedialog.askdirectory(initialdir=self.dir_var.get() or "D:\\")
        if folder:
            self.dir_var.set(folder)

    def _verify_saved(self):
        try:
            courses = self.api.purchases()
            self.q.put(("courses", courses, ""))
            self.q.put(("login_ok", self.api.uid, ""))
        except Exception as exc:
            self.q.put(("log", f"Saved session invalid: {exc}", "warn"))

    def use_token(self):
        token = self.token_var.get().strip()
        uid = self.uid_var.get().strip()
        if not token:
            messagebox.showwarning("Token", "Paste the Authorization token first.")
            return
        self.api = LaApi(self.conf.get("email", ""), "", token, uid)
        try:
            courses = self.api.purchases()
            self._set_courses(courses)
            self.login_status.configure(text=f"token user {uid}", fg="#34d399")
            self.conf.update({"token": token, "uid": uid})
            save_json(CONF_PATH, self.conf)
            self.log("Token accepted.")
        except Exception as exc:
            messagebox.showerror("Token", f"Token rejected: {exc}")

    def login(self):
        email = self.email_var.get().strip()
        password = self.pass_var.get()
        if not email or not password:
            messagebox.showwarning("Login", "Enter email and password.")
            return
        self.login_btn.configure(state="disabled")
        self.login_status.configure(text="logging in…", fg="#fbbf24")

        def work():
            try:
                api = LaApi(email, password)
                api.login()
                courses = api.purchases()
                self.q.put(("api_ready", api, ""))
                self.q.put(("login_ok", api.uid, ""))
                self.q.put(("courses", courses, ""))
                self.q.put(("login_save", (email, password, api.token, api.uid), ""))
            except Exception as exc:
                self.q.put(("login_err", str(exc), ""))
            finally:
                self.q.put(("login_enable", "", ""))

        threading.Thread(target=work, daemon=True).start()

    def refresh_courses(self):
        if not self.api:
            messagebox.showwarning("Courses", "Login first.")
            return

        def work():
            try:
                self.q.put(("courses", self.api.purchases(), ""))
            except Exception as exc:
                self.q.put(("log", f"Could not load courses: {exc}", "error"))

        threading.Thread(target=work, daemon=True).start()

    def _set_courses(self, courses: list[dict]):
        if courses == getattr(self, "courses", None) and courses:
            return
        self.courses = courses
        names = [f"{c['name']}  (#{c['id']})" for c in courses]
        self.course_box["values"] = names
        if courses:
            preferred = next((i for i, c in enumerate(courses) if "physics" in c["name"].lower()), 0)
            self.course_box.current(preferred)
        self.log(f"Loaded {len(courses)} purchased course(s).")

    def start_download(self):
        if not self.api:
            if self.email_var.get().strip() and self.pass_var.get():
                messagebox.showinfo("Login needed", "Press the Login button once, then start the download.")
            else:
                messagebox.showwarning("Login needed", "Enter your email + password and press Login first.")
            return
        idx = self.course_box.current()
        if idx < 0 or idx >= len(self.courses):
            messagebox.showwarning("Download", "Choose a course.")
            return
        save_dir = Path(self.dir_var.get().strip() or "D:\\LA-Downloads")
        try:
            save_dir.mkdir(parents=True, exist_ok=True)
        except Exception as exc:
            messagebox.showerror("Download", f"Cannot create folder:\n{save_dir}\n{exc}")
            return
        course = self.courses[idx]
        try:
            seg_workers = int(self.seg_var.get())
        except ValueError:
            seg_workers = 48
        try:
            parallel_videos = int(self.par_var.get())
        except ValueError:
            parallel_videos = 2
        self.conf.update({
            "save_dir": str(save_dir),
            "email": self.email_var.get().strip(),
            "seg_workers": seg_workers,
            "parallel_videos": parallel_videos,
            "resume_history": bool(self.resume_var.get()),
        })
        save_json(CONF_PATH, self.conf)
        self.downloader = Downloader(
            self.api, course["id"], course["name"], save_dir, self.q,
            seg_workers=seg_workers, parallel_videos=parallel_videos,
            skip_history=bool(self.resume_var.get()),
        )
        self.downloader.start()
        self.start_btn.configure(state="disabled")
        self.stop_btn.configure(state="normal")
        self.progress.configure(value=0)

    def reset_progress(self):
        idx = self.course_box.current()
        if idx < 0 or idx >= len(self.courses):
            messagebox.showwarning("Reset", "Choose a course first.")
            return
        course = self.courses[idx]
        if not messagebox.askyesno("Reset progress",
                                   f"Forget download history for\n{course['name']}?\n\nFiles on disk are NOT deleted."):
            return
        base = Path(self.dir_var.get().strip() or "D:\\LA-Downloads")
        for path in (base / ".la-sync" / f"{course['id']}.json",
                     DATA_DIR / f"completed-{course['id']}.json"):
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass
        self.log(f"Progress reset for {course['name']} — everything can be downloaded again.")

    def _update_disk_label(self):
        try:
            base = Path(self.dir_var.get().strip() or "D:\\LA-Downloads")
            anchor = str(base.anchor or base)
            free = shutil.disk_usage(anchor).free
            self.disk_label.configure(text=f"{anchor}  {free / (1 << 30):.1f} GB free")
        except Exception:
            self.disk_label.configure(text="")

    def stop_download(self):
        if self.downloader:
            self.downloader.stop_event.set()
            self.log("Stopping after current chunk…", "warn")

    # ── UI event pump ───────────────────────────────────────────────────────
    def poll(self):
        try:
            while True:
                kind, payload, extra = self.q.get_nowait()
                if kind == "log":
                    self.log(payload)
                elif kind == "status":
                    self.status_label.configure(text=payload)
                elif kind == "progress":
                    self.progress.configure(value=payload)
                    if extra:
                        self.status_label.configure(text=f"{self.status_label.cget('text').split('  ·  ')[0]}  ·  {extra}")
                elif kind == "speed":
                    self.speed_label.configure(text=f"{payload / 1048576:.1f} MB/s")
                elif kind == "disk_free":
                    self.disk_label.configure(text=f"{Path(self.dir_var.get().strip()).anchor}  {payload / (1 << 30):.1f} GB free")
                elif kind == "counts":
                    done, failed, skipped = payload
                    self.counts_label.configure(text=f"Saved {done} · skipped {skipped} · failed {failed}")
                elif kind == "courses":
                    self._set_courses(payload)
                elif kind == "api_ready":
                    self.api = payload
                elif kind == "login_ok":
                    self.login_status.configure(text=f"logged in · user {payload}", fg="#34d399")
                elif kind == "login_err":
                    self.login_status.configure(text="login failed", fg="#f87171")
                    self.log(f"Login failed: {payload}", "error")
                    messagebox.showerror("Login", payload)
                elif kind == "login_save":
                    email, password, token, uid = payload
                    self.conf.update({"email": email, "token": token, "uid": uid})
                    save_json(CONF_PATH, self.conf)
                elif kind == "login_enable":
                    self.login_btn.configure(state="normal")
                elif kind == "finished":
                    done, failed, skipped = payload
                    self.start_btn.configure(state="normal")
                    self.stop_btn.configure(state="disabled")
                    self.status_label.configure(text=f"Finished — saved {done}, skipped {skipped}, failed {failed}")
                    messagebox.showinfo("Done", f"Download finished.\n\nSaved: {done}\nSkipped: {skipped}\nFailed: {failed}")
                elif kind == "stopped":
                    self.start_btn.configure(state="normal")
                    self.stop_btn.configure(state="disabled")
                    self.status_label.configure(text="Stopped.")
                elif kind == "error":
                    self.start_btn.configure(state="normal")
                    self.stop_btn.configure(state="disabled")
        except queue.Empty:
            pass
        self._update_disk_label()
        self.after(150, self.poll)


def main():
    app = App()
    app.mainloop()


if __name__ == "__main__":
    main()
