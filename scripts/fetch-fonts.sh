#!/bin/sh
# Vendor the three UI fonts into frontend/public/fonts/.
#
# Nightwire is LAN-only, so it never loads Google Fonts at runtime. This runs
# at image build time (see the Dockerfile's frontend stage) and can be run by
# hand for local development. It is the only step in the build that needs
# internet access.
#
# NOTE: these upstream URLs were written without network access to verify
# them, so treat a 404 here as expected maintenance rather than a bug. The UI
# degrades to system fonts if a file is missing, and tokens.css accepts either
# .woff2 or .ttf for each family, so a hand-downloaded file with the right name
# works without touching any code.
set -eu

DEST="${1:-frontend/public/fonts}"
mkdir -p "$DEST"

GF="https://raw.githubusercontent.com/google/fonts/main/ofl"
INTER="https://raw.githubusercontent.com/rsms/inter/v4.1/docs/font-files"
MONO="https://raw.githubusercontent.com/JetBrains/JetBrainsMono/v2.304/fonts/webfonts"

missing=0

fetch() {
    url="$1"
    out="$2"
    if [ -f "$DEST/$out" ]; then
        echo "have    $out"
        return 0
    fi
    if curl -fsSL --retry 3 -o "$DEST/$out.part" "$url"; then
        mv "$DEST/$out.part" "$DEST/$out"
        echo "fetched $out"
        return 0
    fi
    rm -f "$DEST/$out.part"
    echo "MISSING $out  <- $url" >&2
    missing=1
    return 0
}

# Chakra Petch is distributed as TrueType in google/fonts.
fetch "$GF/chakrapetch/ChakraPetch-SemiBold.ttf" chakra-petch-600.ttf
fetch "$GF/chakrapetch/ChakraPetch-Bold.ttf" chakra-petch-700.ttf

# Inter and JetBrains Mono ship web-ready woff2.
fetch "$INTER/Inter-Regular.woff2" inter-400.woff2
fetch "$INTER/Inter-Medium.woff2" inter-500.woff2
fetch "$INTER/Inter-SemiBold.woff2" inter-600.woff2
fetch "$MONO/JetBrainsMono-Regular.woff2" jetbrains-mono-400.woff2

count=$(find "$DEST" -maxdepth 1 \( -name '*.woff2' -o -name '*.ttf' \) | wc -l | tr -d ' ')
echo "fonts present in $DEST: $count of 6"

if [ "$missing" = "1" ]; then
    cat >&2 <<'WARN'

Some fonts were not fetched. This does not fail the build: every family in
frontend/src/lib/tokens.css declares a system fallback, so the UI renders
either way, and only headings and timestamps will look off.

To fix by hand, download the files into frontend/public/fonts/ using the
filenames in its README.md. Either .woff2 or .ttf is accepted.
WARN
fi
