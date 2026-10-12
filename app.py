"""
Smart Pizza Shop Kiosk System — offline-first, production-ready single-file app.

Features:
  * Webcam face recognition — face_recognition (dlib) if installed, else
    offline OpenCV LBPH recognizer (opencv-contrib, no Visual Studio needed)
  * Face enrollment: `python app.py --enroll YourName` saves your face samples
  * Offline TTS greeting via pyttsx3 (returning vs new customer)
  * Per-person greeting cooldown (default 15 s) to avoid audio spam
  * Auto-registration of unknown faces -> customer_photos/ + SQLite (is_synced=0)
  * Background sync worker thread -> POST unsynced rows to central API, mark is_synced=1
  * Fully offline-tolerant: no crash when camera / TTS / internet is missing
  * Simple order capture: press `o` on the video window (or CLI) -> orders table

Project layout (auto-created on first run):
  ./known_faces/       CustomerName.jpg  (one face per image)
  ./customer_photos/   auto snapshots
  ./kiosk_local.db     SQLite (customers, orders)

Usage:
  pip install -r requirements.txt
  python app.py
  python app.py --enroll Rahim        # first: save YOUR face (look at webcam, ~25 shots)
  python app.py                       # then: recognizes you + greets by name
  python app.py --no-camera --demo       # headless self-test without webcam
  python app.py --sync-url https://httpbin.org/post --cooldown 15

Press `q` in the video window to quit, `o` to place an order for last seen customer.
"""

from __future__ import annotations

import argparse
import datetime
import importlib
import json
import logging
import queue
import shutil
import socket
import sqlite3
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

BASE_DIR = Path(__file__).resolve().parent
KNOWN_FACES_DIR = BASE_DIR / "known_faces"
CUSTOMER_PHOTOS_DIR = BASE_DIR / "customer_photos"
DB_PATH = BASE_DIR / "kiosk_local.db"

DEFAULT_SYNC_URL = "https://httpbin.org/post"  # mock central API; override with --sync-url
CONNECTIVITY_HOST = ("8.8.8.8", 53)            # fast TCP check, no HTTP needed
SYNC_INTERVAL_SEC = 15
GREET_COOLDOWN_SEC = 15
UNKNOWN_RECAPTURE_COOLDOWN_SEC = 30
FACE_TOLERANCE = 0.5
FACE_SIZE = (200, 200)     # LBPH training/recognition image size (grayscale)
LBPH_THRESHOLD = 80.0      # LBPH distance: LOWER = better match; above = unknown
FRAME_SCALE = 0.5          # downscale frames for speed; boxes rescaled on draw
PROCESS_EVERY_N_FRAMES = 2
ENROLL_SHOTS = 25          # face samples captured by --enroll

SHOP_NAME = "পিজ্জা গ্লোব"
SHOP_NAME_LATIN = "Pizza Globe"

# Returning customer — previous order is filled in from the DB when known.
RETURNING_MSG_BN = ("{name}, তোমাকে পিজ্জা গ্লোবে স্বাগতম! "
                    "আগেরবার তুমি {last_order} অর্ডার করেছিলে। "
                    "সেটাই আবার অর্ডার করবে, নাকি নতুন কিছু করবে জানাও। ধন্যবাদ।")
RETURNING_MSG_BN_NOPREV = ("{name}, তোমাকে পিজ্জা গ্লোবে স্বাগতম! "
                           "আজ কী অর্ডার করবে আমাদের জানাও। ধন্যবাদ।")
RETURNING_MSG = ("{name}, tomake Pizza Globe-e shagotom! "
                 "Agerbar tumi {last_order} order korechile. "
                 "Shetai abar order korbe, naki notun kichu korbe, janao. Dhonnobad.")
RETURNING_MSG_NOPREV = ("{name}, tomake Pizza Globe-e shagotom! "
                        "Aj ki order korbe, amader janao. Dhonnobad.")
# New customer.
NEW_CUSTOMER_MSG_BN = ("আপনাকে পিজ্জা গ্লোবে স্বাগতম! "
                       "আপনার অর্ডার কী হবে আমাদের জানান।")
NEW_CUSTOMER_MSG = ("Apnake Pizza Globe-e shagotom! "
                    "Apnar order ki hobe, amader janan.")


def _has_bengali(text: str) -> bool:
    """True if text contains Bengali Unicode characters (U+0980–U+09FF)."""
    return any("\u0980" <= ch <= "\u09FF" for ch in text)


ESPEAK_CANDIDATES = (
    "espeak-ng",
    "espeak",
    r"C:\Program Files\eSpeak NG\espeak-ng.exe",
)


def find_espeak() -> str | None:
    """Locate an espeak-ng binary (offline Bengali TTS). None when absent."""
    for cand in ESPEAK_CANDIDATES:
        try:
            if Path(cand).is_file():
                return cand
            found = shutil.which(cand)
            if found:
                return found
        except Exception:
            continue
    return None

REQUIRED_PIP_PACKAGES = {
    "cv2": "opencv-contrib-python",  # contrib wheel includes cv2.face (LBPH)
    "pyttsx3": "pyttsx3",
    "requests": "requests",
}
# face-recognition (dlib) is detected opportunistically at runtime — NOT
# auto-installed, because building dlib on Windows needs Visual Studio C++.
OPTIONAL_MODULES = ("face_recognition",)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(threadName)s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("kiosk")

