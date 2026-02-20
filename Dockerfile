FROM python:3.12-slim

WORKDIR /app

COPY pyproject.toml README.md LICENSE /app/
COPY src /app/src

RUN pip install --no-cache-dir .

ENV LOG_LEVEL=INFO

ENTRYPOINT ["codex-telegram-bot"]
