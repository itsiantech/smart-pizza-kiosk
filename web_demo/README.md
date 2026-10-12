# Pizza Globe — Web Live Demo

Browser version of the kiosk for showing clients online.

## Local run

```powershell
pip install -r web_demo/requirements.txt
streamlit run web_demo/streamlit_app.py
```

Open http://localhost:8501 → **Start** → allow camera → turn **🔊 Sound** on.

Flow: enroll your face (15 shots) → show your face → browser greets you by
name, mentioning your last order → tick menu items → Place order.

## Put it online (free, ~5 min)

1. Push this repo to GitHub (public).
2. Go to https://share.streamlit.io → **New app** →
   - Repository: your repo
   - Main file path: `web_demo/streamlit_app.py`
3. **Deploy** → you get a public `https://*.streamlit.app` link. Send it to
   the client — they open it, allow camera, and try the kiosk themselves.

Notes:
- Webcam needs **HTTPS** (Streamlit Cloud gives it) or localhost.
- Turn 🔊 Sound on (browsers block speech before any click).
- Voice = visitor's own browser/OS voice (Bengali voice used automatically
  if their device has one, e.g. Android/Chrome `Google বাংলা`).
- Demo data (SQLite) lives on the server; redeploys wipe it — fine for demo.
- Alternative host: Hugging Face Spaces (Streamlit SDK) with the same file.
