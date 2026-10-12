"""Pizza Globe — Web Live Demo, live-video edition (Streamlit + WebRTC).

Needs a network that allows WebRTC/UDP. If the video stays black with
"Connection is taking longer than expected", use scan_app.py instead
(photo-scan over plain HTTP — works on any network).

Deploy: main file = web_demo/streamlit_app.py (or scan_app.py).
"""

import queue
import time

import av
import cv2
import streamlit as st
from streamlit_webrtc import RTCConfiguration, VideoProcessorBase, WebRtcMode, webrtc_streamer

from kiosk_lib import (KNOWN_DIR, MENU, NEW_MSG, RECO, add_customer, add_order,
                       get_customer, last_order, returning_msg, speak_js)

RTC_CONFIG = RTCConfiguration(
    {"iceServers": [
        {"urls": ["stun:stun.l.google.com:19302",
                  "stun:stun1.l.google.com:19302"]},
        # Free test TURN (Open Relay) — lets video connect through strict
        # NATs/firewalls where pure STUN fails. Replace with your own TURN
        # (e.g. coturn / Twilio / Metered) for production.
        {"urls": ["turn:openrelay.metered.ca:80",
                  "turn:openrelay.metered.ca:443",
                  "turn:openrelay.metered.ca:443?transport=tcp"],
         "username": "openrelayproject",
         "credential": "openrelayproject"},
    ]})

COOLDOWN_SEC = 15
UNKNOWN_COOLDOWN_SEC = 30


# ------------------------------------------------------- video processor ---
class KioskProcessor(VideoProcessorBase):
    def __init__(self):
        self.outbox: queue.Queue = queue.Queue()  # greeting texts for UI thread
        self.last_seen = (None, None)
        self.last_greeted: dict = {}
        self.last_unknown = 0.0
        self.frame_n = 0
        self.enroll_name: str | None = None
        self.enroll_left = 0
        self.enroll_saved = 0

    def recv(self, frame: av.VideoFrame) -> av.VideoFrame:
        img = frame.to_ndarray(format="bgr24")
        self.frame_n += 1
        if self.frame_n % 2 == 0:
            return av.VideoFrame.from_ndarray(img, format="bgr24")
        small = cv2.resize(img, (0, 0), fx=0.5, fy=0.5)
        gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)
        now = time.time()
        for (x, y, w, h) in RECO._detect(gray):
            x1, y1, x2, y2 = x * 2, y * 2, (x + w) * 2, (y + h) * 2
            crop = gray[y:y + h, x:x + w]
            name = RECO.identify(crop) if crop.size else None

            # -- enrollment mode: save crops, no greeting --
            if self.enroll_name and self.enroll_left > 0 and crop.size:
                n = len(list(KNOWN_DIR.glob(f"{self.enroll_name}_*.jpg"))) + 1
                cv2.imwrite(str(KNOWN_DIR / f"{self.enroll_name}_{n}.jpg"),
                            cv2.resize(crop, (200, 200)))
                self.enroll_left -= 1
                self.enroll_saved += 1
                cv2.rectangle(img, (x1, y1), (x2, y2), (255, 165, 0), 2)
                cv2.putText(img, f"Enrolling {self.enroll_saved}", (x1, y1 - 10),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.7, (255, 165, 0), 2)
                break

            if name:
                color, label = (0, 200, 0), name
                row = get_customer(name)
                self.last_seen = (name, row["id"] if row else None)
                if now - self.last_greeted.get(name, 0) >= COOLDOWN_SEC:
                    prev = last_order(row["id"]) if row else None
                    self.outbox.put(returning_msg(
                        name, prev["order_items"] if prev else None))
                    self.last_greeted[name] = now
            else:
                color, label = (0, 0, 255), "New Customer"
                if now - self.last_unknown >= UNKNOWN_COOLDOWN_SEC and crop.size:
                    self.last_unknown = now
                    import uuid as _uuid
                    guest = f"Guest_{_uuid.uuid4().hex[:6].upper()}"
                    from kiosk_lib import SNAP_DIR
                    photo = SNAP_DIR / f"{guest}.jpg"
                    cv2.imwrite(str(photo), img)
                    cid = add_customer(guest, str(photo))
                    RECO.learn(guest, crop)
                    self.last_seen = (guest, cid)
                    self.outbox.put(NEW_MSG)
            cv2.rectangle(img, (x1, y1), (x2, y2), color, 2)
            cv2.putText(img, label, (x1, y1 - 10),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.7, color, 2)
        return av.VideoFrame.from_ndarray(img, format="bgr24")


