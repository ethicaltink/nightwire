#!/bin/sh
# Nightwire container entrypoint.
#
# tini alone does nothing with PUID/PGID, so this script does the work:
# start as root, reconcile the nvr user's uid/gid with what the host wants,
# fix ownership on the two volumes, then drop privileges before exec'ing
# uvicorn. Nothing in the app ever runs as root.
set -eu

PUID="${PUID:-1000}"
PGID="${PGID:-1000}"
NVR_PORT="${NVR_PORT:-8080}"
CONFIG_DIR="${NVR_CONFIG_DIR:-/config}"
RECORDINGS_DIR="${NVR_RECORDINGS_DIR:-/recordings}"

# No --proxy-headers: Nightwire is served directly on the LAN with no reverse
# proxy in front of it. With that flag, uvicorn would take the client address
# from an X-Forwarded-For header, which any client can set, and the login
# throttle keys on that address - so one attacker could rotate a header value
# and never be rate-limited. Add it only together with a trusted proxy.
run_app() {
    exec uvicorn nightwire.main:app \
        --host 0.0.0.0 \
        --port "$NVR_PORT" \
        --no-server-header
}

# Already unprivileged (someone set `user:` in compose). Run as-is.
if [ "$(id -u)" != "0" ]; then
    echo "entrypoint: running as uid $(id -u), skipping PUID/PGID handling"
    run_app
fi

current_gid="$(getent group nvr | cut -d: -f3)"
current_uid="$(id -u nvr)"

if [ "$current_gid" != "$PGID" ]; then
    echo "entrypoint: setting group nvr gid $current_gid -> $PGID"
    groupmod -o -g "$PGID" nvr
fi
if [ "$current_uid" != "$PUID" ]; then
    echo "entrypoint: setting user nvr uid $current_uid -> $PUID"
    usermod -o -u "$PUID" nvr
fi

# /tmp/nvr is HOME and the cache root for the dropped user as well as the
# segment-list directory, so the cache subdirectory has to exist and be owned
# by them before uvicorn starts.
mkdir -p "$CONFIG_DIR" "$RECORDINGS_DIR" /tmp/nvr /tmp/nvr/cache

# Shallow chown on the recordings volume: a 45-day archive can hold millions
# of files and a recursive chown on every boot would take minutes. Ownership
# of existing footage is only wrong if PUID changed, which is rare and which
# the README tells you to fix by hand.
chown "$PUID:$PGID" "$RECORDINGS_DIR" /tmp/nvr /tmp/nvr/cache
chown -R "$PUID:$PGID" "$CONFIG_DIR"

echo "entrypoint: dropping to nvr ($PUID:$PGID), listening on $NVR_PORT"
exec setpriv --reuid "$PUID" --regid "$PGID" --init-groups --inh-caps=-all \
    uvicorn nightwire.main:app \
    --host 0.0.0.0 \
    --port "$NVR_PORT" \
    --no-server-header
