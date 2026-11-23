FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    HOST=0.0.0.0 \
    PORT=8765 \
    DATA_DIR=/data \
    HEADLESS=1

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
 && useradd --create-home --uid 1000 desk \
 && mkdir -p /data && chown desk:desk /data
COPY config.py notifier.py level2.py yahoo_feed.py momentum_scanner.py replay.py ./
COPY web ./web

USER desk
VOLUME ["/data"]
EXPOSE 8765
HEALTHCHECK --interval=60s --timeout=10s --start-period=60s --retries=3 \
  CMD python -c "import os,urllib.request;urllib.request.urlopen('http://127.0.0.1:%s/healthz' % os.environ.get('PORT','8765'),timeout=8)"

CMD ["python", "momentum_scanner.py"]
