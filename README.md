# Smart Pizza Shop Kiosk System

Offline-first kiosk: webcam face recognition → TTS greeting → local SQLite → background cloud sync.

## 1. Project structure

```
.
├── app.py               # complete system (run this)
├── requirements.txt
├── known_faces/         # put `CustomerName.jpg` here (one face per file)
├── customer_photos/     # auto-saved snapshots of new customers
└── kiosk_local.db       # auto-created SQLite (customers, orders)
```

SQLite schema:

- `customers(id, name, face_encoding JSON, photo_path, is_synced DEFAULT 0, created_at)`
- `orders(id, customer_id, order_items, total_bill, is_synced DEFAULT 0, order_time)`

## 2. Install & run (Windows / macOS / Linux)

```powershell
pip install -r requirements.txt
python app.py
```

> **Note on `face-recognition` (Windows):** it needs `dlib` / CMake + Visual Studio
> Build Tools. If `pip install face-recognition` fails, the app still runs in
> degraded mode (face matching disabled, registration + sync still work). For full
> accuracy on Windows, use Python 3.10–3.11 64-bit and:
> `pip install cmake dlib face-recognition`, or switch to WSL2/Ubuntu.

### Useful flags

```powershell
python app.py --help
python app.py --no-camera --demo      # headless self-test, no webcam needed
python app.py --sync-url https://httpbin.org/post --cooldown 15 --tolerance 0.5
python app.py --camera 1              # use a different webcam
```

### Add a known (returning) customer — two ways

**Way 1 — Enroll from webcam (recommended, best accuracy):**
```powershell
python app.py --enroll Rahim
```
Webcam-এর দিকে তাকাও, মাথা আস্তে আস্তে ঘোরাও — ২৫টা ছবি সেভ হবে
`known_faces/Rahim_1.jpg … Rahim_25.jpg` হিসেবে। তারপর:
```powershell
python app.py
```
এবার ক্যামেরার সামনে এলে নাম ধরে ডাকবে + আগের অর্ডার মনে করিয়ে জিজ্ঞেস করবে,
যেমন *"Fahad, তোমাকে পিজ্জা গ্লোবে স্বাগতম! আগেরবার তুমি Pepperoni অর্ডার
করেছিলে। সেটাই আবার অর্ডার করবে, নাকি নতুন কিছু করবে জানাও। ধন্যবাদ।"*
(max once per `--cooldown` seconds)। আগের অর্ডার না থাকলে শুধু জিজ্ঞেস করবে
আজ কী অর্ডার করবে। অচেনা কেউ এলে *"আপনাকে পিজ্জা গ্লোবে স্বাগতম! আপনার
অর্ডার কী হবে আমাদের জানান।"*

**Way 2 — Photo file:** একটা পরিষ্কার সামনের ছবি `known_faces/Rahim.jpg`
নামে রাখো।

> Face engine: `face-recognition` (dlib) থাকলে সেটা (tolerance 0.5),
> না থাকলে built-in **OpenCV LBPH** — Visual Studio লাগে না।
> Voice: default English voice-এ Latin-বাংলা বলে (নির্ভরযোগ্য)। espeak-ng-এর
> Windows বাংলা voice ভাঙা (যেকোনো বাক্যকে ~১ সেকেন্ডের বিড়বিড় বানায়),
> তাই `--voice bn` শুধু তখনই দাও যখন তোমার espeak ঠিকমতো বাংলা বলে। New faces are
> auto-saved to `customer_photos/` and inserted into SQLite with `is_synced = 0`.

### Controls (video window focused)

| Key | Action |
|-----|--------|
| `o` | Take an order for the last-seen customer (terminal prompt) |
| `s` | Force a sync attempt now |
| `q` | Quit |

## 3. How offline-first sync works

1. Every insert uses `is_synced = 0`.
2. `SyncWorker` thread wakes every `--sync-interval` seconds, checks connectivity
   (TCP to 8.8.8.8:53), and `POST`s pending rows to `--sync-url`.
3. On HTTP 2xx → `UPDATE … SET is_synced = 1`. On failure/offline → rows stay
   local, retried next round. The kiosk never crashes offline.

The default `--sync-url` is `https://httpbin.org/post` (mock echo endpoint).
Point it at your real central API when ready — payloads are
`{"type": "customer"|"order", …}` JSON.

## 4. Troubleshooting

- **No webcam / `Cannot open webcam`**: run `python app.py --no-camera`.
- **No audio**: TTS failures fall back to log/print; check OS voices (SAPI5 /
  NSSpeech / eSpeak).
- **Camera busy**: close other apps using it, or try `--camera 1`.
