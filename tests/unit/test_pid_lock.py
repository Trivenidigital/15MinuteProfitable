"""Tests for src.utils.pid_lock.PidLock."""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import patch

import pytest

# Ensure env is set before any src imports
os.environ.setdefault("BOT_PRIVATE_KEY", "0x" + "ab" * 32)

from src.utils.pid_lock import PidLock, _is_process_alive


# ---------------------------------------------------------------------------
# _is_process_alive
# ---------------------------------------------------------------------------


class TestIsProcessAlive:
    def test_current_process_is_alive(self) -> None:
        assert _is_process_alive(os.getpid()) is True

    def test_nonexistent_pid(self) -> None:
        # PID 99999999 is extremely unlikely to exist
        assert _is_process_alive(99999999) is False


# ---------------------------------------------------------------------------
# PidLock — acquire / release
# ---------------------------------------------------------------------------


class TestPidLockAcquireRelease:
    def test_acquire_creates_file(self, tmp_path: Path) -> None:
        lock_path = str(tmp_path / "test.pid")
        lock = PidLock(lock_path)
        assert lock.acquire() is True
        assert lock.is_acquired is True
        assert Path(lock_path).exists()
        assert Path(lock_path).read_text() == str(os.getpid())
        lock.release()

    def test_release_removes_file(self, tmp_path: Path) -> None:
        lock_path = str(tmp_path / "test.pid")
        lock = PidLock(lock_path)
        lock.acquire()
        lock.release()
        assert not Path(lock_path).exists()
        assert lock.is_acquired is False

    def test_acquire_blocked_by_live_process(self, tmp_path: Path) -> None:
        lock_path = str(tmp_path / "test.pid")
        # Write current PID to simulate another running instance
        Path(lock_path).write_text(str(os.getpid()))

        lock = PidLock(lock_path)
        assert lock.acquire() is False
        assert lock.is_acquired is False

    def test_acquire_cleans_stale_lock(self, tmp_path: Path) -> None:
        lock_path = str(tmp_path / "test.pid")
        # Write a PID that doesn't exist
        Path(lock_path).write_text("99999999")

        lock = PidLock(lock_path)
        assert lock.acquire() is True
        assert lock.is_acquired is True
        lock.release()

    def test_acquire_cleans_corrupted_lock(self, tmp_path: Path) -> None:
        lock_path = str(tmp_path / "test.pid")
        Path(lock_path).write_text("not_a_number")

        lock = PidLock(lock_path)
        assert lock.acquire() is True
        lock.release()

    def test_double_release_safe(self, tmp_path: Path) -> None:
        lock_path = str(tmp_path / "test.pid")
        lock = PidLock(lock_path)
        lock.acquire()
        lock.release()
        lock.release()  # Should not raise


# ---------------------------------------------------------------------------
# PidLock — context manager
# ---------------------------------------------------------------------------


class TestPidLockContextManager:
    def test_context_manager_acquires_and_releases(self, tmp_path: Path) -> None:
        lock_path = str(tmp_path / "test.pid")
        with PidLock(lock_path) as lock:
            assert lock.is_acquired is True
            assert Path(lock_path).exists()
        assert not Path(lock_path).exists()

    def test_context_manager_raises_if_blocked(self, tmp_path: Path) -> None:
        lock_path = str(tmp_path / "test.pid")
        Path(lock_path).write_text(str(os.getpid()))

        with pytest.raises(RuntimeError, match="another instance"):
            with PidLock(lock_path):
                pass

    def test_context_manager_releases_on_exception(self, tmp_path: Path) -> None:
        lock_path = str(tmp_path / "test.pid")
        with pytest.raises(ValueError):
            with PidLock(lock_path):
                raise ValueError("boom")
        assert not Path(lock_path).exists()

    def test_path_property(self, tmp_path: Path) -> None:
        lock_path = str(tmp_path / "test.pid")
        lock = PidLock(lock_path)
        assert lock.path == Path(lock_path)
