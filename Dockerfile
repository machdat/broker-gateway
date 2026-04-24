FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY pyproject.toml /app/
COPY src/ /app/src/

RUN pip install -e .

EXPOSE 8000

CMD ["uvicorn", "broker_gateway.main:app", "--host", "0.0.0.0", "--port", "8000"]
