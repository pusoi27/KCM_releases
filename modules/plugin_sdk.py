"""Lightweight local plugin SDK for Stdytime integrations."""

from __future__ import annotations

import importlib
import json
import os
import threading
from typing import Any

_PLUGIN_LOCK = threading.Lock()
_PLUGIN_STATE: dict[str, Any] = {
    "loaded": False,
    "plugins": [],
    "hooks": {},
    "root": "",
}


def _plugins_root() -> str:
    base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base_dir, "plugins")


def _safe_listdir(path: str) -> list[str]:
    try:
        return sorted(os.listdir(path))
    except Exception:
        return []


def _load_manifest(manifest_path: str) -> dict[str, Any] | None:
    try:
        with open(manifest_path, "r", encoding="utf-8") as fh:
            raw = json.load(fh)
        if not isinstance(raw, dict):
            return None
        return raw
    except Exception:
        return None


def _register_hook(hook_name: str, plugin_id: str, callable_ref) -> None:
    hooks = _PLUGIN_STATE.setdefault("hooks", {})
    handlers = hooks.setdefault(hook_name, [])
    handlers.append({"plugin_id": plugin_id, "callable": callable_ref})


def load_plugins(force_reload: bool = False) -> list[dict[str, Any]]:
    with _PLUGIN_LOCK:
        if _PLUGIN_STATE.get("loaded") and not force_reload:
            return list(_PLUGIN_STATE.get("plugins") or [])

        root = _plugins_root()
        os.makedirs(root, exist_ok=True)

        plugin_entries: list[dict[str, Any]] = []
        hook_map: dict[str, list[dict[str, Any]]] = {}

        for candidate in _safe_listdir(root):
            plugin_dir = os.path.join(root, candidate)
            if not os.path.isdir(plugin_dir):
                continue

            manifest = _load_manifest(os.path.join(plugin_dir, "plugin.json"))
            if not manifest:
                continue

            plugin_id = str(manifest.get("id") or candidate).strip()
            enabled = bool(manifest.get("enabled", True))
            name = str(manifest.get("name") or plugin_id)
            version = str(manifest.get("version") or "0.0.0")
            module_path = str(manifest.get("module") or "").strip()
            hook_names = [str(h).strip() for h in (manifest.get("hooks") or []) if str(h).strip()]

            plugin_info = {
                "id": plugin_id,
                "name": name,
                "version": version,
                "enabled": enabled,
                "module": module_path,
                "hooks": hook_names,
                "status": "loaded",
                "error": "",
            }

            if not enabled:
                plugin_info["status"] = "disabled"
                plugin_entries.append(plugin_info)
                continue

            if not module_path:
                plugin_info["status"] = "invalid"
                plugin_info["error"] = "Manifest missing 'module'."
                plugin_entries.append(plugin_info)
                continue

            try:
                module = importlib.import_module(module_path)
            except Exception as exc:
                plugin_info["status"] = "error"
                plugin_info["error"] = f"import failed: {exc}"
                plugin_entries.append(plugin_info)
                continue

            for hook_name in hook_names:
                hook_callable = getattr(module, hook_name, None)
                if not callable(hook_callable):
                    continue
                handlers = hook_map.setdefault(hook_name, [])
                handlers.append({"plugin_id": plugin_id, "callable": hook_callable})

            plugin_entries.append(plugin_info)

        _PLUGIN_STATE["loaded"] = True
        _PLUGIN_STATE["plugins"] = plugin_entries
        _PLUGIN_STATE["hooks"] = hook_map
        _PLUGIN_STATE["root"] = root

        return list(plugin_entries)


def get_loaded_plugins() -> list[dict[str, Any]]:
    if not _PLUGIN_STATE.get("loaded"):
        return load_plugins()
    return list(_PLUGIN_STATE.get("plugins") or [])


def invoke_hook(hook_name: str, payload: dict[str, Any]) -> dict[str, Any]:
    if not _PLUGIN_STATE.get("loaded"):
        load_plugins()

    state = dict(payload or {})
    handlers = list((_PLUGIN_STATE.get("hooks") or {}).get(hook_name) or [])
    for handler in handlers:
        callable_ref = handler.get("callable")
        if not callable(callable_ref):
            continue
        try:
            maybe_new_state = callable_ref(state)
            if isinstance(maybe_new_state, dict):
                state = maybe_new_state
        except Exception:
            continue
    return state
