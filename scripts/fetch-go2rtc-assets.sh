#!/bin/sh
# Vendor go2rtc's player into frontend/public/.
#
# The live tiles use go2rtc's own `<video-stream>` web component, which does
# WebRTC with an automatic MSE fallback. It is one file. Nightwire is LAN-only
# and fetches nothing at runtime, so it is bundled at image build time next to
# the fonts.
#
# NOTE: the URL and the pinned tag below were written without network access
# to verify them. A 404 here is ordinary maintenance, not a bug: the build
# carries on and the tiles say "Player not loaded" instead of showing video.
set -eu

DEST="${1:-frontend/public}"
TAG="${GO2RTC_TAG:-v1.9.14}"
URL="https://raw.githubusercontent.com/AlexxIT/go2rtc/${TAG}/www/video-rtc.js"

mkdir -p "$DEST"

if [ -f "$DEST/video-rtc.js" ]; then
    echo "have    video-rtc.js"
    exit 0
fi

if curl -fsSL --retry 3 -o "$DEST/video-rtc.js.part" "$URL"; then
    mv "$DEST/video-rtc.js.part" "$DEST/video-rtc.js"
    echo "fetched video-rtc.js from $TAG"
    exit 0
fi

rm -f "$DEST/video-rtc.js.part"
cat >&2 <<WARN
MISSING video-rtc.js  <- $URL

Live video will not play: the tiles will read "Player not loaded". Recording,
playback and everything else are unaffected.

To fix by hand, save that file (or the equivalent from the go2rtc release you
pinned in the Dockerfile) to $DEST/video-rtc.js.
WARN
exit 0
