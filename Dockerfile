# syntax=docker/dockerfile:1
#
# qr-mail container. Defaults to the Gmail fetch daemon (scripts/gmail_fetch.py);
# override the command to run the backfill, pain_to_qr, or the Postfix processor.
#
#   docker build -t qr-mail .
#   docker run --rm \
#     -e GMAIL_OAUTH_TOKEN_FILE=/secrets/token.json \
#     -e GMAIL_IMPERSONATE_ADDRESS=me@example.com \
#     -e MY_ADDRESS=qr@example.com \
#     -e ALLOWED_SENDERS='*@trusted.com' \
#     -v /host/secrets:/secrets \
#     qr-mail
#
# One-shot pain.001 → QR images:
#   docker run --rm -v "$PWD:/data" qr-mail \
#     python scripts/pain_to_qr.py /data/batch.xml -o /data/out --format both
FROM python:3.13-slim-bookworm

# Native libraries the Python deps shell out to or wrap:
#   libzbar0         -> pyzbar (QR decode)
#   poppler-utils    -> pdf2image / pdfinfo (PDF render + encryption check)
#   tesseract-ocr    -> pytesseract (OCR)
#   fonts-liberation -> Slovene diacritics (š/č/ž) on generated UPN slips
RUN apt-get update && apt-get install -y --no-install-recommends \
        libzbar0 \
        poppler-utils \
        tesseract-ocr \
        fonts-liberation \
    && rm -rf /var/lib/apt/lists/*

# Pinned uv for reproducible, lockfile-driven dependency installation.
COPY --from=ghcr.io/astral-sh/uv:0.11.26 /uv /uvx /bin/

WORKDIR /app

# Install dependencies first so this layer caches across source changes.
# package=false in pyproject means uv only builds the environment, not the app.
ENV UV_PYTHON_DOWNLOADS=0 \
    UV_PROJECT_ENVIRONMENT=/app/.venv
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --python /usr/local/bin/python3.13

# Application code and the runtime data files referenced by default code paths.
COPY core ./core
COPY parsers ./parsers
COPY scripts ./scripts
COPY upn_base_empty.jpg slo-intermediates.pem ./

ENV PATH="/app/.venv/bin:${PATH}" \
    PYTHONPATH=/app \
    PYTHONUNBUFFERED=1 \
    QR_MAIL_MONO_FONT_REGULAR=/usr/share/fonts/truetype/liberation/LiberationMono-Regular.ttf \
    QR_MAIL_MONO_FONT_BOLD=/usr/share/fonts/truetype/liberation/LiberationMono-Bold.ttf

# Run as an unprivileged user.
RUN useradd --system --uid 10001 qrmail && chown -R qrmail /app
USER qrmail

CMD ["python", "scripts/gmail_fetch.py"]
