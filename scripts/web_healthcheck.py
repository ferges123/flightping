import os
import sys
import urllib.request

host = os.getenv("FPB_WEB_HOST", "127.0.0.1")
port = int(os.getenv("FPB_WEB_PORT", "8080"))
try:
    request = urllib.request.Request(f"http://{host}:{port}/")
    if token := os.getenv("FPB_WEB_AUTH_TOKEN", "").strip():
        request.add_header("Authorization", f"Bearer {token}")
    urllib.request.urlopen(request, timeout=10)
except Exception as exc:
    print(f"web panel health check failed on {host}:{port}: {exc}", file=sys.stderr)
    sys.exit(1)
