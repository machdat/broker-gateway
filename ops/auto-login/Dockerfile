# Auto-Login-Sidecar fuer den Paper-Stack (Karte ece90a8e Phase B).
#
# Ein-Container-Image, das von broker-gateway-paper bei Bedarf via
# `docker run --rm` gestartet wird. Laeuft NICHT dauerhaft.
#
# Build (auf cma-pi-1, ARM64):
#   docker build --platform linux/arm64 \
#     -t broker-gateway-paper-auto-login:1.29.0 \
#     -f ops/auto-login/Dockerfile ops/auto-login/
#
# Aufruf (vom Trigger im Hauptcontainer):
#   docker run --rm --network broker-gateway-paper_default \
#     -e BG_PAPER_USERNAME=... -e BG_PAPER_PASSWORD=... \
#     -e BG_AUTO_LOGIN_TARGET_URL=http://broker-gateway-paper-cpgateway:5000/ \
#     broker-gateway-paper-auto-login:1.29.0

FROM mcr.microsoft.com/playwright/python:v1.49.1-noble
# Microsoft-Image bringt Chromium + System-Libs vorinstalliert mit;
# spart 200MB+ Build-Time-Aufwand fuer Chromium-Dependencies und
# unterstuetzt ARM64 (linux/arm64) seit playwright 1.40.

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY auto_login.py auto_login_logic.py ./

# Defensive: laeuft als nicht-root, damit ein versehentlicher RCE im
# JS-Bundle nicht direkt root im Container gibt.
RUN useradd -r -u 10001 -g root -s /usr/sbin/nologin sidecar && \
    chown -R sidecar:root /app

USER sidecar

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

ENTRYPOINT ["python", "auto_login.py"]
