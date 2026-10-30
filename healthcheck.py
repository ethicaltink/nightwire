#!/usr/bin/env python3
"""Container healthcheck. Stdlib only: the slim image does not ship curl."""

import os
import sys
import urllib.request

port = os.environ.get("NVR_PORT", "8080")
url = f"http://127.0.0.1:{port}/api/health"

try:
    with urllib.request.urlopen(url, timeout=5) as response:
        sys.exit(0 if response.status == 200 else 1)
except Exception as exc:  # noqa: BLE001 - any failure means unhealthy
    print(f"healthcheck failed: {exc}", file=sys.stderr)
    sys.exit(1)