if sys.platform == "win32":
    # Windows console defaults to cp1252/cp437 which cannot print Bengali script.
    # Without this, logging a Bangla greeting crashes with UnicodeEncodeError.
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


@dataclass
class Config:
    sync_url: str = DEFAULT_SYNC_URL
    tolerance: float = FACE_TOLERANCE
    cooldown: int = GREET_COOLDOWN_SEC
    sync_interval: int = SYNC_INTERVAL_SEC
    no_camera: bool = False
    demo: bool = False
    camera_index: int = 0
    enroll: str | None = None       # --enroll Name: capture face samples then exit
    enroll_shots: int = ENROLL_SHOTS
    lbph_threshold: float = LBPH_THRESHOLD
    say_test: str | None = None     # --say-test "text": speak once (speaker check) then exit
    voice: str = "auto"             # "auto" = SAPI5 Latin (reliable); "bn" = espeak Bengali script


# ---------------------------------------------------------------------------
# 1. Environment bootstrap
# ---------------------------------------------------------------------------

def ensure_directories() -> None:
    """Create required folders (+ .gitkeep so empty dirs survive in git)."""
    for d in (KNOWN_FACES_DIR, CUSTOMER_PHOTOS_DIR):
        d.mkdir(parents=True, exist_ok=True)
        keep = d / ".gitkeep"
        if not any(d.iterdir()) or not keep.exists():
            try:
                if not keep.exists():
                    keep.touch()
            except OSError:
                pass
    log.info("Directories ready: %s, %s", KNOWN_FACES_DIR.name, CUSTOMER_PHOTOS_DIR.name)


def ensure_dependencies() -> None:
    """Check imports; pip-install whatever is missing (best effort, never fatal)."""
    missing: list[str] = []
    for module, pip_name in REQUIRED_PIP_PACKAGES.items():
        try:
            importlib.import_module(module)
        except ImportError:
            missing.append(pip_name)
    if not missing:
        return
    log.warning("Missing dependencies: %s — attempting auto-install…", ", ".join(missing))
    try:
        subprocess.check_call(
            [sys.executable, "-m", "pip", "install", "--quiet", *missing],
            timeout=600,
        )
        log.info("Auto-install complete.")
    except Exception as exc:  # offline machine, no pip, etc. -> degrade gracefully
        log.error("Auto-install failed (%s). Continuing with degraded features.", exc)


# Lazy / tolerant third-party imports — each may be None when offline or uninstallable
cv2 = None
face_recognition = None
pyttsx3 = None
requests = None
numpy = None


def import_third_party() -> None:
    global cv2, face_recognition, pyttsx3, requests, numpy
    for name in ("cv2", "face_recognition", "pyttsx3", "requests", "numpy"):
        try:
            globals()[name] = importlib.import_module(name)
        except ImportError:
            globals()[name] = None
            log.warning("Optional module '%s' unavailable — related feature disabled.", name)


# ---------------------------------------------------------------------------
# 2. SQLite layer (thread-safe, WAL, offline-first schema)
# ---------------------------------------------------------------------------

SCHEMA = """
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS customers (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    name          TEXT NOT NULL,
    face_encoding TEXT,                       -- JSON list of 128 floats (nullable when encoder missing)
    photo_path    TEXT,
    is_synced     INTEGER NOT NULL DEFAULT 0, -- 0 = local-only, 1 = pushed to cloud
    created_at    TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS orders (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    customer_id INTEGER REFERENCES customers(id) ON DELETE SET NULL,
    order_items TEXT NOT NULL,                 -- JSON or comma-separated items
    total_bill  REAL NOT NULL DEFAULT 0,
    is_synced   INTEGER NOT NULL DEFAULT 0,
    order_time  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_customers_synced ON customers(is_synced);
CREATE INDEX IF NOT EXISTS idx_orders_synced    ON orders(is_synced);
"""


