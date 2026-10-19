"""Start the isolated PostgreSQL used by the seven page scenarios.

Reuses the integration compose file, the database-target guard, and
``alembic upgrade head``. A missing or unsafe database stops the run.
There is no SQLite fallback.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import psycopg
from psycopg import sql


ROOT = Path(__file__).resolve().parents[1]
BACKEND_ROOT = ROOT / "backend"
COMPOSE_FILE = BACKEND_ROOT / "tests" / "integration" / "compose.postgres.yaml"
sys.path.insert(0, str(BACKEND_ROOT / "tests" / "integration"))

from db_target_guard import UnsafeDatabaseTarget, assert_isolated_test_database  # noqa: E402


ROLES = {
    "plain": "better_agent_e2e_plain_test",
    "scripted": "better_agent_e2e_scripted_test",
}
STOP_TIMEOUT_SECONDS = 30


def require_isolated_postgres() -> None:
    """Point this page server at its own migrated test database."""
    if os.environ.get("BETTER_AGENT_TEST_ALLOW_SQLITE") == "1":
        raise SystemExit("page e2e does not fall back to SQLite")
    os.environ.pop("BETTER_AGENT_TEST_ALLOW_SQLITE", None)
    os.environ["DATABASE_URL"] = ""
    if os.environ.get("BETTER_AGENT_E2E_POSTGRES") != "1":
        raise SystemExit("page e2e requires isolated PostgreSQL")
    role = os.environ.get("BETTER_AGENT_E2E_ROLE", "")
    try:
        url = database_url_for_role(role)
    except (UnsafeDatabaseTarget, FileNotFoundError, subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError) as exc:
        raise SystemExit(f"isolated PostgreSQL is unavailable: {exc}") from exc
    if not url.startswith(("postgresql://", "postgres://")):
        raise SystemExit("page e2e refused a non-PostgreSQL database")
    assert_isolated_test_database(url)
    os.environ["DATABASE_URL"] = url
    print(f"page e2e database {_redact(url)}", flush=True)


def database_url_for_role(role: str) -> str:
    if role not in ROLES:
        raise SystemExit("BETTER_AGENT_E2E_ROLE must be plain or scripted")
    root = _pg_root()
    with _file_lock(root / "lock"):
        state_path = root / "state.json"
        if state_path.is_file():
            state = json.loads(state_path.read_text(encoding="utf-8"))
        else:
            state = _start(root)
            temporary = state_path.with_suffix(".json.tmp")
            temporary.write_text(json.dumps(state), encoding="utf-8")
            temporary.replace(state_path)
    url = state["urls"][role]
    assert_isolated_test_database(url)
    return url


def down() -> None:
    """Stop the page servers, then remove their compose project.

    The servers exit through the existing process lifespan, which stops
    workers and closes the connection pool. The container is removed only
    after that. A failed ``docker compose down`` fails this command.
    An interruption during the wait still attempts removal and stays a failure.
    """
    interrupted = False
    try:
        _stop_page_servers()
    except KeyboardInterrupt:
        interrupted = True
        print(
            "page server stop was interrupted; the database container will still be removed",
            file=sys.stderr,
            flush=True,
        )
    root = _pg_root()
    project_file = root / "project"
    project = project_file.read_text(encoding="utf-8").strip() if project_file.is_file() else ""
    if project:
        _require_compose_down(project)
    if interrupted:
        raise KeyboardInterrupt


def _pg_root() -> Path:
    raw = os.environ.get("BETTER_AGENT_E2E_PG_ROOT", "").strip()
    if not raw:
        raise SystemExit("BETTER_AGENT_E2E_PG_ROOT is required")
    root = Path(raw)
    root.mkdir(parents=True, exist_ok=True)
    return root


def _start(root: Path) -> dict[str, object]:
    project_file = root / "project"
    if not project_file.is_file():
        project_file.write_text(f"better-agent-e2e-pg-{uuid.uuid4().hex[:8]}", encoding="utf-8")
    project = project_file.read_text(encoding="utf-8").strip()
    try:
        _compose(project, "up", "-d", "--wait", "postgres")
        published = _compose(project, "port", "postgres", "5432").stdout.strip()
        match = re.search(r":(\d+)$", published)
        if match is None:
            raise SystemExit(f"could not parse PostgreSQL port from {published!r}")
        port = match.group(1)
        admin = f"postgresql://better_agent_test:better_agent_test@127.0.0.1:{port}/better_agent_test"
        assert_isolated_test_database(admin)
        urls: dict[str, str] = {}
        with psycopg.connect(admin, autocommit=True) as connection:
            for name in ROLES.values():
                exists = connection.execute(
                    "SELECT 1 FROM pg_database WHERE datname = %s",
                    (name,),
                ).fetchone()
                if exists is None:
                    connection.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
        for role, name in ROLES.items():
            url = f"postgresql://better_agent_test:better_agent_test@127.0.0.1:{port}/{name}"
            assert_isolated_test_database(url)
            _migrate(url)
            urls[role] = url
        return {"project": project, "urls": urls}
    except BaseException:
        _best_effort_compose_down(project)
        raise


def _migrate(database_url: str) -> None:
    assert_isolated_test_database(database_url)
    environment = os.environ.copy()
    environment["DATABASE_URL"] = database_url
    environment.pop("BETTER_AGENT_TEST_ALLOW_SQLITE", None)
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "-c", str(BACKEND_ROOT / "alembic.ini"), "upgrade", "head"],
        cwd=BACKEND_ROOT,
        env=environment,
        check=False,
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        timeout=60,
    )
    if result.returncode:
        detail = _redact_text(result.stderr or result.stdout, environment)
        raise SystemExit("Isolated PostgreSQL migration failed:\n" + detail[-5000:])


def _stop_page_servers() -> None:
    """Ask both page servers to finish their lifespan, and wait until they do."""
    for _role, port in _page_ports():
        _request_shutdown(port)
    deadline = time.monotonic() + STOP_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        if _page_servers_stopped():
            return
        time.sleep(0.1)
    pending = ", ".join(f"{role} at 127.0.0.1:{port}" for role, port in _page_ports())
    print(
        "page servers did not stop before PostgreSQL cleanup: " + pending,
        file=sys.stderr,
        flush=True,
    )
    raise SystemExit(1)


def _request_shutdown(port: str) -> None:
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}/api/e2e/shutdown",
        data=b"{}",
        method="POST",
        headers={"content-type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            response.read()
    except urllib.error.URLError:
        return


def _page_servers_stopped() -> bool:
    """True only after each published server process has left its lifespan."""
    root_raw = os.environ.get("BETTER_AGENT_E2E_PG_ROOT", "").strip()
    root = Path(root_raw) if root_raw else None
    for role, port in _page_ports():
        if root is not None:
            pid_path = root / f"{role}.pid"
            if pid_path.is_file():
                text = pid_path.read_text(encoding="utf-8").strip()
                if text.isdigit() and _pid_alive(int(text)):
                    return False
        if _health_open(port):
            return False
    return True


def _page_ports() -> tuple[tuple[str, str], tuple[str, str]]:
    return (
        ("plain", os.environ.get("BETTER_AGENT_E2E_PORT", "61129")),
        ("scripted", os.environ.get("BETTER_AGENT_E2E_SCRIPTED_PORT", "61130")),
    )


def _health_open(port: str) -> bool:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/health", timeout=1) as response:
            return response.status < 500
    except urllib.error.URLError:
        return False
    except TimeoutError:
        return True


def _pid_alive(pid: int) -> bool:
    """Return whether a process exists without signaling it.

    ``os.kill(pid, 0)`` on Windows can deliver a console control event to every
    process sharing the console, including the server that is trying to finish
    its lifespan. Query the process handle instead.
    """
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes

        handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)
        if not handle:
            return False
        ctypes.windll.kernel32.CloseHandle(handle)
        return True
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _require_compose_down(project: str) -> None:
    try:
        result = _compose(project, "down", "-v", check=False)
    except subprocess.TimeoutExpired as exc:
        print(
            "isolated PostgreSQL cleanup timed out waiting for docker compose down: " + str(exc),
            file=sys.stderr,
            flush=True,
        )
        raise SystemExit(1) from exc
    if result.returncode == 0:
        return
    _fail_cleanup(result, best_effort=False)


def _best_effort_compose_down(project: str) -> None:
    """Remove a container after startup failed, without hiding that failure."""
    try:
        result = _compose(project, "down", "-v", check=False)
    except (subprocess.SubprocessError, OSError) as exc:
        print(f"best-effort PostgreSQL cleanup failed: {exc}", file=sys.stderr, flush=True)
        return
    if result.returncode == 0:
        return
    _fail_cleanup(result, best_effort=True)


def _fail_cleanup(result: subprocess.CompletedProcess[str], *, best_effort: bool) -> None:
    detail = _redact_text((result.stderr or result.stdout or "").strip(), os.environ)
    prefix = "best-effort PostgreSQL cleanup failed" if best_effort else "isolated PostgreSQL cleanup failed"
    message = f"{prefix} (docker compose down exit {result.returncode})"
    if detail:
        message = f"{message}: {detail}"
    print(message, file=sys.stderr, flush=True)
    if not best_effort:
        raise SystemExit(1)


def _compose(project: str, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["docker", "compose", "-f", str(COMPOSE_FILE), "-p", project, *args],
        check=check,
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        timeout=90,
    )


def _redact(database_url: str) -> str:
    parsed = urlsplit(database_url)
    hostname = parsed.hostname or ""
    port = f":{parsed.port}" if parsed.port else ""
    username = f"{parsed.username}:***@" if parsed.username else ""
    return urlunsplit((parsed.scheme, f"{username}{hostname}{port}", parsed.path, "", ""))


def _redact_text(detail: str, environment: dict[str, str]) -> str:
    redacted = detail
    for key, value in environment.items():
        if value and ("KEY" in key or "SECRET" in key or "TOKEN" in key or "PASSWORD" in key or "DATABASE_URL" in key):
            redacted = redacted.replace(value, "<redacted>")
    return redacted


@contextmanager
def _file_lock(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = open(path, "a+b")
    locked = False
    try:
        if handle.seek(0, os.SEEK_END) == 0:
            handle.write(b"\0")
            handle.flush()
        handle.seek(0)
        if os.name == "nt":
            import msvcrt

            deadline = time.monotonic() + 180
            while True:
                try:
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                    locked = True
                    break
                except OSError:
                    if time.monotonic() >= deadline:
                        raise TimeoutError("timed out waiting for the isolated PostgreSQL lock")
                    time.sleep(0.25)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            locked = True
        yield
    finally:
        if locked and os.name == "nt":
            import msvcrt

            handle.seek(0)
            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            except OSError:
                pass
        elif locked:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()


if __name__ == "__main__":
    if len(sys.argv) == 2 and sys.argv[1] == "down":
        down()
    else:
        raise SystemExit("usage: python scripts/e2e_postgres.py down")
