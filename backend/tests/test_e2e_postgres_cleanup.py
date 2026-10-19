"""Fault injection for the page-test PostgreSQL teardown.

These tests do not start Docker. They inject the compose result and the
server-stop signal that the teardown already uses.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

import e2e_postgres  # noqa: E402


def _closed_ports(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BETTER_AGENT_E2E_PORT", "39991")
    monkeypatch.setenv("BETTER_AGENT_E2E_SCRIPTED_PORT", "39992")


def test_cleanup_failure_fails_the_command(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setenv("BETTER_AGENT_E2E_PG_ROOT", str(tmp_path))
    _closed_ports(monkeypatch)
    (tmp_path / "project").write_text("better-agent-e2e-pg-injected", encoding="utf-8")

    def compose(project: str, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        assert args[:2] == ("down", "-v")
        return subprocess.CompletedProcess(args, 1, "", "injected docker failure")

    monkeypatch.setattr(e2e_postgres, "_compose", compose)

    with pytest.raises(SystemExit) as raised:
        e2e_postgres.down()

    assert raised.value.code == 1
    error = capsys.readouterr().err
    assert "isolated PostgreSQL cleanup failed (docker compose down exit 1)" in error
    assert "injected docker failure" in error


def test_container_is_removed_only_after_servers_stop(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BETTER_AGENT_E2E_PG_ROOT", str(tmp_path))
    _closed_ports(monkeypatch)
    (tmp_path / "project").write_text("better-agent-e2e-pg-order", encoding="utf-8")
    stopped = {"value": False}
    events: list[str] = []

    def request(port: str) -> None:
        events.append(f"shutdown:{port}")
        if {item.removeprefix("shutdown:") for item in events} == {"39991", "39992"}:
            stopped["value"] = True

    def servers_stopped() -> bool:
        return stopped["value"]

    def compose(project: str, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        assert stopped["value"] is True
        events.append(f"compose:{args[0]}")
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(e2e_postgres, "_request_shutdown", request)
    monkeypatch.setattr(e2e_postgres, "_page_servers_stopped", servers_stopped)
    monkeypatch.setattr(e2e_postgres, "_compose", compose)

    e2e_postgres.down()

    assert events == ["shutdown:39991", "shutdown:39992", "compose:down"]


def test_running_server_blocks_container_removal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setenv("BETTER_AGENT_E2E_PG_ROOT", str(tmp_path))
    _closed_ports(monkeypatch)
    monkeypatch.setattr(e2e_postgres, "STOP_TIMEOUT_SECONDS", 0.2)
    (tmp_path / "project").write_text("better-agent-e2e-pg-live", encoding="utf-8")
    (tmp_path / "plain.pid").write_text(str(os.getpid()), encoding="utf-8")

    def compose(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        raise AssertionError("docker compose down ran while a page server was still running")

    monkeypatch.setattr(e2e_postgres, "_request_shutdown", lambda port: None)
    monkeypatch.setattr(e2e_postgres, "_compose", compose)

    with pytest.raises(SystemExit) as raised:
        e2e_postgres.down()

    assert raised.value.code == 1
    assert "page servers did not stop before PostgreSQL cleanup" in capsys.readouterr().err


def test_interrupted_stop_still_removes_the_container(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setenv("BETTER_AGENT_E2E_PG_ROOT", str(tmp_path))
    (tmp_path / "project").write_text("better-agent-e2e-pg-interrupt", encoding="utf-8")
    events: list[str] = []

    def stop() -> None:
        raise KeyboardInterrupt

    def compose(project: str, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        events.append(args[0])
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(e2e_postgres, "_stop_page_servers", stop)
    monkeypatch.setattr(e2e_postgres, "_compose", compose)

    with pytest.raises(KeyboardInterrupt):
        e2e_postgres.down()

    assert events == ["down"]
    assert "page server stop was interrupted" in capsys.readouterr().err


def test_startup_cleanup_failure_keeps_the_original_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    def compose(project: str, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        if args[0] == "up":
            raise subprocess.CalledProcessError(1, "docker compose up", stderr="docker up failed")
        assert args[0] == "down"
        return subprocess.CompletedProcess(args, 1, "", "injected cleanup failure")

    monkeypatch.setattr(e2e_postgres, "_compose", compose)

    with pytest.raises(subprocess.CalledProcessError) as raised:
        e2e_postgres._start(tmp_path)

    assert "docker up failed" in (raised.value.stderr or "")
    error = capsys.readouterr().err
    assert "best-effort PostgreSQL cleanup failed (docker compose down exit 1)" in error
    assert "injected cleanup failure" in error