class Database:
    """Small thread-safe wrapper around the local SQLite file."""

    def __init__(self, path: Path = DB_PATH):
        self.path = path
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock, self._conn:
            self._conn.executescript(SCHEMA)

    # -- customers ---------------------------------------------------------
    def add_customer(self, name: str, encoding: list[float] | None,
                     photo_path: str | None) -> int:
        now = datetime.datetime.now().isoformat(timespec="seconds")
        blob = json.dumps(encoding) if encoding is not None else None
        with self._lock, self._conn:
            cur = self._conn.execute(
                "INSERT INTO customers (name, face_encoding, photo_path, is_synced, created_at)"
                " VALUES (?, ?, ?, 0, ?)",
                (name, blob, photo_path, now),
            )
            return cur.lastrowid

    def get_customer_by_name(self, name: str) -> sqlite3.Row | None:
        with self._lock:
            return self._conn.execute(
                "SELECT * FROM customers WHERE name = ? ORDER BY id DESC LIMIT 1", (name,)
            ).fetchone()

    def get_last_order(self, customer_id: int) -> sqlite3.Row | None:
        """Most recent order of a customer (used to remind them in the greeting)."""
        with self._lock:
            return self._conn.execute(
                "SELECT * FROM orders WHERE customer_id = ? ORDER BY id DESC LIMIT 1",
                (customer_id,),
            ).fetchone()

    def get_unsynced_customers(self) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(
                "SELECT * FROM customers WHERE is_synced = 0 ORDER BY id").fetchall()

    # -- orders ------------------------------------------------------------
    def add_order(self, customer_id: int | None, items: str, total: float) -> int:
        now = datetime.datetime.now().isoformat(timespec="seconds")
        with self._lock, self._conn:
            cur = self._conn.execute(
                "INSERT INTO orders (customer_id, order_items, total_bill, is_synced, order_time)"
                " VALUES (?, ?, ?, 0, ?)",
                (customer_id, items, total, now),
            )
            return cur.lastrowid

    def get_unsynced_orders(self) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(
                "SELECT * FROM orders WHERE is_synced = 0 ORDER BY id").fetchall()

    def mark_synced(self, table: str, row_id: int) -> None:
        assert table in ("customers", "orders")
        with self._lock, self._conn:
            self._conn.execute(f"UPDATE {table} SET is_synced = 1 WHERE id = ?", (row_id,))

    def stats(self) -> dict:
        with self._lock:
            c = self._conn.execute("SELECT COUNT(*), SUM(is_synced=0) FROM customers").fetchone()
            o = self._conn.execute("SELECT COUNT(*), SUM(is_synced=0) FROM orders").fetchone()
        return {"customers_total": c[0], "customers_pending": c[1] or 0,
                "orders_total": o[0], "orders_pending": o[1] or 0}


# ---------------------------------------------------------------------------
# 3. Offline TTS (pyttsx3) — serial worker thread so speech never blocks video
# ---------------------------------------------------------------------------