# ------------------------------------------------------------------- ui ---
st.set_page_config(page_title="Pizza Globe — Live Kiosk Demo", page_icon="🍕")
st.title("🍕 Pizza Globe — Live Kiosk Demo")
st.caption("Camera stays in your browser. Allow camera access when asked. "
           "Use Chrome/Edge over HTTPS (or localhost).")

st.info(f"Recognizer: OpenCV LBPH — {len(RECO.n2l)} known people loaded. "
        f"Faces detected live in the video below.")

sound = st.toggle("🔊 Sound (browser voice)", value=False,
                  help="Browsers block speech until you interact — turn this on.")
if not sound:
    st.warning("Turn **🔊 Sound** on to hear greetings.")

col_cam, col_side = st.columns([3, 2])
with col_cam:
    ctx = webrtc_streamer(
        key="kiosk", mode=WebRtcMode.SENDRECV,
        rtc_configuration=RTC_CONFIG,
        video_processor_factory=KioskProcessor,
        media_stream_constraints={"video": True, "audio": False},
        async_processing=True,
    )

with col_side:
    st.subheader("📝 Enroll a face")
    ename = st.text_input("Your name", placeholder="e.g. Fahad")
    if st.button("Start enrollment (15 shots)"):
        if ctx.video_processor and ename.strip():
            ctx.video_processor.enroll_name = ename.strip()
            ctx.video_processor.enroll_left = 15
            ctx.video_processor.enroll_saved = 0
            st.session_state["enrolling"] = ename.strip()
    if st.session_state.get("enrolling") and ctx.video_processor:
        p = ctx.video_processor
        st.progress(min(p.enroll_saved / 15, 1.0),
                    f"Saved {p.enroll_saved}/15 — move your head slowly")
        if p.enroll_left <= 0 and p.enroll_saved > 0:
            RECO.load()
            st.success(f"Enrolled {p.enroll_name}! Show your face to be greeted.")
            st.session_state["enrolling"] = None
            p.enroll_name = None

    st.subheader("🧾 Order for last seen")
    who = ctx.video_processor.last_seen if ctx.video_processor else (None, None)
    st.write(f"Last seen: **{who[0] or '—'}**")
    picks = [item for item, _ in MENU.items() if st.checkbox(item, key=f"m_{item}")]
    if st.button("Place order") and picks and who[0]:
        total = round(sum(MENU[i] for i in picks), 2)
        oid = add_order(who[1], ", ".join(picks), total)
        st.success(f"Order #{oid}: {', '.join(picks)} = ${total:.2f}")
        if sound:
            speak_js(f"Order confirmed. {', '.join(picks)}. Total {total} dollars.")

# -- greetings: poll processor outbox, speak in browser, show text --
if ctx.state.playing and ctx.video_processor:
    try:
        msg = ctx.video_processor.outbox.get_nowait()
        st.success(f"🔔 {msg}")
        if sound:
            speak_js(msg)
    except queue.Empty:
        pass
    time.sleep(1.5)
    st.rerun()

st.divider()
st.subheader("📋 Recent activity")
from kiosk_lib import CONN, _db_lock
with _db_lock:
    custs = CONN.execute(
        "SELECT * FROM customers ORDER BY id DESC LIMIT 5").fetchall()
    ords = CONN.execute(
        "SELECT o.id, c.name, o.order_items, o.total_bill "
        "FROM orders o LEFT JOIN customers c ON o.customer_id=c.id "
        "ORDER BY o.id DESC LIMIT 5").fetchall()
st.write("**Customers:**", [dict(r)["name"] for r in custs] or "—")
st.table([dict(r) for r in ords] or [{"orders": "none yet"}])
