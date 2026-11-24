FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    LEDGER_DB=/data/ledger.sqlite3 \
    LEDGER_HOST=0.0.0.0 \
    LEDGER_PORT=8000

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY ledger ./ledger
COPY templates ./templates
COPY static ./static
COPY wsgi.py ./

RUN useradd --uid 10001 --create-home --shell /usr/sbin/nologin ledger \
    && mkdir -p /data \
    && chown -R ledger:ledger /data /app
USER ledger

EXPOSE 8000
VOLUME ["/data"]

HEALTHCHECK --interval=15s --timeout=3s --start-period=10s --retries=5 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=2).status == 200 else 1)"

CMD ["gunicorn", "--bind", "0.0.0.0:8000", "--workers", "1", "--threads", "8", \
     "--timeout", "60", "--access-logfile", "-", "wsgi:app"]
