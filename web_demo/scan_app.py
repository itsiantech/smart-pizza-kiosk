"""Pizza Globe — Web Kiosk Demo, photo-scan edition (no WebRTC).

For networks/hosts where live video (UDP) is blocked: the visitor takes a
photo with their camera, the server recognizes the face and the browser
speaks the greeting. Plain HTTP — works everywhere Streamlit runs.

Deploy: main file = web_demo/scan_app.py
Local:  streamlit run web_demo/scan_app.py
"""

import time
import uuid

import cv2
import numpy as np
import streamlit as st
from PIL import Image

from kiosk_lib import (CONN, KNOWN_DIR, MENU, NEW_MSG, RECO, SNAP_DIR, _db_lock,
                       add_customer, add_order, get_customer, last_order,
                       returning_msg, speak_js)

st.set_page_config(page_title="Pizza Globe — Kiosk Demo", page_icon="🍕")
st.title("🍕 Pizza Globe — Kiosk Demo")
st.caption("Take a photo with your camera — the kiosk recognizes you, "
           "greets you by name, and takes your order.")

st.info(f"Recognizer: OpenCV LBPH — **{len(RECO.n2l)}** known people. "
        f"Photos are processed on the server, nothing is uploaded elsewhere.")

sound = st.toggle("🔊 Sound (browser voice)", value=False,
                  help="Browsers block speech until you interact — turn this on.")
if "last_seen" not in st.session_state:
    st.session_state["last_seen"] = (None, None)

col_scan, col_side = st.columns(2)

with col_scan:
    st.subheader("📸 Scan face")
    photo = st.camera_input("Show your face and take a photo", key="scan")
    if photo:
        img = cv2.cvtColor(np.array(Image.open(photo).convert("RGB")),
                           cv2.COLOR_RGB2BGR)
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        faces = RECO._detect(gray)
        if not faces:
            st.warning("No face found — move closer to the camera and retry.")
        else:
            x, y, w, h = max(faces, key=lambda b: b[2] * b[3])
            crop = gray[y:y + h, x:x + w]
            name = RECO.identify(crop)
            cv2.rectangle(img, (x, y), (x + w, y + h),
                          (0, 200, 0) if name else (0, 0, 255), 2)
            st.image(cv2.cvtColor(img, cv2.COLOR_BGR2RGB),
                     caption=name or "New Customer")
            if name:
                row = get_customer(name)
                st.session_state["last_seen"] = (name, row["id"] if row else None)
                prev = last_order(row["id"]) if row else None
                msg = returning_msg(name, prev["order_items"] if prev else None)
                st.success(f"🔔 {msg}")
                if sound:
                    speak_js(msg)
            else:
                guest = f"Guest_{uuid.uuid4().hex[:6].upper()}"
                pic = SNAP_DIR / f"{guest}.jpg"
                cv2.imwrite(str(pic), img)
                cid = add_customer(guest, str(pic))
                RECO.learn(guest, crop)
                st.session_state["last_seen"] = (guest, cid)
                st.success(f"🔔 {NEW_MSG}")
                if sound:
                    speak_js(NEW_MSG)

with col_side:
    st.subheader("📝 Enroll (new person)")
    st.caption("Upload 3–5 clear front photos, then scan to be greeted by name.")
    ename = st.text_input("Name", placeholder="e.g. Fahad", key="en_name")
    files = st.file_uploader("Face photos", type=("jpg", "jpeg", "png"),
                             accept_multiple_files=True, key="en_files")
    if st.button("Save enrollment"):
        if ename.strip() and files:
            n0 = len(list(KNOWN_DIR.glob(f"{ename.strip()}_*.jpg")))
            for i, f in enumerate(files, start=n0 + 1):
                data = np.frombuffer(f.read(), np.uint8)
                gray = cv2.imdecode(data, cv2.IMREAD_GRAYSCALE)
                if gray is None:
                    continue
                found = RECO._detect(gray)
                crop = gray
                if found:
                    x, y, w, h = max(found, key=lambda b: b[2] * b[3])
                    crop = gray[y:y + h, x:x + w]
                cv2.imwrite(str(KNOWN_DIR / f"{ename.strip()}_{i}.jpg"),
                            cv2.resize(crop, (200, 200)))
            n, people = RECO.load()
            st.success(f"Enrolled **{ename.strip()}**! "
                       f"({n} samples, {people} people) Now scan your face.")
        else:
            st.warning("Enter a name AND upload at least one photo.")

    st.subheader("🧾 Order")
    who = st.session_state["last_seen"]
    st.write(f"Last seen: **{who[0] or 'scan first'}**")
    picks = [item for item, _ in MENU.items()
             if st.checkbox(f"{item} — ${MENU[item]:.2f}", key=f"s_{item}")]
    if st.button("Place order"):
        if picks and who[0]:
            total = round(sum(MENU[i] for i in picks), 2)
            oid = add_order(who[1], ", ".join(picks), total)
            ok = f"Order #{oid}: {', '.join(picks)} = ${total:.2f}"
            st.success(ok)
            if sound:
                speak_js(f"Order confirmed. {', '.join(picks)}. "
                         f"Total {total} dollars.")
        else:
            st.warning("Scan a face and tick at least one item first.")

    if st.button("🔄 Rescan / next customer"):
        st.session_state["last_seen"] = (None, None)
        st.rerun()

st.divider()
st.subheader("📋 Recent activity")
with _db_lock:
    custs = CONN.execute(
        "SELECT * FROM customers ORDER BY id DESC LIMIT 5").fetchall()
    ords = CONN.execute(
        "SELECT o.id, c.name, o.order_items, o.total_bill "
        "FROM orders o LEFT JOIN customers c ON o.customer_id=c.id "
        "ORDER BY o.id DESC LIMIT 5").fetchall()
st.write("**Customers:**", [dict(r)["name"] for r in custs] or "—")
st.table([dict(r) for r in ords] or [{"orders": "none yet"}])
st.caption(f"Server time: {time.strftime('%H:%M:%S')}")
