import json
import random
import sqlite3
import threading
import time
import uuid
from datetime import datetime

import requests

from modules.database import DB_PATH, get_station_runtime_config
from modules import student_manager
from modules import assistant_manager
from modules.book_manager import loan_book, return_book, clear_active_loan, get_book
from modules.materials_manager import loan_material, return_material, clear_active_material_loan, get_material

_RECONCILER_THREAD: threading.Thread | None = None
_RECONCILER_STOP = threading.Event()
_RECONCILER_LOCK = threading.Lock()
_RECONCILE_STATE_LOCK = threading.Lock()
_RECONCILE_STATE = {
    "last_run_started_at": "",
    "last_run_finished_at": "",
    "last_run_processed": 0,
    "last_run_sent": 0,
    "last_run_skipped": False,
    "last_run_reason": "never_run",
    "last_error": "",
}

_RETRY_BASE_SECONDS = 5
_RETRY_MAX_SECONDS = 300
_RETRY_JITTER_MIN = 0.80
_RETRY_JITTER_MAX = 1.20


def _now_iso() -> str:
    return datetime.now().isoformat()


def _runtime_station_mode() -> str:
    return str(get_station_runtime_config().get("station_mode") or "").strip().lower() or "instructor_server"


def _runtime_instructor_api_base() -> str:
    return str(get_station_runtime_config().get("instructor_api_base_url") or "").strip().rstrip("/")


def _runtime_pairing_token() -> str:
    return str(get_station_runtime_config().get("station_pairing_token") or "").strip()


def _scanner_api_client_enabled() -> bool:
    return _runtime_station_mode() == "scanner_api_client"


def _ensure_tables() -> None:
    with sqlite3.connect(DB_PATH) as conn:
        cur = conn.cursor()
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS scanner_mutation_queue (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                op_id TEXT UNIQUE NOT NULL,
                op_type TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                attempts INTEGER NOT NULL DEFAULT 0,
                last_error TEXT DEFAULT '',
                next_retry_at TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS scanner_mutation_applied (
                op_id TEXT PRIMARY KEY,
                op_type TEXT NOT NULL,
                applied_at TEXT NOT NULL
            )
            """
        )

        # Lightweight schema migration for existing installs.
        table_info = cur.execute("PRAGMA table_info(scanner_mutation_queue)").fetchall()
        existing_cols = {str(row[1] or "").strip().lower() for row in table_info}
        if "next_retry_at" not in existing_cols:
            cur.execute("ALTER TABLE scanner_mutation_queue ADD COLUMN next_retry_at TEXT")
        conn.commit()


def _compute_retry_delay_seconds(next_attempt_number: int) -> int:
    attempt = max(1, int(next_attempt_number or 1))
    raw = _RETRY_BASE_SECONDS * (2 ** (attempt - 1))
    bounded = min(_RETRY_MAX_SECONDS, raw)
    jitter = random.uniform(_RETRY_JITTER_MIN, _RETRY_JITTER_MAX)
    return max(1, int(round(float(bounded) * float(jitter))))


def _set_reconcile_state(**fields) -> None:
    with _RECONCILE_STATE_LOCK:
        _RECONCILE_STATE.update(fields)


def enqueue_mutation(op_type: str, payload: dict, op_id: str | None = None) -> str:
    _ensure_tables()
    resolved_op_id = str(op_id or uuid.uuid4().hex).strip()
    payload_json = json.dumps(payload or {}, ensure_ascii=False, separators=(",", ":"))
    now = _now_iso()

    with sqlite3.connect(DB_PATH) as conn:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT OR IGNORE INTO scanner_mutation_queue
            (op_id, op_type, payload_json, status, attempts, last_error, next_retry_at, created_at, updated_at)
            VALUES (?, ?, ?, 'pending', 0, '', ?, ?, ?)
            """,
            (resolved_op_id, str(op_type or "").strip(), payload_json, now, now, now),
        )
        conn.commit()

    return resolved_op_id