class Speaker:
    """Offline TTS with two backends:
    * Bengali text (বাংলা অক্ষর) -> espeak-ng `-v bn` (if installed)
    * Everything else           -> pyttsx3/SAPI5 voice.
    """

    def __init__(self, enabled: bool = True, rate: int = 150):
        self.enabled = enabled and (pyttsx3 is not None or find_espeak() is not None)
        self._espeak = find_espeak()
        self._q: queue.Queue[str] = queue.Queue()
        self._thread = threading.Thread(target=self._loop, name="TTS", daemon=True)
        self._engine = None
        if pyttsx3 is not None:
            try:
                # Explicit driver: 'sapi5' on Windows is more reliable than auto-detect.
                driver = "sapi5" if sys.platform == "win32" else None
                self._engine = pyttsx3.init(driver) if driver else pyttsx3.init()
                try:
                    self._engine.setProperty("rate", rate)
                    self._engine.setProperty("volume", 1.0)
                    voices = self._engine.getProperty("voices") or []
                    first = getattr(voices[0], "name", "?") if voices else "?"
                    log.info("TTS ready: %d voice(s), using '%s'", len(voices), first)
                except Exception as exc:
                    log.error("TTS property setup failed: %s", exc)
            except Exception as exc:
                log.error("TTS init failed (%s) — SAPI5 disabled.", exc)
                self._engine = None
        if self._espeak:
            log.info("Bangla TTS ready via espeak-ng: %s", self._espeak)
        elif self.enabled:
            log.warning("espeak-ng missing — Bengali script will be skipped. "
                        "Install it for real Bangla voice.")
        self._thread.start()

    @property
    def supports_bangla(self) -> bool:
        return self._espeak is not None

    def say(self, text: str) -> None:
        log.info("TTS >> %s", text)
        if self.enabled:
            self._q.put(text)  # non-blocking; worker speaks sequentially

    def _speak_espeak(self, text: str) -> None:
        operable = self._espeak
        assert operable
        subprocess.run([operable, "-v", "bn", "-s", "150", "-a", "100", text],
                       timeout=60, check=False,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def drain(self, timeout: float = 20.0) -> None:
        """Wait until queued utterances finish — call before program exit,
        otherwise the OS kills the daemon thread mid-sentence and you hear nothing."""
        end = time.time() + timeout
        while self._q.unfinished_tasks > 0 and time.time() < end:
            time.sleep(0.1)
        time.sleep(0.5)  # let the audio buffer flush

    def _loop(self) -> None:
        while True:
            text = self._q.get()
            try:
                if _has_bengali(text) and self._espeak is not None:
                    self._speak_espeak(text)
                elif self._engine is not None:
                    self._engine.say(text)
                    self._engine.runAndWait()
                elif self._espeak is not None:
                    # No SAPI5 engine, but espeak exists: strip Bengali and speak rest.
                    ascii_only = "".join(ch for ch in text if ord(ch) < 128)
                    self._speak_espeak(ascii_only or text)
            except Exception as exc:
                log.error("TTS speak failed: %s", exc)
            finally:
                self._q.task_done()


def greet_returning(speaker: Speaker, name: str, last_order: str | None = None) -> None:
    """Bengali script only when --voice bn was requested AND espeak works;
    otherwise Latin transliteration via the SAPI5 English voice (reliable)."""
    bn = bool(getattr(speaker, "use_bangla_script", False))
    if last_order:
        msg = RETURNING_MSG_BN if bn else RETURNING_MSG
        speaker.say(msg.format(name=name, last_order=last_order))
    else:
        msg = RETURNING_MSG_BN_NOPREV if bn else RETURNING_MSG_NOPREV
        speaker.say(msg.format(name=name))


def greet_new_customer(speaker: Speaker) -> None:
    bn = bool(getattr(speaker, "use_bangla_script", False))
    speaker.say(NEW_CUSTOMER_MSG_BN if bn else NEW_CUSTOMER_MSG)


# ---------------------------------------------------------------------------
# 4. Face library — load known_faces/CustomerName.jpg into encodings
# ---------------------------------------------------------------------------

class FaceLibrary:
    """In-memory parallel lists: names[i] <-> encodings[i]. Grows as guests register."""

    def __init__(self, tolerance: float = FACE_TOLERANCE):
        self.tolerance = tolerance
        self.names: list[str] = []
        self.encodings: list = []

    def load_from_disk(self) -> None:
        if face_recognition is None or cv2 is None:
            log.warning("face_recognition/cv2 missing — known-face loading skipped.")
            return
        files = sorted(KNOWN_FACES_DIR.glob("*"))
        for img_path in files:
            if img_path.name == ".gitkeep" or img_path.suffix.lower() not in (".jpg", ".jpeg", ".png"):
                continue
            name = img_path.stem  # CustomerName.jpg -> CustomerName
            try:
                image = face_recognition.load_image_file(str(img_path))
                encs = face_recognition.face_encodings(image)
                if not encs:
                    log.warning("No face found in %s — skipped.", img_path.name)
                    continue
                self.names.append(name)
                self.encodings.append(encs[0])
                log.info("Loaded known face: %s", name)
            except Exception as exc:
                log.error("Failed to encode %s: %s", img_path.name, exc)
        log.info("Known faces loaded: %d", len(self.names))

    def identify(self, encoding) -> str | None:
        """Return matched name or None. Uses compare_faces(tolerance=0.5) + best distance."""
        if face_recognition is None or not self.encodings:
            return None
        try:
            import numpy as _np  # local alias; face_recognition depends on numpy anyway
            matches = face_recognition.compare_faces(
                self.encodings, encoding, tolerance=self.tolerance)
            distances = face_recognition.face_distance(self.encodings, encoding)
            best = int(_np.argmin(distances))
            return self.names[best] if matches[best] else None
        except Exception as exc:
            log.error("identify() failed: %s", exc)
            return None

    def add(self, name: str, encoding) -> None:
        self.names.append(name)
        self.encodings.append(encoding)


def _person_name(stem: str) -> str:
    """'Rahim_3' -> 'Rahim' (multi-shot enrollments); 'Rahim' -> 'Rahim'."""
    if "_" in stem:
        head, tail = stem.rsplit("_", 1)
        if tail.isdigit():
            return head
    return stem


CASCADE_CANDIDATES = (
    BASE_DIR / "assets" / "haarcascade_frontalface_default.xml",
)


class OpenCVBackend:
    """Offline Haar-cascade + LBPH face recognizer. No dlib / Visual Studio needed.

    Used automatically when `face_recognition` is not installed.
    LBPH `predict` returns (label, distance); LOWER distance = better match.
    Anything above `threshold` counts as an unknown face.
    """

    def __init__(self, threshold: float = LBPH_THRESHOLD):
        self.threshold = threshold
        self.detector = None
        self.recognizer = None
        self.trained = False
        self._label_to_name: dict[int, str] = {}
        self._name_to_label: dict[str, int] = {}
        self._samples: list = []
        self._labels: list[int] = []
        if cv2 is None:
            return
        candidates = list(CASCADE_CANDIDATES)
        try:
            candidates.append(
                Path(cv2.data.haarcascades) / "haarcascade_frontalface_default.xml")
        except Exception:
            pass
        for cand in candidates:
            try:
                if cand.exists():
                    det = cv2.CascadeClassifier(str(cand))
                    if not det.empty():
                        self.detector = det
                        log.info("Haar cascade loaded from %s", cand)
                        break
            except Exception as exc:
                log.error("Cascade load failed (%s): %s", cand, exc)
        if self.detector is None:
            log.error("No Haar cascade found — face detection disabled.")
        if not hasattr(cv2, "face"):
            log.error("cv2.face missing — install opencv-contrib-python for recognition.")

    # -- training ------------------------------------------------------
    def _label_for(self, name: str) -> int:
        if name not in self._name_to_label:
            lbl = len(self._name_to_label)
            self._name_to_label[name] = lbl
            self._label_to_name[lbl] = name
        return self._name_to_label[name]

    def _retrain(self) -> None:
        if cv2 is None or not hasattr(cv2, "face") or not self._samples:
            self.trained = False
            return
        try:
            import numpy as _np
            self.recognizer = cv2.face.LBPHFaceRecognizer_create()
            self.recognizer.train(self._samples, _np.array(self._labels, dtype=_np.int32))
            self.trained = True
        except Exception as exc:
            log.error("LBPH train failed: %s", exc)
            self.trained = False

    def detect(self, gray_img) -> list[tuple[int, int, int, int]]:
        """Face boxes (x, y, w, h), largest first."""
        try:
            boxes = self.detector.detectMultiScale(
                gray_img, scaleFactor=1.1, minNeighbors=5, minSize=(60, 60))
            return sorted([tuple(int(v) for v in b) for b in boxes],
                          key=lambda b: b[2] * b[3], reverse=True)
        except Exception as exc:
            log.error("detect() failed: %s", exc)
            return []

    def _face_crops(self, gray_img) -> list:
        crops = []
        for (x, y, w, h) in self.detect(gray_img):
            crop = gray_img[y:y + h, x:x + w]
            if crop is not None and crop.size > 0:
                crops.append(cv2.resize(crop, FACE_SIZE))
        return crops

    def load_from_disk(self) -> None:
        """Train on known_faces/*.jpg (supports multi-shot NAME_1.jpg, NAME_2.jpg…)."""
        if cv2 is None:
            return
        n = 0
        for img_path in sorted(KNOWN_FACES_DIR.glob("*")):
            if img_path.name in (".gitkeep", "README.txt"):
                continue
            if img_path.suffix.lower() not in (".jpg", ".jpeg", ".png"):
                continue
            name = _person_name(img_path.stem)
            gray = cv2.imread(str(img_path), cv2.IMREAD_GRAYSCALE)
            if gray is None:
                log.warning("Cannot read %s — skipped.", img_path.name)
                continue
            crops = self._face_crops(gray)
            if not crops:  # close-up enrollment shot with no detection -> whole image
                crops = [cv2.resize(gray, FACE_SIZE)]
            for crop in crops:
                self._samples.append(crop)
                self._labels.append(self._label_for(name))
                n += 1
        self._retrain()
        log.info("LBPH known faces: %d samples, %d people, trained=%s",
                 n, len(self._name_to_label), self.trained)

    def identify(self, face_img) -> tuple[str | None, float]:
        """Return (matched name or None, LBPH distance)."""
        if not self.trained or self.recognizer is None:
            return None, float("inf")
        try:
            label, dist = self.recognizer.predict(cv2.resize(face_img, FACE_SIZE))
            name = self._label_to_name.get(int(label))
            if name is not None and float(dist) <= self.threshold:
                return name, float(dist)
            return None, float(dist)
        except Exception as exc:
            log.error("LBPH identify() failed: %s", exc)
            return None, float("inf")

    def add_sample(self, name: str, face_img) -> None:
        """Learn a newly seen face immediately (no restart needed)."""
        try:
            if face_img is None or face_img.size == 0:
                return
            self._samples.append(cv2.resize(face_img, FACE_SIZE))
            self._labels.append(self._label_for(name))
            self._retrain()
        except Exception as exc:
            log.error("add_sample() failed: %s", exc)


def enroll_face(cfg: Config, name: str, shots: int) -> None:
    """Capture `shots` face crops from the webcam into known_faces/NAME_N.jpg.

    Tip: look at the camera, slowly turn your head left/right, change expression
    slightly between shots so recognition is robust.
    """
    backend = OpenCVBackend()
    if cv2 is None or backend.detector is None:
        log.error("Enrollment needs OpenCV + the Haar cascade in assets/.")
        return
    cap = cv2.VideoCapture(cfg.camera_index)
    if not cap.isOpened():
        log.error("Cannot open webcam (index %d).", cfg.camera_index)
        return
    start = len(list(KNOWN_FACES_DIR.glob(f"{name}_*.jpg"))) + 1
    saved = 0
    log.info("Enrolling '%s': look at the camera, %d shots. [q] abort", name, shots)
    try:
        while saved < shots:
            ok, frame = cap.read()
            if not ok:
                time.sleep(0.1)
                continue
            small = cv2.resize(frame, (0, 0), fx=FRAME_SCALE, fy=FRAME_SCALE)
            gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)
            boxes = backend.detect(gray)
            if boxes:
                x, y, w, h = boxes[0]  # largest face only
                inv = 1.0 / FRAME_SCALE
                fx, fy, fw, fh = (int(v * inv) for v in (x, y, w, h))
                gray_full = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
                face = gray_full[fy:fy + fh, fx:fx + fw]
                if face is not None and face.size > 0:
                    out = KNOWN_FACES_DIR / f"{name}_{start + saved}.jpg"
                    cv2.imwrite(str(out), cv2.resize(face, FACE_SIZE))
                    saved += 1
                    log.info("Saved %s (%d/%d)", out.name, saved, shots)
                    cv2.rectangle(frame, (fx, fy), (fx + fw, fy + fh), (0, 200, 0), 2)
                    time.sleep(0.15)  # slight pose variation between shots
            cv2.putText(frame, f"Enrolling {name}: {saved}/{shots} (q=abort)",
                        (10, 30), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (0, 200, 0), 2)
            cv2.imshow("Enroll face (q=abort)", frame)
            if cv2.waitKey(1) & 0xFF == ord("q"):
                break
    finally:
        cap.release()
        cv2.destroyAllWindows()
    log.info("Enrollment done: %d new samples for '%s'. Now run: python app.py", saved, name)


