from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable, Iterable

from .job_store import JobStore, utc_now


@dataclass(frozen=True)
class PreviewSession:
    url: str
    port: int
    pid: int
    status: str
    started_at: str


def _port_available(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        try:
            sock.bind(("127.0.0.1", port))
        except OSError:
            return False
    return True


def _http_health(url: str) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=1) as response:
            payload = json.loads(response.read().decode("utf-8"))
            return response.status == 200 and payload.get("live") is True
    except (OSError, ValueError, urllib.error.URLError):
        return False


def _wait_until(
    predicate: Callable[[], bool], *, timeout_seconds: float
) -> bool:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.2)
    return False


class PreviewRuntime:
    def __init__(
        self,
        store: JobStore,
        *,
        popen: Callable[..., Any] = subprocess.Popen,
        health_check: Callable[[str], bool] | None = None,
    ) -> None:
        self.store = store
        self.popen = popen
        self.health_check = health_check or _http_health
        self.last_command: list[str] = []
        self.start_count = 0

    def health(self, session: PreviewSession) -> bool:
        return self.health_check(session.url + "/api/config")

    def start(
        self,
        job_id: str,
        project_path: Path,
        *,
        ports: Iterable[int] = range(5050, 5061),
    ) -> PreviewSession:
        existing = (self.store.get_job(job_id) or {}).get("preview")
        if existing:
            existing_session = PreviewSession(**existing)
            if self.health(existing_session):
                return existing_session
        script = (
            Path(__file__).resolve().parents[1]
            / "skills"
            / "ppt-master"
            / "scripts"
            / "svg_editor"
            / "server.py"
        )
        attempted_ports: list[int] = []
        for port in ports:
            if not _port_available(port):
                continue
            attempted_ports.append(port)
            command = [
                sys.executable,
                str(script),
                str(project_path),
                "--port",
                str(port),
                "--live",
                "--timeout",
                "0",
            ]
            self.last_command = command
            self.start_count += 1
            creationflags = (
                subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
            )
            process = self.popen(
                command,
                cwd=str(project_path),
                creationflags=creationflags,
            )
            url = f"http://127.0.0.1:{port}"
            if _wait_until(
                lambda: self.health_check(url + "/api/config"),
                timeout_seconds=10,
            ):
                session = PreviewSession(
                    url=url,
                    port=port,
                    pid=process.pid,
                    status="healthy",
                    started_at=utc_now(),
                )
                self.store.save_preview(job_id, asdict(session))
                return session
            if process.poll() is None:
                process.terminate()
        tried = ", ".join(str(port) for port in attempted_ports) or "none"
        raise RuntimeError(
            f"unable to start live preview on ports 5050-5060 (attempted: {tried})"
        )
