#!/usr/bin/env python3
"""Launch script for the HSR ADB Framework.

Starts the sidecar server as a subprocess and streams its output.

Usage
-----
    python3 launch.py                    # Start sidecar only
    python3 launch.py --config sidecar.toml
"""

from __future__ import annotations

import argparse
import logging
import signal
import subprocess
import sys
import time


logger = logging.getLogger("launch")


def start_sidecar(config: str = "sidecar.toml") -> subprocess.Popen:
    """Start the sidecar server as a subprocess."""
    logger.info("Starting sidecar...")
    proc = subprocess.Popen(
        [sys.executable, "-m", "server", "--config", config],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )

    # Stream sidecar output in background thread
    def stream_output():
        for line in proc.stdout:
            print(f"[sidecar] {line.rstrip()}")

    import threading
    t = threading.Thread(target=stream_output, daemon=True)
    t.start()

    # Wait for sidecar to be ready
    from sidecar_client import SidecarClient
    client = SidecarClient(host="127.0.0.1", port=8765)
    for i in range(30):
        try:
            health = client.get_health()
            if health.ok:
                logger.info("Sidecar ready (uptime: %.1fs)", health.uptime_s)
                return proc
        except Exception:
            time.sleep(1)
    logger.warning("Sidecar may not be ready yet")
    return proc


def main() -> int:
    parser = argparse.ArgumentParser(
        description="HSR ADB Framework - Launch Script",
        prog="launch",
    )
    parser.add_argument("--config", default="sidecar.toml", help="Sidecar config file")

    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")

    # Start sidecar
    sidecar_proc = start_sidecar(args.config)

    try:
        print("\nSidecar running. Press Ctrl+C to stop.\n")
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\nShutting down...")
    finally:
        logger.info("Terminating sidecar...")
        sidecar_proc.terminate()
        try:
            sidecar_proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            sidecar_proc.kill()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