# ---------------------------------------------------------------------------
# 5. Connectivity + background sync worker (offline-first)
# ---------------------------------------------------------------------------

def is_online(timeout: float = 3.0) -> bool:
    """Cheap TCP check (no DNS/HTTP dependency). Returns False when offline."""
    try:
        socket.create_connection(CONNECTIVITY_HOST, timeout=timeout).close()
        return True
    except OSError:
        return False


def sync_worker(db: Database, cfg: Config, stop: threading.Event) -> None:
    """Background thread: push is_synced=0 rows to central API; mark 1 on success."""
    log.info("Sync worker started (every %ds -> %s)", cfg.sync_interval, cfg.sync_url)
    while not stop.is_set():
        try:
            if requests is None:
                log.debug("requests missing — sync skipped.")
            elif not is_online():
                log.info("Offline — keeping data local, will retry.")
            else:
                _push_pending(db, cfg)
        except Exception as exc:  # never let the worker die
            log.error("Sync iteration failed (will retry): %s", exc)
        stop.wait(cfg.sync_interval)
    log.info("Sync worker stopped.")


def _push_pending(db: Database, cfg: Config) -> None:
    if requests is None:
        return
    for row in db.get_unsynced_customers():
        payload = {"type": "customer", "id": row["id"], "name": row["name"],
                   "photo_path": row["photo_path"], "created_at": row["created_at"]}
        try:
            r = requests.post(cfg.sync_url, json=payload, timeout=8)
            if r.ok:
                db.mark_synced("customers", row["id"])
                log.info("Synced customer #%d (%s)", row["id"], row["name"])
            else:
                log.warning("Server rejected customer #%d: HTTP %s", row["id"], r.status_code)
        except requests.RequestException as exc:
            log.warning("Push customer #%d failed: %s", row["id"], exc)
            return  # likely went offline mid-batch; retry whole batch next round
    for row in db.get_unsynced_orders():
        payload = {"type": "order", "id": row["id"], "customer_id": row["customer_id"],
                   "order_items": row["order_items"], "total_bill": row["total_bill"],
                   "order_time": row["order_time"]}
        try:
            r = requests.post(cfg.sync_url, json=payload, timeout=8)
            if r.ok:
                db.mark_synced("orders", row["id"])
                log.info("Synced order #%d", row["id"])
            else:
                log.warning("Server rejected order #%d: HTTP %s", row["id"], r.status_code)
        except requests.RequestException as exc:
            log.warning("Push order #%d failed: %s", row["id"], exc)
            return


