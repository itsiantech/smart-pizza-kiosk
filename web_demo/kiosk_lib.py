"""Shared kiosk engine: paths, messages, SQLite, Haar+LBPH recognizer, browser TTS.

Used by both web frontends (streamlit_app.py = live WebRTC video,
scan_app.py = photo-scan for restricted networks). No Streamlit *UI* calls
here except inside speak_js() — safe to import from tests.
"""

import json
import sqlite3
import threading
from pathlib import Path

import cv2
import numpy as np
import streamlit.components.v1 as components

# ---------------------------------------------------------------- paths ---
BASE_DIR = Path(__file__).resolve().parent
REPO_ROOT = BASE_DIR.parent
CASCADE_PATH = REPO_ROOT / "assets" / "haarcascade_frontalface_default.xml"
KNOWN_DIR = BASE_DIR / "known_faces"
KNOWN_DIR.mkdir(exist_ok=True)
SNAP_DIR = BASE_DIR / "guest_snaps"
SNAP_DIR.mkdir(exist_ok=True)
DB_PATH = BASE_DIR / "kiosk_web.db"

FACE_SIZE = (200, 200)
LBPH_THRESHOLD = 80.0
COOLDOWN_SEC = 15
UNKNOWN_COOLDOWN_SEC = 30

MENU = {"Margherita": 8.99, "Pepperoni": 10.99, "Veggie": 9.49, "Coke": 1.99}


def returning_msg(name, last_order=None):
    if last_order:
        return (f"{name}, tomake Pizza Globe-e shagotom! Agerbar tumi "
                f"{last_order} order korechile. Shetai abar order korbe, "
                f"naki notun kichu korbe, janao. Dhonnobad.")
    return (f"{name}, tomake Pizza Globe-e shagotom! "
            f"Aj ki order korbe, amader janao. Dhonnobad.")


NEW_MSG = "Apnake Pizza Globe-e shagotom! Apnar order ki hobe, amader janan."


# ------------------------------------------------------------------- db ---
_db_lock = threading.Lock()


def db():
    conn = sqlite3.connect(str(DB_PATH), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    with _db_lock, conn:
        conn.executescript(
            """CREATE TABLE IF NOT EXISTS customers(
                 id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT,
                 photo_path TEXT, created_at TEXT);
               CREATE TABLE IF NOT EXISTS orders(
                 id INTEGER PRIMARY KEY AUTOINCREMENT, customer_id INTEGER,
                 order_items TEXT, total_bill REAL, order_time TEXT);""")
    return conn


CONN = db()


def add_customer(name, photo=None):
    with _db_lock, CONN:
        cur = CONN.execute(
            "INSERT INTO customers (name, photo_path, created_at) "
            "VALUES (?, ?, datetime('now'))", (name, photo))
        return cur.lastrowid


def get_customer(name):
    with _db_lock:
        return CONN.execute(
            "SELECT * FROM customers WHERE name=? ORDER BY id DESC LIMIT 1",
            (name,)).fetchone()


def last_order(cid):
    with _db_lock:
        return CONN.execute(
            "SELECT * FROM orders WHERE customer_id=? ORDER BY id DESC LIMIT 1",
            (cid,)).fetchone()


def add_order(cid, items, total):
    with _db_lock, CONN:
        cur = CONN.execute(
            "INSERT INTO orders (customer_id, order_items, total_bill, order_time)"
            " VALUES (?, ?, ?, datetime('now'))", (cid, items, total))
        return cur.lastrowid


# ------------------------------------------------------------ recognizer ---
def person_name(stem):
    if "_" in stem:
        head, tail = stem.rsplit("_", 1)
        if tail.isdigit():
            return head
    return stem


class Recognizer:
    """Haar + LBPH (same engine as the desktop app, dlib-free)."""

    def __init__(self):
        self.lock = threading.Lock()
        self.detector = cv2.CascadeClassifier(str(CASCADE_PATH))
        self.rec = None
        self.trained = False
        self.l2n, self.n2l, self.samples, self.labels = {}, {}, [], []
        self.load()

    def _label(self, name):
        if name not in self.n2l:
            i = len(self.n2l)
            self.n2l[name] = i
            self.l2n[i] = name
        return self.n2l[name]

    def _retrain(self):
        if not self.samples:
            self.trained = False
            return
        self.rec = cv2.face.LBPHFaceRecognizer_create()
        self.rec.train(self.samples, np.array(self.labels, dtype=np.int32))
        self.trained = True

    def load(self):
        """Rebuild from known_faces/ on disk (drops runtime guest shadows)."""
        with self.lock:
            self.samples, self.labels = [], []
            self.l2n, self.n2l = {}, {}
            n = 0
            for p in sorted(KNOWN_DIR.glob("*")):
                if p.suffix.lower() not in (".jpg", ".jpeg", ".png"):
                    continue
                gray = cv2.imread(str(p), cv2.IMREAD_GRAYSCALE)
                if gray is None:
                    continue
                name = person_name(p.stem)
                faces = self._detect(gray)
                crops = [cv2.resize(gray[y:y + h, x:x + w], FACE_SIZE)
                         for (x, y, w, h) in faces] or [cv2.resize(gray, FACE_SIZE)]
                for c in crops:
                    self.samples.append(c)
                    self.labels.append(self._label(name))
                    n += 1
            self._retrain()
            return n, len(self.n2l)

    def _detect(self, gray):
        if self.detector.empty():
            return []
        boxes = self.detector.detectMultiScale(
            gray, scaleFactor=1.1, minNeighbors=5, minSize=(60, 60))
        return sorted([tuple(map(int, b)) for b in boxes],
                      key=lambda b: b[2] * b[3], reverse=True)

    def identify(self, crop):
        with self.lock:
            if not self.trained:
                return None
            try:
                label, dist = self.rec.predict(cv2.resize(crop, FACE_SIZE))
                name = self.l2n.get(int(label))
                return name if (name and dist <= LBPH_THRESHOLD) else None
            except Exception:
                return None

    def learn(self, name, crop):
        with self.lock:
            self.samples.append(cv2.resize(crop, FACE_SIZE))
            self.labels.append(self._label(name))
            self._retrain()


RECO = Recognizer()


def speak_js(text: str):
    """Make the *browser* speak (uses OS voices, incl. Bengali if present)."""
    components.html(f"""<script>
    try {{
      const u = new SpeechSynthesisUtterance({json.dumps(text)});
      const vs = speechSynthesis.getVoices();
      const bn = vs.find(v => (v.lang || '').toLowerCase().startsWith('bn'));
      if (bn) u.voice = bn;
      u.rate = 0.95;
      speechSynthesis.cancel();
      speechSynthesis.speak(u);
    }} catch (e) {{}}
    </script>""", height=0)