def _pending_mutations(limit: int = 60) -> list[dict]:
    _ensure_tables()
    out: list[dict] = []
    now = _now_iso()
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        rows = cur.execute(
            """
            SELECT op_id, op_type, payload_json, attempts, next_retry_at
            FROM scanner_mutation_queue
            WHERE status = 'pending'
              AND (
                    next_retry_at IS NULL
                    OR TRIM(COALESCE(next_retry_at, '')) = ''
                    OR next_retry_at <= ?
                  )
            ORDER BY id ASC
            LIMIT ?
            """,
            (now, int(max(1, limit))),
        ).fetchall()
        for row in rows:
            try:
                payload = json.loads(row["payload_json"] or "{}")
            except Exception:
                payload = {}
            out.append(
                {
                    "op_id": str(row["op_id"] or "").strip(),
                    "op_type": str(row["op_type"] or "").strip(),
                    "payload": payload,
                    "attempts": int(row["attempts"] or 0),
                    "next_retry_at": str(row["next_retry_at"] or "").strip(),
                }
            )
    return out


def _mark_mutation_sent(op_id: str) -> None:
    now = _now_iso()
    with sqlite3.connect(DB_PATH) as conn:
        cur = conn.cursor()
        cur.execute(
            """
            UPDATE scanner_mutation_queue
            SET status = 'sent', updated_at = ?, last_error = '', next_retry_at = NULL
            WHERE op_id = ?
            """,
            (now, str(op_id or "").strip()),
        )
        conn.commit()


def _mark_mutation_error(op_id: str, error: str) -> None:
    now = _now_iso()
    with sqlite3.connect(DB_PATH) as conn:
        cur = conn.cursor()
        row = cur.execute(
            "SELECT attempts FROM scanner_mutation_queue WHERE op_id = ? LIMIT 1",
            (str(op_id or "").strip(),),
        ).fetchone()
        current_attempts = int((row[0] if row else 0) or 0)
        next_attempt = current_attempts + 1
        delay_seconds = _compute_retry_delay_seconds(next_attempt)
        retry_at_iso = datetime.fromtimestamp(time.time() + delay_seconds).isoformat()
        cur.execute(
            """
            UPDATE scanner_mutation_queue
            SET attempts = COALESCE(attempts, 0) + 1,
                updated_at = ?,
                last_error = ?,
                next_retry_at = ?
            WHERE op_id = ?
            """,
            (now, str(error or "")[:500], retry_at_iso, str(op_id or "").strip()),
        )
        conn.commit()


def _bridge_headers() -> dict:
    token = _runtime_pairing_token()
    headers = {"Accept": "application/json", "Content-Type": "application/json"}
    if token:
        headers["X-Stdytime-Pairing-Token"] = token
    return headers


