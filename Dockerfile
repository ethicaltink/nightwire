# Nightwire: one image carrying the app, ffmpeg and go2rtc.
# ffmpeg and go2rtc are both installed here: the recorder spawns ffmpeg per
# camera, and go2rtc is supervised as a child process for live video.

# --- 1. Build the frontend -------------------------------------------------
FROM node:22-alpine AS frontend
WORKDIR /build
COPY frontend/package.json frontend/package-lock.json* ./
RUN npm install
COPY frontend/ ./

# Bundle the three fonts into the image. forbids loading
# Google Fonts at runtime, because the app is LAN-only. A font that cannot be
# fetched is a warning, not a build failure: tokens.css falls back to system
# families.
COPY scripts/fetch-fonts.sh scripts/fetch-go2rtc-assets.sh /build/scripts/
RUN apk add --no-cache curl \
    && sh /build/scripts/fetch-fonts.sh /build/public/fonts \
    && sh /build/scripts/fetch-go2rtc-assets.sh /build/public \
    && rm -rf /build/scripts

RUN npm run build


# --- 2. go2rtc binary ------------------------------------------------------
# Pinned. Bump deliberately, never with :latest.
# Keep this tag and GO2RTC_TAG in scripts/fetch-go2rtc-assets.sh in step.
FROM alexxit/go2rtc:1.9.14 AS go2rtc


# --- 3. Runtime ------------------------------------------------------------
FROM python:3.12-slim AS runtime

# HOME and XDG_CACHE_HOME point at a writable directory because the app runs
# as `nvr` after the entrypoint drops privileges, while HOME would otherwise
# still be root's. zeep caches parsed WSDL documents under the cache
# directory, so with root's HOME **every ONVIF call fails** with
# "[Errno 13] Permission denied: '/root/.cache'". Found 2026-10-30 by
# probing a camera from the UI, before credentials were even in play.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    HOME=/tmp/nvr \
    XDG_CACHE_HOME=/tmp/nvr/cache \
    NVR_PORT=8080 \
    NVR_CONFIG_DIR=/config \
    NVR_RECORDINGS_DIR=/recordings \
    NVR_STATIC_DIR=/app/static

# util-linux carries setpriv, which the entrypoint uses to drop privileges.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ffmpeg \
        tini \
        util-linux \
    && rm -rf /var/lib/apt/lists/*

RUN groupadd -g 1000 nvr && useradd -u 1000 -g nvr -d /app -s /sbin/nologin nvr

WORKDIR /app

# The licence travels with the image, because an image is a copy and MIT
# requires the notice to be included in one. THIRD-PARTY.md goes with it: the
# bundled FFmpeg is GPL, and a recipient should be able to see that from the
# image itself rather than having to find the repository.
COPY LICENSE THIRD-PARTY.md pyproject.toml ./
COPY src/ ./src/
# The sources are only needed to build the wheel; the installed package in
# site-packages is what runs. Dropping them afterwards removes two further
# copies of the same code from the image, including setuptools' `build/`
# residue, so there is one copy and nothing that can drift from it.
RUN pip install . && rm -rf /app/build /app/src

COPY --from=frontend /build/dist/ /app/static/
COPY --from=go2rtc /usr/local/bin/go2rtc /usr/local/bin/go2rtc
COPY healthcheck.py entrypoint.sh ./
RUN chmod +x /app/entrypoint.sh /app/healthcheck.py

VOLUME ["/config", "/recordings"]
EXPOSE 8080

# Stdlib only: this image has no curl, by design.
HEALTHCHECK --interval=30s --timeout=10s --start-period=20s --retries=3 \
    CMD ["python", "/app/healthcheck.py"]

ENTRYPOINT ["tini", "--", "/app/entrypoint.sh"]