# ---------------------------------------------------------------------------
# 6. New-customer registration
# ---------------------------------------------------------------------------

def register_new_customer(db: Database, library, frame, encoding) -> tuple[int, str]:
    """Snapshot face to customer_photos/, insert into SQLite, extend live library."""
    guest = f"Guest_{uuid.uuid4().hex[:6].upper()}"
    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    photo = CUSTOMER_PHOTOS_DIR / f"{guest}_{ts}.jpg"
    try:
        if cv2 is not None and frame is not None:
            cv2.imwrite(str(photo), frame)
        else:
            photo = CUSTOMER_PHOTOS_DIR / f"{guest}_{ts}.txt"
            photo.write_text("no-camera snapshot placeholder")
    except Exception as exc:
        log.error("Snapshot save failed: %s", exc)
        photo = CUSTOMER_PHOTOS_DIR / f"{guest}_{ts}.jpg"

    enc_list = encoding.tolist() if hasattr(encoding, "tolist") else (
        list(encoding) if encoding is not None else None)
    row_id = db.add_customer(guest, enc_list, str(photo))
    if encoding is not None:
        library.add(guest, encoding)  # don't re-register the same visitor every frame
    log.info("New customer registered: %s (id=%d)", guest, row_id)
    return row_id, guest


# ---------------------------------------------------------------------------
# 7. Order capture (keyboard `o` in video window, or CLI fallback)
# ---------------------------------------------------------------------------

MENU = {"1": ("Margherita", 8.99), "2": ("Pepperoni", 10.99),
        "3": ("Veggie", 9.49), "4": ("Coke", 1.99)}


def take_order_cli(db: Database, customer_name: str | None,
                   customer_id: int | None) -> int | None:
    print(f"\n--- New order for {customer_name or 'Walk-in'} ---")
    print("Menu: 1=Margherita $8.99  2=Pepperoni $10.99  3=Veggie $9.49  4=Coke $1.99")
    picks = input("Enter item numbers separated by comma (e.g. 1,2,4): ").strip()
    items, total = [], 0.0
    for p in picks.split(","):
        p = p.strip()
        if p in MENU:
            items.append(MENU[p][0])
            total += MENU[p][1]
    if not items:
        print("No valid items — order cancelled.")
        return None
    order_str = ", ".join(items)
    oid = db.add_order(customer_id, order_str, round(total, 2))
    print(f"Order #{oid} saved locally (offline-first, is_synced=0): {order_str} = ${total:.2f}")
    return oid


# ---------------------------------------------------------------------------
# 8. Main real-time loop
# ---------------------------------------------------------------------------

