"""Start this application's loopback server without a console window."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
import webbrowser

APP = Path(__file__).resolve().parent
ROOT = APP.parent


def read_health(url: str) -> dict | None:
    try:
        # Never use the user's proxy for a loopback application.
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(url.rstrip("/") + "/api/health", timeout=1.5) as response:
            value = json.load(response)
        return value if isinstance(value, dict) else None
    except (OSError, ValueError, urllib.error.URLError):
        return None


def same_app(health: dict | None, database: Path) -> bool:
    if not health or health.get("app") != "shoe-replenishment":
        return False
    existing = health.get("database")
    return bool(existing and Path(existing).resolve() == database.resolve())


def available_port(first: int) -> int:
    for port in range(first, first + 15):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as candidate:
            try:
                candidate.bind(("127.0.0.1", port))
            except OSError:
                continue
            return port
    raise RuntimeError("本地端口暂被占用，请关闭旧窗口后重试。")


def main() -> int:
    parser = argparse.ArgumentParser(description="打开鞋款补货软件")
    parser.add_argument("--no-browser", action="store_true", help="只启动服务，不打开浏览器")
    parser.add_argument("--port", type=int, default=18765)
    parser.add_argument("--db", type=Path, default=ROOT / "data" / "replenishment.sqlite3")
    parser.add_argument("--runtime-file", type=Path, default=ROOT / "data" / "runtime.json")
    args = parser.parse_args()
    database = args.db.resolve()
    runtime_path = args.runtime_file.resolve()
    database.parent.mkdir(parents=True, exist_ok=True)
    runtime_path.parent.mkdir(parents=True, exist_ok=True)

    url = None
    try:
        runtime = json.loads(runtime_path.read_text(encoding="utf-8"))
        previous_url = runtime.get("url", "")
        if previous_url.startswith("http://127.0.0.1:") and same_app(read_health(previous_url), database):
            url = previous_url
    except (OSError, ValueError):
        pass

    # Recover a running server even when runtime.json was removed or stale.
    if url is None:
        candidate = f"http://127.0.0.1:{args.port}"
        if same_app(read_health(candidate), database):
            url = candidate
    if url is None:
        port = available_port(args.port)
        url = f"http://127.0.0.1:{port}"
        log_path = runtime_path.parent / "server.log"
        command = [
            sys.executable, str(APP / "server.py"), "serve",
            "--port", str(port), "--db", str(database),
            "--runtime-file", str(runtime_path),
        ]
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
        with log_path.open("ab") as log:
            child = subprocess.Popen(
                command, cwd=str(ROOT), stdin=subprocess.DEVNULL, stdout=log,
                stderr=subprocess.STDOUT, creationflags=flags,
                start_new_session=os.name != "nt",
            )
        for _ in range(60):
            if same_app(read_health(url), database):
                break
            if child.poll() is not None:
                raise RuntimeError(f"启动没有成功，请查看 {log_path}")
            time.sleep(0.2)
        else:
            raise RuntimeError(f"启动超时，请查看 {log_path}")
    print(url)
    if not args.no_browser:
        webbrowser.open(url, new=2)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, RuntimeError) as error:
        print(f"无法打开补货软件：{error}", file=sys.stderr)
        raise SystemExit(1)
