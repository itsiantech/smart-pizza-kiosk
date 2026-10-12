# Hugging Face Spaces (Docker SDK) fallback host.
# Streamlit Cloud ignores this file. For HF: create a Space with Docker SDK
# and push this repo to it — serves web_demo/streamlit_app.py on port 7860.
FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg libgl1 libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY web_demo/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .

EXPOSE 7860
CMD ["streamlit", "run", "web_demo/streamlit_app.py", \
     "--server.port=7860", "--server.address=0.0.0.0", \
     "--server.headless=true"]
