FROM python:3.11-slim AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    VIRTUAL_ENV=/opt/venv \
    PATH="/opt/venv/bin:$PATH"

WORKDIR /app

RUN python -m venv "$VIRTUAL_ENV"

COPY pyproject.toml README.md ./
COPY src ./src

RUN pip install --upgrade pip \
    && pip install --no-cache-dir .


FROM python:3.11-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    VIRTUAL_ENV=/opt/venv \
    PATH="/opt/venv/bin:$PATH" \
    PYTHONPATH=/app/src

WORKDIR /app

RUN groupadd --system app \
    && useradd --system --gid app --create-home --home-dir /home/app app \
    && chown -R app:app /app

COPY --from=builder /opt/venv /opt/venv
COPY --chown=app:app src ./src

USER app

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import sys, urllib.request; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=3).status == 200 else 1)"

ENTRYPOINT ["uvicorn", "seo_indexing_tracker.main:app"]
CMD ["--host", "0.0.0.0", "--port", "8000"]
