# Multi-Stage: docker-CLI aus dem offiziellen Image kopieren, damit der
# Auto-Login-Trigger (Karte ece90a8e Phase B) `docker run --rm <sidecar>`
# als Subprocess aufrufen kann. Im Live-Stack ist der docker.sock-Mount
# nicht vorhanden (Hard-Guard 3) — das CLI-Binary liegt zwar im Image,
# laeuft dort aber ins Leere und wird vom AutoLoginTrigger sowieso nicht
# aufgerufen (Hard-Guard 1 + Trigger-Skip bei stack_kind=live).
FROM docker:27-cli AS docker-cli

FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

COPY --from=docker-cli /usr/local/bin/docker /usr/local/bin/docker

WORKDIR /app

COPY pyproject.toml /app/
COPY src/ /app/src/

RUN pip install -e .

EXPOSE 8000

CMD ["uvicorn", "broker_gateway.main:app", "--host", "0.0.0.0", "--port", "8000"]
