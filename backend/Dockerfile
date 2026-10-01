FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    PATH=/app/.venv/bin:$PATH

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
       ca-certificates ffmpeg tesseract-ocr tesseract-ocr-eng tesseract-ocr-chi-sim libgomp1 \
    && rm -rf /var/lib/apt/lists/* \
    && python -m pip install --no-cache-dir uv==0.11.19 \
    && groupadd --system --gid 10001 dovideo \
    && useradd --system --uid 10001 --gid dovideo --home-dir /app dovideo

WORKDIR /app

COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY app ./app
COPY migrations ./migrations
COPY alembic.ini ./

# The official RocketMQ Python SDK writes logs under ~/logs/rocketmq_python.
RUN mkdir -p /app/logs && chown dovideo:dovideo /app/logs

USER dovideo
EXPOSE 9090

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "9090"]
