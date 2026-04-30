"""Cerberus entry point. Starts uvicorn on 0.0.0.0:8000."""
import socket
from pathlib import Path

import uvicorn
from dotenv import load_dotenv

# Load .env before importing the app so vision/GSC clients can read keys at import time.
load_dotenv(Path(__file__).parent / ".env", override=False)


def _print_banner(port: int) -> None:
    hostname = socket.gethostname()
    try:
        local_ip = socket.gethostbyname(hostname)
    except socket.gaierror:
        local_ip = "127.0.0.1"
    print()
    print("  Cerberus — SEO/GEO Pre-Flight Checker")
    print(f"  Local:    http://127.0.0.1:{port}")
    print(f"  Network:  http://{local_ip}:{port}   (Tailscale-reachable if VM is on tailnet)")
    print()


if __name__ == "__main__":
    PORT = 8000
    _print_banner(PORT)
    uvicorn.run("cerberus.app:app", host="0.0.0.0", port=PORT, log_level="info")
