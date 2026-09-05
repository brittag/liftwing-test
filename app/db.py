"""Toolforge replica connection helper for local SSH tunnel."""

from __future__ import annotations

import os
import socket
import subprocess
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

# Load .env from repo root (parent of app/) or cwd.
_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(_ROOT / ".env")
load_dotenv()


def get_db_connection(db_name: str = "enwiki_p"):
    """Connect via local SSH tunnel. Raises if tunnel/creds missing."""
    import pymysql

    host = "127.0.0.1"
    port = int(os.getenv("TOOLFORGE_DB_PORT", "3307"))
    user = os.getenv("TOOLFORGE_SQL_USER")
    password = os.getenv("TOOLFORGE_SQL_PASSWORD")
    if not user or not password:
        raise RuntimeError(
            "Missing TOOLFORGE_SQL_USER / TOOLFORGE_SQL_PASSWORD. "
            "Copy .env.example to .env and fill in replica credentials."
        )

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(1)
        if s.connect_ex((host, port)) != 0:
            tf_user = os.getenv("TOOLFORGE_USER")
            if not tf_user:
                raise RuntimeError(
                    f"No tunnel on {host}:{port} and TOOLFORGE_USER unset. "
                    "Run the SSH tunnel first (see README)."
                )
            cmd = [
                "ssh",
                "-L",
                f"{port}:enwiki.analytics.db.svc.wikimedia.cloud:3306",
                f"{tf_user}@login.toolforge.org",
                "-N",
            ]
            subprocess.Popen(
                cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
            )
            # Brief wait for tunnel
            import time

            for _ in range(20):
                time.sleep(0.5)
                with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s2:
                    s2.settimeout(1)
                    if s2.connect_ex((host, port)) == 0:
                        break
            else:
                raise RuntimeError(
                    f"Could not open SSH tunnel to Toolforge on port {port}."
                )

    return pymysql.connect(
        host=host,
        port=port,
        user=user,
        password=password,
        database=db_name,
        cursorclass=pymysql.cursors.DictCursor,
        charset="utf8mb4",
        connect_timeout=30,
        read_timeout=600,
    )


def decode_title(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (bytes, bytearray, memoryview)):
        return bytes(value).decode("utf-8")
    return str(value)
