FROM python:3.12-slim

# Arabic fonts + complex text layout for designed post images
RUN apt-get update \
 && apt-get install -y --no-install-recommends fonts-noto-core libraqm0 ca-certificates \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY web ./web

ENV SAHM_DATA_DIR=/data \
    PYTHONUNBUFFERED=1 \
    PORT=8000
RUN useradd --create-home sahm && mkdir -p /data && chown sahm /data
USER sahm
VOLUME ["/data"]
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s CMD python -c "import urllib.request,os;urllib.request.urlopen(f'http://127.0.0.1:{os.environ.get(\"PORT\",\"8000\")}/healthz')"

CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT} --proxy-headers --forwarded-allow-ips='*'"]