def run_kiosk(cfg: Config) -> None:
    db = Database()
    speaker = Speaker()
    # Voice routing: espeak-ng's Windows Bengali voice is broken (renders ~1s
    # of mumble for any sentence), so default to the SAPI5 English voice with
    # Latin transliteration. --voice bn forces Bengali script via espeak.
    speaker.use_bangla_script = (cfg.voice == "bn" and speaker.supports_bangla)
    if cfg.voice == "bn" and not speaker.supports_bangla:
        log.warning("--voice bn requested but espeak-ng not found — using Latin voice.")

    # Pick recognizer: face_recognition (dlib) when installed, else OpenCV LBPH.
    library = FaceLibrary(tolerance=cfg.tolerance)
    backend = OpenCVBackend(threshold=cfg.lbph_threshold)
    use_dlib = face_recognition is not None and cv2 is not None
    if use_dlib:
        library.load_from_disk()
        log.info("Recognizer: face_recognition (dlib), tolerance=%.2f", cfg.tolerance)
    elif cv2 is not None:
        backend.load_from_disk()
        log.info("Recognizer: OpenCV LBPH (threshold=%.1f), trained=%s",
                 cfg.lbph_threshold, backend.trained)
        if not backend.trained:
            log.warning("No known faces! Run: python app.py --enroll YourName")

    stop_sync = threading.Event()
    sync_thread = threading.Thread(target=sync_worker, args=(db, cfg, stop_sync),
                                   name="SyncWorker", daemon=True)
    sync_thread.start()

    last_greeted: dict[str, float] = {}   # name -> timestamp (returning cooldown)
    last_unknown_at = 0.0                 # global unknown-face snapshot throttle
    last_seen: tuple[str | None, int | None] = (None, None)  # for `o` order key
    frame_count = 0

    log.info("DB stats at startup: %s", db.stats())
    log.info("Controls: [q] quit  [o] order for last seen customer  [s] sync now")

    if cfg.enroll and cfg.no_camera:
        log.error("--enroll needs the webcam; re-run without --no-camera/--demo.")
        stop_sync.set()
        return

    if cfg.say_test:  # quick speaker check: speak, wait till heard, exit
        speaker.say(cfg.say_test)
        speaker.drain()
        stop_sync.set()
        return

    if cfg.no_camera or cv2 is None:
        log.warning("No camera mode — running headless. Data layer + sync worker still live.")
        _headless_demo(db, speaker, cfg, stop_sync)
        return

    if cfg.enroll:  # capture face samples, then exit (no kiosk loop)
        stop_sync.set()
        enroll_face(cfg, cfg.enroll, cfg.enroll_shots)
        log.info("Final DB stats: %s", db.stats())
        return

    cap = cv2.VideoCapture(cfg.camera_index)
    if not cap.isOpened():
        log.error("Cannot open webcam (index %d). Re-run with --no-camera.", cfg.camera_index)
        stop_sync.set()
        return

    try:
        while True:
            ok, frame = cap.read()
            if not ok:
                log.error("Frame grab failed — retrying…")
                time.sleep(0.2)
                continue
            frame_count += 1
            now = time.time()

            # Throttle heavy encoding work to every Nth frame
            if frame_count % PROCESS_EVERY_N_FRAMES != 0:
                cv2.imshow("Smart Pizza Kiosk  (q=quit, o=order, s=sync)", frame)
                key = cv2.waitKey(1) & 0xFF
                if _handle_key(key, db, speaker, last_seen):
                    break
                continue

            small = cv2.resize(frame, (0, 0), fx=FRAME_SCALE, fy=FRAME_SCALE)
            inv = 1.0 / FRAME_SCALE

            # -- detect faces with whichever recognizer is active --
            faces: list = []  # [((top, right, bottom, left), (kind, data))]
            if use_dlib:
                rgb = cv2.cvtColor(small, cv2.COLOR_BGR2RGB)
                locations = face_recognition.face_locations(rgb)
                encodings = face_recognition.face_encodings(rgb, locations)
                for (top, right, bottom, left), enc in zip(locations, encodings):
                    faces.append(((top, right, bottom, left), ("dlib", enc)))
            else:
                if backend.detector is None:
                    cv2.imshow("Smart Pizza Kiosk  (q=quit, o=order, s=sync)", frame)
                    key = cv2.waitKey(1) & 0xFF
                    if _handle_key(key, db, speaker, last_seen):
                        break
                    continue
                gray_small = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)
                for (x, y, w, h) in backend.detect(gray_small):
                    crop = gray_small[y:y + h, x:x + w]
                    faces.append(((y, x + w, y + h, x), ("lbph", crop)))

            for (top, right, bottom, left), (kind, data) in faces:
                if kind == "dlib":
                    name = library.identify(data)
                else:
                    name, _dist = backend.identify(data)
                x1, y1, x2, y2 = int(left * inv), int(top * inv), int(right * inv), int(bottom * inv)

                if name:  # ---- Returning customer ----
                    color, label = (0, 200, 0), name
                    last = last_greeted.get(name, 0)
                    row = db.get_customer_by_name(name)
                    last_seen = (name, row["id"] if row else None)
                    if now - last >= cfg.cooldown:
                        prev = db.get_last_order(row["id"]) if row else None
                        greet_returning(
                            speaker, name,
                            prev["order_items"] if prev else None)
                        last_greeted[name] = now
                else:     # ---- New customer ----
                    color, label = (0, 0, 255), "New Customer"
                    if now - last_unknown_at >= UNKNOWN_RECAPTURE_COOLDOWN_SEC:
                        last_unknown_at = now
                        if kind == "dlib":
                            row_id, guest = register_new_customer(db, library, frame, data)
                        else:
                            row_id, guest = register_new_customer(db, backend, frame, None)
                            try:  # learn this face immediately (full-res crop)
                                gray_full = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
                                backend.add_sample(guest, gray_full[y1:y2, x1:x2])
                            except Exception as exc:
                                log.error("LBPH learn-new-face failed: %s", exc)
                        greet_new_customer(speaker)
                        last_seen = (guest, row_id)
                        last_greeted[guest] = now

                cv2.rectangle(frame, (x1, y1), (x2, y2), color, 2)
                cv2.putText(frame, label, (x1, y1 - 10), cv2.FONT_HERSHEY_SIMPLEX,
                            0.7, color, 2)

            cv2.imshow("Smart Pizza Kiosk  (q=quit, o=order, s=sync)", frame)
            key = cv2.waitKey(1) & 0xFF
            if _handle_key(key, db, speaker, last_seen, cfg):
                break
    except KeyboardInterrupt:
        log.info("Interrupted by user.")
    finally:
        cap.release()
        cv2.destroyAllWindows()
        speaker.drain()  # let the last greeting finish before exit
        stop_sync.set()
        log.info("Final DB stats: %s", db.stats())


