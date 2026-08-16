# ─────────────────────────────────────────────────────────────────────────────
# Dockerfile
#
# Builds the Prime Capital backend as a container.
# Updated for Render deployment (was previously written for Azure App Service).
#
# ── Build and run locally ─────────────────────────────────────────────────────
#   docker build -t pcil-backend .
#   docker run -p 8000:8000 --env-file .env pcil-backend
# ─────────────────────────────────────────────────────────────────────────────

FROM python:3.11-slim

WORKDIR /app

# ── System dependencies ───────────────────────────────────────────────────────
# libpq-dev + build-essential: needed to build psycopg2 and some wheels.
# The rest are runtime libraries for PyMuPDF and Pillow, which handle the
# uploaded images and PDF rasterising in the KYC export. Without these the
# build succeeds but PDF generation fails at runtime with a missing .so error.
RUN apt-get update && apt-get install -y --no-install-recommends \
    libpq-dev \
    build-essential \
    libjpeg-dev \
    zlib1g-dev \
    libfreetype6-dev \
    && rm -rf /var/lib/apt/lists/*

# ── Python dependencies ───────────────────────────────────────────────────────
# Copied first so Docker caches this layer when only app code changes.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# ── Application code ──────────────────────────────────────────────────────────
COPY . .

# The app writes uploaded files here when STORAGE_BACKEND=local. Created so the
# first upload does not fail on a missing directory.
#
# IMPORTANT: on Render this directory is wiped on every deploy and restart.
# Set STORAGE_BACKEND=supabase in production, or client KYC documents will
# disappear after each release.
RUN mkdir -p app/static/kyc-documents app/static/avatars

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV APP_ENV=production

# Render supplies the port at runtime via $PORT. Hardcoding 8000 means the
# platform cannot route traffic to the container.
ENV PORT=8000
EXPOSE 8000

# ── Startup ───────────────────────────────────────────────────────────────────
# Migrations run before the server starts. Alembic skips already-applied
# revisions, so this is safe on every deploy. Without it, a release that adds
# a column leaves the app broken until someone runs it by hand.
#
# Two workers rather than four: Render's free and starter instances have
# limited memory, and each worker loads the whole app.
CMD alembic upgrade head && \
    gunicorn app.main:app \
      --workers 1 \
      --worker-class uvicorn.workers.UvicornWorker \
      --bind 0.0.0.0:$PORT \
      --proxy-headers \
      --forwarded-allow-ips="*" \
      --timeout 120 \
      --access-logfile - \
      --error-logfile -
