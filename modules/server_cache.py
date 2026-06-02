"""Simple server-side in-memory cache with variable TTL and jitter.

This module is intentionally lightweight for a first migration step.
It supports:
- per-policy TTL configuration
- randomized TTL jitter to reduce synchronized expirations
- thread-safe get/set/invalidate operations

Note: this is process-local memory cache. In multi-worker deployments,
each worker maintains its own cache.
"""

import logging
from dataclasses import dataclass
from random import randint
from threading import RLock
from time import monotonic
from typing import Any, Callable, Dict, Optional

# Set to True (or flip at runtime via CACHE_DEBUG=True) to see hit/miss/invalidate lines.
# Kept False by default so production logs stay clean.
DEBUG_CACHE: bool = False

_logger = logging.getLogger("server_cache")


def _log(event: str, key: str, policy: str = "") -> None:
    """Emit a single cache-debug line when DEBUG_CACHE is enabled."""
    if DEBUG_CACHE:
        lane = f" [{policy}]" if policy else ""
        _logger.debug("[cache] %-12s %s%s", event, key, lane)


@dataclass
class _CacheEntry:
	value: Any
	expires_at: float


_lock = RLock()
_store: Dict[str, _CacheEntry] = {}


# Shared cache key base strings
STUDENTS_LIST_CACHE_KEY = "students:list:v1"
BOOKS_CATALOG_CACHE_KEY = "books:catalog:v1"
BOOK_DETAIL_CACHE_PREFIX = "books:detail:v1:"
ASSISTANTS_PROFILE_LIST_CACHE_KEY = "assistants:profiles:list:v1"
ASSISTANTS_DUTY_LIST_CACHE_KEY = "assistants:duty:list:v1"


# TTL policies (seconds)
CACHE_POLICIES = {
	"default": {"ttl": 300, "jitter": 30},
	# Example: student check-in data => 1h + rand(0..30m)
	"checkin": {"ttl": 3600, "jitter": 1800},
	# Dashboard/student attendance view should reflect new active sessions quickly,
	# even when a session is started from another tab or machine.
	"checkin_live": {"ttl": 2, "jitter": 0},
	# Books catalog/details are mostly static and can live longer.
	"book_catalog": {"ttl": 21600, "jitter": 1800},
	# Assistant profiles are mostly static.
	"assistant_profile": {"ttl": 21600, "jitter": 1800},
	# Assistant duty state changes more often; keep shorter-lived.
	"assistant_duty": {"ttl": 900, "jitter": 120},
}


def _ttl_with_jitter(policy: str = "default", ttl_seconds: Optional[int] = None) -> int:
	"""Resolve effective TTL using policy defaults and optional override."""
	if ttl_seconds is not None:
		return max(1, int(ttl_seconds))

	conf = CACHE_POLICIES.get(policy, CACHE_POLICIES["default"])
	base_ttl = int(conf.get("ttl", 300))
	jitter = int(conf.get("jitter", 0))
	return max(1, base_ttl + (randint(0, jitter) if jitter > 0 else 0))


def get_cache(key: str) -> Any:
	"""Return cached value or None if missing/expired."""
	now = monotonic()
	with _lock:
		entry = _store.get(key)
		if not entry:
			_log("MISS(absent)", key)
			return None
		if entry.expires_at <= now:
			_store.pop(key, None)
			_log("MISS(expired)", key)
			return None
		_log("HIT", key)
		return entry.value


def set_cache(key: str, value: Any, policy: str = "default", ttl_seconds: Optional[int] = None) -> Any:
	"""Store value in cache and return it."""
	ttl = _ttl_with_jitter(policy=policy, ttl_seconds=ttl_seconds)
	expires_at = monotonic() + ttl
	with _lock:
		_store[key] = _CacheEntry(value=value, expires_at=expires_at)
	_log("STORE", key, policy)
	return value


def get_or_set(key: str, builder: Callable[[], Any], policy: str = "default", ttl_seconds: Optional[int] = None) -> Any:
	"""Read-through caching: return existing entry, otherwise build and store."""
	cached = get_cache(key)
	if cached is not None:
		# get_cache already logged HIT
		return cached
	_log("BUILD", key, policy)
	value = builder()
	return set_cache(key, value, policy=policy, ttl_seconds=ttl_seconds)


def invalidate(key: str) -> None:
	"""Invalidate one cache key."""
	with _lock:
		_store.pop(key, None)
	_log("INVALIDATE", key)


def invalidate_prefix(prefix: str) -> int:
	"""Invalidate all keys with the given prefix. Returns count deleted."""
	deleted = 0
	with _lock:
		keys = [k for k in _store.keys() if k.startswith(prefix)]
		for key in keys:
			_store.pop(key, None)
			deleted += 1
	if deleted:
		_log("INVALIDATE_PFX", f"{prefix}* ({deleted} keys)")
	return deleted


def clear_all() -> None:
	"""Clear all cache entries."""
	with _lock:
		_store.clear()