def _handle_key(key: int, db: Database, speaker: Speaker,
                last_seen: tuple, cfg: Config | None = None) -> bool:
    """Return True when the app should quit."""
    if key == ord("q"):
        log.info("Quit requested.")
        return True
    if key == ord("o"):
        name, cid = last_seen if last_seen else (None, None)
        # Deferred import safe: input() blocks video briefly — acceptable for kiosk operator key
        take_order_cli(db, name, cid)
    if key == ord("s") and cfg is not None:
        log.info("Manual sync requested.")
        _push_pending(db, cfg)
    return False


def _headless_demo(db: Database, speaker: Speaker, cfg: Config, stop: threading.Event) -> None:
    """Exercise DB + sync + TTS without a webcam (CI / --demo / broken camera)."""
    try:
        log.info("Headless demo: registering a test guest + order, then syncing once.")
        greet_new_customer(speaker)
        cid = db.add_customer(f"DemoGuest_{uuid.uuid4().hex[:4]}", None, None)
        db.add_order(cid, "Margherita, Coke", 10.98)
        log.info("DB stats after demo inserts: %s", db.stats())
        if cfg.demo:
            _push_pending(db, cfg)  # single attempt; worker keeps retrying anyway
            log.info("DB stats after demo sync attempt: %s", db.stats())
        else:
            print("Headless mode: demo rows inserted. Press Ctrl+C to stop (sync worker running).")
            while True:
                time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        speaker.drain()
        stop.set()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args(argv: list[str] | None = None) -> Config:
    p = argparse.ArgumentParser(description="Smart Pizza Shop Kiosk (offline-first)")
    p.add_argument("--sync-url", default=DEFAULT_SYNC_URL, help="Central API endpoint for sync")
    p.add_argument("--tolerance", type=float, default=FACE_TOLERANCE,
                   help="face_recognition tolerance (default 0.5)")
    p.add_argument("--cooldown", type=int, default=GREET_COOLDOWN_SEC,
                   help="Seconds between repeat greetings per person")
    p.add_argument("--sync-interval", type=int, default=SYNC_INTERVAL_SEC)
    p.add_argument("--camera", type=int, default=0, help="OpenCV camera index")
    p.add_argument("--no-camera", action="store_true", help="Run without webcam")
    p.add_argument("--demo", action="store_true", help="Headless self-test then exit-ish")
    p.add_argument("--enroll", metavar="NAME", default=None,
                   help="Capture face samples for NAME from webcam, then exit")
    p.add_argument("--shots", type=int, default=ENROLL_SHOTS,
                   help="Face samples to capture with --enroll")
    p.add_argument("--lbph-threshold", type=float, default=LBPH_THRESHOLD,
                   help="LBPH max distance for a match (lower = stricter)")
    p.add_argument("--say-test", metavar="TEXT", default=None,
                   help='Speaker check, e.g. --say-test "Welcome back"')
    p.add_argument("--voice", choices=("auto", "bn"), default="auto",
                   help="'auto' = English voice + Latin Bangla (reliable); "
                        "'bn' = espeak-ng Bengali script (needs working espeak bn voice)")
    a = p.parse_args(argv)
    return Config(sync_url=a.sync_url, tolerance=a.tolerance, cooldown=a.cooldown,
                  sync_interval=a.sync_interval, no_camera=a.no_camera or a.demo,
                  demo=a.demo, camera_index=a.camera, enroll=a.enroll,
                  enroll_shots=a.shots, lbph_threshold=a.lbph_threshold,
                  say_test=a.say_test, voice=a.voice)


def main() -> None:
    ensure_directories()
    ensure_dependencies()
    import_third_party()
    run_kiosk(parse_args())


if __name__ == "__main__":
    main()
