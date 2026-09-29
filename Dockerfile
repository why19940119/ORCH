# ORCH v0.21.0 (WP-ORCH-12) - single-container deployment.
# Secrets are never baked in: OPENROUTER_API_KEY comes from the environment
# (.env via docker compose); the session key is generated on first start and
# kept in the state volume (state/secret_key, 0600).
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    ORCH_HOST=0.0.0.0 \
    ORCH_PORT=5050 \
    ORCH_PERSIST_SECRET_KEY=1

WORKDIR /app

RUN groupadd --system --gid 10001 orch \
    && useradd --system --uid 10001 --gid orch --home-dir /app --shell /usr/sbin/nologin orch

COPY requirements.txt ./
RUN pip install -r requirements.txt

COPY --chown=orch:orch . .

# Volume mount points (owned by the app user so a fresh named volume is writable).
RUN mkdir -p state uploads data/import artifacts output \
    && chown -R orch:orch state uploads data artifacts output \
    && rm -f .env

USER orch

EXPOSE 5050
VOLUME ["/app/state", "/app/uploads", "/app/data", "/app/artifacts"]

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import os,sys,urllib.request; r=urllib.request.urlopen('http://127.0.0.1:%s/healthz' % os.environ.get('ORCH_PORT','5050'), timeout=4); sys.exit(0 if r.status==200 else 1)"

CMD ["python", "serve.py"]