def reconcile_pending_once(limit: int = 50) -> dict:
    started_at = _now_iso()

    if not _scanner_api_client_enabled():
        result = {"processed": 0, "sent": 0, "skipped": True, "reason": "not_scanner_api_client"}
        _set_reconcile_state(
            last_run_started_at=started_at,
            last_run_finished_at=_now_iso(),
            last_run_processed=0,
            last_run_sent=0,
            last_run_skipped=True,
            last_run_reason="not_scanner_api_client",
        )
        return result

    base = _runtime_instructor_api_base()
    if not base.startswith("http"):
        result = {"processed": 0, "sent": 0, "skipped": True, "reason": "missing_instructor_api_base_url"}
        _set_reconcile_state(
            last_run_started_at=started_at,
            last_run_finished_at=_now_iso(),
            last_run_processed=0,
            last_run_sent=0,
            last_run_skipped=True,
            last_run_reason="missing_instructor_api_base_url",
        )
        return result

    url = f"{base}/api/bridge/reconcile/apply"
    pending = _pending_mutations(limit=limit)
    if not pending:
        result = {"processed": 0, "sent": 0, "skipped": False}
        _set_reconcile_state(
            last_run_started_at=started_at,
            last_run_finished_at=_now_iso(),
            last_run_processed=0,
            last_run_sent=0,
            last_run_skipped=False,
            last_run_reason="ok",
        )
        return result

    processed = 0
    sent = 0

    for item in pending:
        op_id = item["op_id"]
        payload = {
            "op_id": op_id,
            "op_type": item["op_type"],
            "payload": item["payload"],
        }
        processed += 1
        try:
            resp = requests.post(url, headers=_bridge_headers(), json=payload, timeout=12)
        except requests.RequestException as exc:
            message = f"network: {exc}"
            _mark_mutation_error(op_id, message)
            _set_reconcile_state(last_error=message)
            break

        body = {}
        try:
            body = resp.json() if "json" in str(resp.headers.get("content-type") or "").lower() else {}
        except Exception:
            body = {}

        if resp.ok and bool(body.get("ok", True)):
            _mark_mutation_sent(op_id)
            sent += 1
            continue

        # Conflict-safe behavior: only treat explicit duplicates as applied.
        if resp.status_code == 409 and bool(body.get("duplicate")):
            _mark_mutation_sent(op_id)
            sent += 1
            continue

        _mark_mutation_error(op_id, body.get("error") or f"http {resp.status_code}")
        _set_reconcile_state(last_error=(body.get("error") or f"http {resp.status_code}"))
        if resp.status_code >= 500 or resp.status_code == 409:
            break

    result = {"processed": processed, "sent": sent, "skipped": False}
    _set_reconcile_state(
        last_run_started_at=started_at,
        last_run_finished_at=_now_iso(),
        last_run_processed=int(processed),
        last_run_sent=int(sent),
        last_run_skipped=False,
        last_run_reason="ok",
    )
    return result


def get_queue_observability() -> dict:
    _ensure_tables()
    now = _now_iso()
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()

        total_pending = int(
            (cur.execute("SELECT COUNT(*) FROM scanner_mutation_queue WHERE status='pending'").fetchone() or [0])[0] or 0
        )
        total_sent = int(
            (cur.execute("SELECT COUNT(*) FROM scanner_mutation_queue WHERE status='sent'").fetchone() or [0])[0] or 0
        )
        ready_pending = int(
            (
                cur.execute(
                    """
                    SELECT COUNT(*)
                    FROM scanner_mutation_queue
                    WHERE status='pending'
                      AND (
                            next_retry_at IS NULL
                            OR TRIM(COALESCE(next_retry_at, '')) = ''
                            OR next_retry_at <= ?
                          )
                    """,
                    (now,),
                ).fetchone()
                or [0]
            )[0]
            or 0
        )
        delayed_pending = max(0, total_pending - ready_pending)

        oldest_pending_row = cur.execute(
            """
            SELECT created_at
            FROM scanner_mutation_queue
            WHERE status='pending'
            ORDER BY created_at ASC
            LIMIT 1
            """
        ).fetchone()
        next_retry_row = cur.execute(
            """
            SELECT next_retry_at
            FROM scanner_mutation_queue
            WHERE status='pending'
              AND TRIM(COALESCE(next_retry_at, '')) != ''
            ORDER BY next_retry_at ASC
            LIMIT 1
            """
        ).fetchone()
        last_error_row = cur.execute(
            """
            SELECT last_error
            FROM scanner_mutation_queue
            WHERE status='pending'
              AND TRIM(COALESCE(last_error, '')) != ''
            ORDER BY updated_at DESC
            LIMIT 1
            """
        ).fetchone()

    with _RECONCILE_STATE_LOCK:
        reconcile_state = dict(_RECONCILE_STATE)

    return {
        "mode": _runtime_station_mode(),
        "is_scanner_api_client": bool(_scanner_api_client_enabled()),
        "pending_total": total_pending,
        "pending_ready": ready_pending,
        "pending_delayed": delayed_pending,
        "sent_total": total_sent,
        "oldest_pending_created_at": str((oldest_pending_row[0] if oldest_pending_row else "") or ""),
        "next_retry_at": str((next_retry_row[0] if next_retry_row else "") or ""),
        "last_queue_error": str((last_error_row[0] if last_error_row else "") or ""),
        "reconcile": reconcile_state,
    }


