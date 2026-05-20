"""Process-level single-instance lock utilities."""

from __future__ import annotations

import json
import os
import tempfile
import ctypes
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional


def _pid_is_alive(pid: int) -> bool:
	if pid <= 0:
		return False
	try:
		os.kill(pid, 0)
	except ProcessLookupError:
		return False
	except PermissionError:
		# Exists but owned by another user/session.
		return True
	except OSError:
		return False
	return True


def _safe_read_json(path: str) -> dict:
	try:
		with open(path, 'r', encoding='utf-8') as fh:
			return json.load(fh)
	except Exception:
		return {}


@dataclass
class _LockState:
	file_descriptor: int
	lock_path: str


_LOCK_STATE: Optional[_LockState] = None
_WINDOWS_MUTEX_HANDLE = None


def _build_lock_name(app_name: str, host: str, port: int) -> str:
	return f"{app_name}_{host}_{port}".replace(':', '_').replace('/', '_').replace('\\', '_')


def _acquire_windows_mutex(lock_name: str, host: str, port: int) -> None:
	"""Acquire a per-session named mutex on Windows.

	Using a kernel mutex avoids stale lock-file/PID-reuse false positives that can
	require an unnecessary reboot to clear.
	"""
	global _WINDOWS_MUTEX_HANDLE

	if _WINDOWS_MUTEX_HANDLE is not None:
		return

	mutex_name = f"Local\\{lock_name}"
	handle = ctypes.windll.kernel32.CreateMutexW(None, False, mutex_name)
	if not handle:
		err = ctypes.GetLastError()
		raise RuntimeError(f"Failed to acquire Windows instance mutex '{mutex_name}' (error {err}).")

	ERROR_ALREADY_EXISTS = 183
	err = ctypes.GetLastError()
	if err == ERROR_ALREADY_EXISTS:
		ctypes.windll.kernel32.CloseHandle(handle)
		raise RuntimeError(
			f"Another Stdytime instance is already running on {host}:{port}. "
			"Close it before launching a new one."
		)

	_WINDOWS_MUTEX_HANDLE = handle


def ensure_single_instance(app_name: str, host: str = '127.0.0.1', port: int = 5000) -> None:
	"""Acquire an instance lock or raise RuntimeError if another live instance exists."""
	global _LOCK_STATE

	if _LOCK_STATE is not None or _WINDOWS_MUTEX_HANDLE is not None:
		return

	base_lock_name = _build_lock_name(app_name, host, port)

	if os.name == 'nt':
		_acquire_windows_mutex(base_lock_name, host, port)
		return

	lock_name = f"{base_lock_name}.lock"
	lock_path = os.path.join(tempfile.gettempdir(), lock_name)

	for _ in range(2):
		try:
			fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
			payload = {
				'pid': os.getpid(),
				'host': host,
				'port': port,
				'started_utc': datetime.now(timezone.utc).isoformat(),
			}
			os.write(fd, json.dumps(payload).encode('utf-8'))
			os.fsync(fd)
			_LOCK_STATE = _LockState(file_descriptor=fd, lock_path=lock_path)
			return
		except FileExistsError:
			info = _safe_read_json(lock_path)
			existing_pid = int(info.get('pid') or 0)
			if existing_pid and _pid_is_alive(existing_pid):
				raise RuntimeError(
					f"Another Stdytime instance is already running (PID {existing_pid}) on "
					f"{host}:{port}. Close it before launching a new one."
				)
			# Stale lock from a crashed process; remove and retry once.
			try:
				os.remove(lock_path)
			except FileNotFoundError:
				pass
			except Exception:
				break
		except Exception as exc:
			raise RuntimeError(f"Failed to acquire instance lock at '{lock_path}': {exc}") from exc

	raise RuntimeError(
		f"Unable to acquire instance lock at '{lock_path}'. "
		"If no Stdytime process is running, delete the lock file and retry."
	)


def release_single_instance_lock() -> None:
	"""Release the process lock (best effort)."""
	global _LOCK_STATE
	global _WINDOWS_MUTEX_HANDLE

	if _WINDOWS_MUTEX_HANDLE is not None:
		try:
			ctypes.windll.kernel32.CloseHandle(_WINDOWS_MUTEX_HANDLE)
		except Exception:
			pass
		finally:
			_WINDOWS_MUTEX_HANDLE = None

	if _LOCK_STATE is None:
		return

	state = _LOCK_STATE
	_LOCK_STATE = None

	try:
		os.close(state.file_descriptor)
	except Exception:
		pass

	try:
		os.remove(state.lock_path)
	except Exception:
		pass
