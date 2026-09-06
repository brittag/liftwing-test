"""Wiki replica connection helper — local SSH tunnel or in-cluster Toolforge."""

from __future__ import annotations

import configparser
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

ANALYTICS_HOST = "enwiki.analytics.db.svc.wikimedia.cloud"
ANALYTICS_PORT = 3306


def _replica_cnf_paths() -> list[Path]:
    paths: list[Path] = []
    tool_data = os.environ.get("TOOL_DATA_DIR")
    if tool_data:
        paths.append(Path(tool_data) / "replica.my.cnf")
    home = os.environ.get("HOME")
    if home:
        paths.append(Path(home) / "replica.my.cnf")
    return paths


def _read_replica_cnf() -> tuple[str | None, str | None]:
    parser = configparser.ConfigParser()
    for path in _replica_cnf_paths():
        if not path.is_file():
            continue
        parser.read(path)
        if not parser.has_section("client"):
            continue
        user = parser.get("client", "user", fallback=None)
        password = parser.get("client", "password", fallback=None)
        if user and password:
            return user.strip().strip("'\""), password.strip().strip("'\"")
    return None, None


def _on_toolforge() -> bool:
    """True when running in a Toolforge webservice or job (not a local laptop)."""
    return bool(
        os.environ.get("TOOL_REPLICA_USER")
        or os.environ.get("TOOL_DATA_DIR")
        or os.environ.get("TOOL_TOOLSDB_USER")
    )


def get_db_connection(db_name: str = "enwiki_p"):
    """Connect to a wiki replica.

    On Toolforge: analytics replica + TOOL_REPLICA_* or replica.my.cnf.
    Locally: SSH tunnel on 127.0.0.1 (TOOLFORGE_DB_PORT, default 3307).
    """
    import pymysql

    if _on_toolforge():
        host = os.environ.get("TOOLFORGE_REPLICA_HOST", ANALYTICS_HOST)
        port = int(os.environ.get("TOOLFORGE_REPLICA_PORT", str(ANALYTICS_PORT)))
        user = os.environ.get("TOOL_REPLICA_USER")
        password = os.environ.get("TOOL_REPLICA_PASSWORD")
        if not user or not password:
            user, password = _read_replica_cnf()
        if not user or not password:
            raise RuntimeError(
                "On Toolforge but missing replica credentials. "
                "Expected TOOL_REPLICA_USER / TOOL_REPLICA_PASSWORD "
                "or replica.my.cnf under $TOOL_DATA_DIR or $HOME."
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
                f"{port}:{ANALYTICS_HOST}:{ANALYTICS_PORT}",
                f"{tf_user}@login.toolforge.org",
                "-N",
            ]
            subprocess.Popen(
                cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
            )
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