def _already_applied(cur: sqlite3.Cursor, op_id: str) -> bool:
    row = cur.execute(
        "SELECT op_id FROM scanner_mutation_applied WHERE op_id = ? LIMIT 1",
        (str(op_id or "").strip(),),
    ).fetchone()
    return bool(row)


def _mark_applied(cur: sqlite3.Cursor, op_id: str, op_type: str) -> None:
    cur.execute(
        """
        INSERT OR IGNORE INTO scanner_mutation_applied (op_id, op_type, applied_at)
        VALUES (?, ?, ?)
        """,
        (str(op_id or "").strip(), str(op_type or "").strip(), _now_iso()),
    )


def _apply_session_start(cur: sqlite3.Cursor, payload: dict) -> None:
    sid = int(payload.get("student_id") or 0)
    if sid <= 0:
        return
    if not student_manager.get_student(sid):
        return
    open_row = cur.execute(
        "SELECT id FROM sessions WHERE student_id=? AND end_time IS NULL ORDER BY id DESC LIMIT 1",
        (sid,),
    ).fetchone()
    if open_row:
        return
    cur.execute(
        "INSERT INTO sessions (student_id, start_time) VALUES (?, ?)",
        (sid, str(payload.get("started_at") or _now_iso()).strip()),
    )


def _apply_session_stop(cur: sqlite3.Cursor, payload: dict) -> None:
    sid = int(payload.get("student_id") or 0)
    if sid <= 0:
        return
    row = cur.execute(
        "SELECT id, start_time FROM sessions WHERE student_id=? AND end_time IS NULL ORDER BY id DESC LIMIT 1",
        (sid,),
    ).fetchone()
    if not row:
        return

    sess_id = int(row[0])
    start_time = str(row[1] or "").strip()
    end_time = str(payload.get("end_time") or _now_iso()).strip()

    duration = payload.get("duration")
    if duration is None:
        try:
            from modules.utils import duration_seconds
            duration = max(0, int(duration_seconds(start_time, end_time)))
        except Exception:
            duration = 0

    cur.execute(
        "UPDATE sessions SET end_time=?, duration=?, sync_synced=0 WHERE id=?",
        (end_time, int(duration or 0), sess_id),
    )


def _apply_assistant_set_duty(cur: sqlite3.Cursor, payload: dict) -> None:
    aid = int(payload.get("assistant_id") or 0)
    if aid <= 0:
        return
    if not assistant_manager.get_assistant(aid):
        return

    desired_on_duty = bool(payload.get("on_duty"))
    open_row = cur.execute(
        "SELECT id, start_time FROM assistant_sessions WHERE assistant_id=? AND end_time IS NULL ORDER BY id DESC LIMIT 1",
        (aid,),
    ).fetchone()

    if desired_on_duty:
        if open_row:
            return
        cur.execute(
            "INSERT INTO assistant_sessions (assistant_id, start_time, end_time, duration) VALUES (?, ?, NULL, NULL)",
            (aid, _now_iso()),
        )
        return

    if not open_row:
        return

    sess_id = int(open_row[0])
    start_time = str(open_row[1] or "").strip()
    end_time = _now_iso()
    try:
        from modules.utils import duration_seconds
        duration = max(0, int(duration_seconds(start_time, end_time)))
    except Exception:
        duration = 0

    cur.execute(
        "UPDATE assistant_sessions SET end_time=?, duration=?, sync_synced=0 WHERE id=?",
        (end_time, duration, sess_id),
    )


def _apply_book_loan(payload: dict) -> None:
    book_id = int(payload.get("book_id") or 0)
    student_id = int(payload.get("student_id") or 0)
    if book_id <= 0 or student_id <= 0:
        return

    book = get_book(book_id)
    if not book:
        return

    borrower_id = int(book[9] or 0) if len(book) > 9 and book[9] is not None else 0
    if borrower_id == student_id:
        return
    if borrower_id > 0:
        return

    loan_book(book_id, student_id)


