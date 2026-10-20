"""Per-delivery OS execution locks for notification sends.

The lock file is an exclusive kernel lock on a local directory. It is not a
database session and it is not deleted or replaced. A dead process loses the
lock because the operating system closes its handles. A database disconnect
does not.
"""
from __future__ import annotations

import json
import os
import re
import stat
import uuid
from pathlib import Path

TOKEN_RE = re.compile(r"^[0-9a-f]{32}$")
SCOPE_ENV = "BETTER_NOTIFICATION_SCOPE_DIR"
_NETWORK_FS = {
    "nfs", "nfs4", "cifs", "smb", "smbfs", "smb3", "fuse.sshfs", "ceph", "glusterfs",
    "afs", "9p", "ncpfs",
}
_LIVE_CLAIMS: dict[tuple[str, str], "ExecutionGuard"] = {}


class NotificationExecutionUnavailable(RuntimeError):
    """The local execution scope cannot be trusted, so no send or recovery proceeds."""


class NotificationLockRefused(Exception):
    """This token cannot be locked. The delivery stays in place and is reported."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class ExecutionGuard:
    """One exclusive hold of a delivery token. Closing it does not delete the file."""

    def __init__(self, fd: int, token: str, scope_id: str, *, claim: bool) -> None:
        self._fd = fd
        self.token = token
        self.scope_id = scope_id
        self.claim = claim
        self.file_dev = ""
        self.file_ino = ""
        self.result_stored = False
        self._released = False

    @property
    def held(self) -> bool:
        return not self._released and self._fd >= 0

    @property
    def can_store_result(self) -> bool:
        return self.claim and self.held and not self.result_stored

    def release(self) -> None:
        if self._released:
            return
        self._released = True
        _LIVE_CLAIMS.pop((self.scope_id, self.token), None)
        fd, self._fd = self._fd, -1
        try:
            if os.name == "nt":
                import msvcrt
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


class NotificationExecutionScope:
    def __init__(self, directory: Path, scope_id: str) -> None:
        self.directory = directory
        self.scope_id = scope_id
        self.lock_dir = directory / "locks"

    def acquire(self, token: str) -> ExecutionGuard:
        guard = self._lock(token, blocking=True, claim=True)
        if guard is None:
            raise NotificationExecutionUnavailable("notification execution lock was not acquired")
        return guard

    def try_acquire(self, token: str, *, file_dev: str, file_ino: str) -> ExecutionGuard | None:
        if not TOKEN_RE.fullmatch(token):
            return None
        if not file_dev or not file_ino:
            raise NotificationLockRefused("missing-file-identity")
        current = _LIVE_CLAIMS.get((self.scope_id, token))
        if current is not None and current.held:
            return None
        return self._lock(
            token, blocking=False, claim=False, expected=(str(file_dev), str(file_ino)),
        )

    def release_claim(self, guard: ExecutionGuard) -> None:
        guard.release()

    def _lock(
        self, token: str, *, blocking: bool, claim: bool, expected: tuple[str, str] | None = None,
    ) -> ExecutionGuard | None:
        if not TOKEN_RE.fullmatch(token):
            raise NotificationExecutionUnavailable("notification executor token is not usable")
        self._confirm_binding()
        path = self.lock_dir / token
        flags = os.O_RDWR
        if hasattr(os, "O_BINARY"):
            flags |= os.O_BINARY
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        if claim:
            flags |= os.O_CREAT | os.O_EXCL
            try:
                fd = os.open(path, flags, 0o600)
            except FileExistsError as exc:
                raise NotificationExecutionUnavailable("notification token file already exists") from exc
        else:
            fd = _open_existing_token(path)
            if fd is None:
                return None
        try:
            if claim:
                os.write(fd, b"\0")
            _assert_same_file(fd, path)
            if expected is not None:
                _assert_expected_file(fd, path, expected)
            _lock_fd(fd, blocking=blocking)
            _assert_same_file(fd, path)
            self._confirm_binding()
            if expected is not None:
                _assert_expected_file(fd, path, expected)
            identity = _identity_text(os.fstat(fd))
        except BlockingIOError:
            os.close(fd)
            return None
        except Exception:
            os.close(fd)
            raise
        guard = ExecutionGuard(fd, token, self.scope_id, claim=claim)
        guard.file_dev, guard.file_ino = identity
        if claim:
            _LIVE_CLAIMS[(self.scope_id, token)] = guard
        return guard

    def _confirm_binding(self) -> None:
        try:
            _assert_local_directory(self.directory)
            identity = self.directory / "scope-identity.json"
            if self.directory.is_symlink() or identity.is_symlink() or self.lock_dir.is_symlink():
                raise NotificationLockRefused("untrusted-scope")
            if not self.lock_dir.is_dir():
                raise NotificationLockRefused("untrusted-scope")
            if _read_scope_id(self.directory) != self.scope_id:
                raise NotificationLockRefused("untrusted-scope")
        except NotificationExecutionUnavailable as exc:
            raise NotificationLockRefused("untrusted-scope") from exc


def open_notification_scope(path: str | Path | None = None) -> NotificationExecutionScope:
    if os.name not in {"nt", "posix"}:
        raise NotificationExecutionUnavailable("notification execution locks are not supported on this system")
    selected = Path(path).expanduser() if path is not None else _configured_directory().expanduser()
    if selected.is_symlink():
        raise NotificationExecutionUnavailable("notification scope cannot be a symlink")
    directory = selected.resolve()
    _assert_local_directory(directory)
    identity = directory / "scope-identity.json"
    if identity.exists():
        scope_id = _read_scope_id(directory)
    else:
        directory.mkdir(parents=True, exist_ok=True)
        _tighten(directory)
        locks = directory / "locks"
        locks.mkdir(mode=0o700, exist_ok=True)
        _tighten(locks)
        scope_id = _create_scope_id(directory)
    return NotificationExecutionScope(directory, scope_id)


def _configured_directory() -> Path:
    configured = os.environ.get(SCOPE_ENV)
    if configured:
        return Path(configured)
    if os.name == "nt":
        root = os.environ.get("LOCALAPPDATA")
        if not root:
            raise NotificationExecutionUnavailable("LOCALAPPDATA is required for the notification execution scope")
        return Path(root) / "better-agent" / "notification-execution"
    state = os.environ.get("XDG_STATE_HOME")
    base = Path(state) if state else Path.home() / ".local" / "state"
    return base / "better-agent" / "notification-execution"


def _machine_id() -> str:
    if os.name == "nt":
        import winreg
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Cryptography") as key:
            value, _kind = winreg.QueryValueEx(key, "MachineGuid")
        text = str(value or "").strip()
        if not text:
            raise NotificationExecutionUnavailable("machine identity is missing")
        return text
    for candidate in (Path("/etc/machine-id"), Path("/var/lib/dbus/machine-id")):
        if candidate.is_file():
            text = candidate.read_text(encoding="utf-8").strip()
            if text:
                return text
    raise NotificationExecutionUnavailable("machine identity is missing")


def _create_scope_id(directory: Path) -> str:
    locks = directory / "locks"
    directory_dev, directory_ino = _identity_text(directory.lstat())
    locks_dev, locks_ino = _identity_text(locks.lstat())
    payload = json.dumps(
        {
            "scope_id": uuid.uuid4().hex,
            "machine_id": _machine_id(),
            "directory": str(directory),
            "directory_dev": directory_dev,
            "directory_ino": directory_ino,
            "locks_dev": locks_dev,
            "locks_ino": locks_ino,
        },
        separators=(",", ":"),
    ).encode("utf-8")
    identity = directory / "scope-identity.json"
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
    if hasattr(os, "O_BINARY"):
        flags |= os.O_BINARY
    try:
        fd = os.open(identity, flags, 0o600)
    except FileExistsError:
        return _read_scope_id(directory)
    try:
        os.write(fd, payload)
    finally:
        os.close(fd)
    return _read_scope_id(directory)


def _read_scope_id(directory: Path) -> str:
    identity = directory / "scope-identity.json"
    try:
        info = identity.lstat()
    except OSError as exc:
        raise NotificationExecutionUnavailable("notification scope identity is unreadable") from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise NotificationExecutionUnavailable("notification scope identity is not a regular file")
    try:
        data = json.loads(identity.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise NotificationExecutionUnavailable("notification scope identity is unreadable") from exc
    scope_id = data.get("scope_id") if isinstance(data, dict) else None
    bound_machine = data.get("machine_id") if isinstance(data, dict) else None
    bound_directory = data.get("directory") if isinstance(data, dict) else None
    try:
        directory_dev, directory_ino = _identity_text(directory.lstat())
        locks_info = (directory / "locks").lstat()
    except (OSError, NotificationExecutionUnavailable) as exc:
        raise NotificationExecutionUnavailable("notification scope identity is unreadable") from exc
    if stat.S_ISLNK(locks_info.st_mode) or not stat.S_ISDIR(locks_info.st_mode):
        raise NotificationExecutionUnavailable("notification scope is not trusted on this machine")
    locks_dev, locks_ino = _identity_text(locks_info)
    if (
        not isinstance(scope_id, str)
        or not TOKEN_RE.fullmatch(scope_id)
        or bound_machine != _machine_id()
        or bound_directory != str(directory)
        or data.get("directory_dev") != directory_dev
        or data.get("directory_ino") != directory_ino
        or data.get("locks_dev") != locks_dev
        or data.get("locks_ino") != locks_ino
    ):
        raise NotificationExecutionUnavailable("notification scope is not trusted on this machine")
    return scope_id


def _tighten(path: Path) -> None:
    try:
        os.chmod(path, 0o700)
    except OSError:
        return


def _open_existing_token(path: Path) -> int | None:
    try:
        info = path.lstat()
    except FileNotFoundError as exc:
        raise NotificationLockRefused("missing-token-file") from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise NotificationLockRefused("untrusted-token-file")
    flags = os.O_RDWR
    if hasattr(os, "O_BINARY"):
        flags |= os.O_BINARY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        return os.open(path, flags)
    except FileNotFoundError as exc:
        raise NotificationLockRefused("missing-token-file") from exc
    except OSError as exc:
        raise NotificationLockRefused("untrusted-token-file") from exc


def _identity_text(info) -> tuple[str, str]:
    dev = int(info.st_dev)
    ino = int(info.st_ino)
    if dev == 0 or ino == 0:
        raise NotificationExecutionUnavailable("file identity is not available on this platform")
    return str(dev), str(ino)


def _assert_expected_file(fd: int, path: Path, expected: tuple[str, str]) -> None:
    if _identity_text(os.fstat(fd)) != expected or _identity_text(path.lstat()) != expected:
        raise NotificationLockRefused("untrusted-token-file")


def _assert_same_file(fd: int, path: Path) -> None:
    file_stat = os.fstat(fd)
    path_stat = path.lstat()
    if stat.S_ISLNK(path_stat.st_mode) or not stat.S_ISREG(path_stat.st_mode):
        raise NotificationLockRefused("untrusted-token-file")
    if file_stat.st_dev != path_stat.st_dev or file_stat.st_ino != path_stat.st_ino:
        raise NotificationLockRefused("untrusted-token-file")


def _assert_local_directory(directory: Path) -> None:
    text = str(directory)
    if text.startswith("\\\\") or text.startswith("//"):
        raise NotificationExecutionUnavailable("notification scope cannot be a network path")
    if os.name == "nt":
        import ctypes
        drive, _tail = os.path.splitdrive(text)
        if not drive:
            raise NotificationExecutionUnavailable("notification scope drive is unknown")
        kind = ctypes.windll.kernel32.GetDriveTypeW(drive + "\\")
        if kind not in {2, 3, 6}:
            raise NotificationExecutionUnavailable("notification scope is not a local drive")
        return
    mounts = Path("/proc/mounts")
    if not mounts.is_file():
        raise NotificationExecutionUnavailable("local mount list is unavailable")
    best = ""
    fstype = ""
    for line in mounts.read_text(encoding="utf-8", errors="replace").splitlines():
        parts = line.split()
        if len(parts) < 3:
            continue
        mount = parts[1].replace("\\040", " ")
        if text == mount or text.startswith(mount.rstrip("/") + "/"):
            if len(mount) >= len(best):
                best = mount
                fstype = parts[2].lower()
    if not best or fstype in _NETWORK_FS:
        raise NotificationExecutionUnavailable("notification scope is not a local filesystem")


def _lock_fd(fd: int, *, blocking: bool) -> None:
    if os.name == "nt":
        import msvcrt
        os.lseek(fd, 0, os.SEEK_SET)
        try:
            msvcrt.locking(fd, msvcrt.LK_LOCK if blocking else msvcrt.LK_NBLCK, 1)
        except OSError as exc:
            if not blocking:
                raise BlockingIOError from exc
            raise
        return
    import fcntl
    flags = fcntl.LOCK_EX
    if not blocking:
        flags |= fcntl.LOCK_NB
    try:
        fcntl.flock(fd, flags)
    except OSError as exc:
        if not blocking:
            raise BlockingIOError from exc
        raise