def _apply_book_return(payload: dict) -> None:
    book_id = int(payload.get("book_id") or 0)
    if book_id <= 0:
        return
    book = get_book(book_id)
    if not book:
        return
    is_available = bool(book[3]) if len(book) > 3 else True
    if is_available:
        return
    return_book(book_id)


def _apply_book_clear(payload: dict) -> None:
    book_id = int(payload.get("book_id") or 0)
    student_id = int(payload.get("student_id") or 0)
    if book_id <= 0 or student_id <= 0:
        return
    clear_active_loan(book_id, student_id)


def _apply_material_loan(payload: dict) -> None:
    material_id = int(payload.get("material_id") or 0)
    student_id = int(payload.get("student_id") or 0)
    if material_id <= 0 or student_id <= 0:
        return

    material = get_material(material_id)
    if not material:
        return

    borrower_id = int(material[8] or 0) if len(material) > 8 and material[8] is not None else 0
    if borrower_id == student_id:
        return
    if borrower_id > 0:
        return

    loan_material(material_id, student_id)


def _apply_material_return(payload: dict) -> None:
    material_id = int(payload.get("material_id") or 0)
    if material_id <= 0:
        return
    material = get_material(material_id)
    if not material:
        return
    is_available = bool(material[3]) if len(material) > 3 else True
    if is_available:
        return
    return_material(material_id)


def _apply_material_clear(payload: dict) -> None:
    material_id = int(payload.get("material_id") or 0)
    student_id = int(payload.get("student_id") or 0)
    if material_id <= 0 or student_id <= 0:
        return
    clear_active_material_loan(material_id, student_id)


def apply_incoming_mutation(op_id: str, op_type: str, payload: dict) -> dict:
    _ensure_tables()
    op_id = str(op_id or "").strip()
    op_type = str(op_type or "").strip()
    payload = payload or {}

    if not op_id or not op_type:
        return {"ok": False, "error": "op_id and op_type are required.", "code": 400}

    with sqlite3.connect(DB_PATH) as conn:
        cur = conn.cursor()

        if _already_applied(cur, op_id):
            return {"ok": True, "duplicate": True, "applied": False, "code": 200}

        try:
            if op_type == "session_start":
                _apply_session_start(cur, payload)
            elif op_type == "session_stop":
                _apply_session_stop(cur, payload)
            elif op_type == "assistant_set_duty":
                _apply_assistant_set_duty(cur, payload)
            elif op_type == "book_loan":
                _apply_book_loan(payload)
            elif op_type == "book_return":
                _apply_book_return(payload)
            elif op_type == "book_clear_loan":
                _apply_book_clear(payload)
            elif op_type == "material_loan":
                _apply_material_loan(payload)
            elif op_type == "material_return":
                _apply_material_return(payload)
            elif op_type == "material_clear_loan":
                _apply_material_clear(payload)
            else:
                return {"ok": False, "error": f"Unsupported op_type: {op_type}", "code": 400}

            _mark_applied(cur, op_id, op_type)
            conn.commit()
            return {"ok": True, "duplicate": False, "applied": True, "code": 200}
        except Exception as exc:
            conn.rollback()
            return {"ok": False, "error": str(exc), "code": 409}


def start_scanner_reconciler(interval_seconds: int = 15) -> None:
    global _RECONCILER_THREAD

    with _RECONCILER_LOCK:
        if _RECONCILER_THREAD and _RECONCILER_THREAD.is_alive():
            return

        _RECONCILER_STOP.clear()
        interval = max(5, int(interval_seconds or 15))

        def _loop() -> None:
            while not _RECONCILER_STOP.is_set():
                try:
                    reconcile_pending_once(limit=80)
                except Exception:
                    pass
                _RECONCILER_STOP.wait(interval)

        _RECONCILER_THREAD = threading.Thread(
            target=_loop,
            daemon=True,
            name="scanner-offline-reconciler",
        )
        _RECONCILER_THREAD.start()


def stop_scanner_reconciler() -> None:
    _RECONCILER_STOP.set()
