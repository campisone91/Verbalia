from __future__ import annotations

import csv
import json
import os
import re
import sqlite3
import threading
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path

import config as _config
from modules.contracts import now_tz
from modules.logging_setup import get_logger

logger = get_logger("job_registry")

DATA_DIR_ENV_VAR: str = "TEAMS_SCRIBE_DATA_DIR"
REGISTRY_SCHEMA_VERSION: int = 1
_BUSY_TIMEOUT_MS: int = 5000

# Stati di un job nello storico (lo stage di dettaglio resta quello della state machine)
JOB_RECORDING = "recording"
JOB_PAUSED = "paused"
JOB_QUEUED = "queued"
JOB_PROCESSING = "processing"
JOB_COMPLETED = "completed"
JOB_FAILED = "failed"
JOB_PARKED = "parked"
JOB_INTERRUPTED = "interrupted"
ACTIVE_JOB_STATUSES: tuple[str, ...] = (
    JOB_RECORDING,
    JOB_PAUSED,
    JOB_QUEUED,
    JOB_PROCESSING,
    JOB_FAILED,
    JOB_PARKED,
    JOB_INTERRUPTED,
)
JOB_STATUS_LABELS_IT: dict[str, str] = {
    JOB_RECORDING: "In registrazione",
    JOB_PAUSED: "In pausa",
    JOB_QUEUED: "In coda",
    JOB_PROCESSING: "In elaborazione",
    JOB_COMPLETED: "Completato",
    JOB_FAILED: "Errore (nuovo tentativo)",
    JOB_PARKED: "Fermo: serve Riprova",
    JOB_INTERRUPTED: "Interrotto (serve Riprova)",
}

_JOB_COLUMNS: tuple[str, ...] = (
    "session_id",
    "title",
    "mode",
    "status",
    "stage",
    "detail",
    "progress_current",
    "progress_total",
    "tokens_generated",
    "call_started_at",
    "call_ended_at",
    "processing_started_at",
    "processing_ended_at",
    "stage_started_at",
    "docx_path",
    "session_dir",
    "error",
    "whisper_model",
    "llm_model",
    "llm_on_gpu",
    "attempts",
    "next_retry_at",
    "created_at",
    "updated_at",
)

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS jobs (
    session_id TEXT PRIMARY KEY,
    title TEXT,
    mode TEXT,
    status TEXT NOT NULL,
    stage TEXT,
    detail TEXT,
    progress_current INTEGER,
    progress_total INTEGER,
    tokens_generated INTEGER,
    call_started_at TEXT,
    call_ended_at TEXT,
    processing_started_at TEXT,
    processing_ended_at TEXT,
    stage_started_at TEXT,
    docx_path TEXT,
    session_dir TEXT,
    error TEXT,
    whisper_model TEXT,
    llm_model TEXT,
    llm_on_gpu REAL,
    attempts INTEGER NOT NULL DEFAULT 0,
    next_retry_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status);
CREATE INDEX IF NOT EXISTS idx_jobs_call_started ON jobs(call_started_at);
CREATE TABLE IF NOT EXISTS job_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    occurred_at TEXT NOT NULL,
    kind TEXT NOT NULL,
    stage TEXT,
    detail TEXT
);
CREATE INDEX IF NOT EXISTS idx_events_session ON job_events(session_id, id);
CREATE TABLE IF NOT EXISTS ui_commands (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    command TEXT NOT NULL,
    payload TEXT,
    consumed_at TEXT
);
CREATE TABLE IF NOT EXISTS app_status (
    key TEXT PRIMARY KEY,
    value TEXT,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT
);
CREATE TABLE IF NOT EXISTS deleted_jobs (
    session_id TEXT PRIMARY KEY,
    deleted_at TEXT NOT NULL
);
"""


def resolve_history_db_path(path: Path | None = None) -> Path:
    """Percorso del database storico: argomento, TEAMS_SCRIBE_DATA_DIR oppure data/history.db."""
    if path is not None:
        return path
    env_dir = os.environ.get(DATA_DIR_ENV_VAR, "").strip()
    if env_dir:
        return Path(env_dir) / "history.db"
    return _config.HISTORY_DB_PATH


def _iso(value: datetime | str | None) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    if value.tzinfo is None:
        value = value.astimezone()
    return value.isoformat()


def parse_iso(value: object) -> datetime | None:
    """Converte una stringa ISO 8601 del database in datetime (None se assente o non valida)."""
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.astimezone()


class JobRegistry:
    """Storico dei job e canale comandi GUI su SQLite (WAL).

    state.json resta la fonte di verità per il resume: il database serve a cruscotto e storico
    e sopravvive al cleanup della cartella di sessione. Ogni operazione apre una connessione
    breve, quindi l'istanza è utilizzabile da più thread e da più processi.
    """

    def __init__(self, db_path: Path | None = None) -> None:
        self.db_path: Path = resolve_history_db_path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._write_lock = threading.RLock()
        with self._connect() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(_SCHEMA_SQL)
            conn.execute(
                "INSERT OR IGNORE INTO meta(key, value) VALUES ('schema_version', ?)",
                (str(REGISTRY_SCHEMA_VERSION),),
            )

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(str(self.db_path), timeout=_BUSY_TIMEOUT_MS / 1000.0)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute(f"PRAGMA busy_timeout={_BUSY_TIMEOUT_MS}")
            yield conn
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

    # ------------------------------------------------------------------ jobs
    def upsert_job(self, session_id: str, **fields: object) -> None:
        """Crea o aggiorna la riga del job; i datetime vengono salvati in ISO 8601."""
        unknown = set(fields) - set(_JOB_COLUMNS)
        if unknown:
            raise ValueError(f"Colonne job sconosciute: {sorted(unknown)}")
        now = _iso(now_tz())
        values = {key: (_iso(val) if isinstance(val, datetime) else val) for key, val in fields.items()}
        values["updated_at"] = now
        with self._write_lock, self._connect() as conn:
            exists = conn.execute("SELECT 1 FROM jobs WHERE session_id=?", (session_id,)).fetchone()
            if exists is None:
                values.setdefault("status", JOB_QUEUED)
                values["created_at"] = now
                values["session_id"] = session_id
                columns = ", ".join(values)
                placeholders = ", ".join("?" for _ in values)
                conn.execute(
                    f"INSERT INTO jobs ({columns}) VALUES ({placeholders})",
                    tuple(values.values()),
                )
            elif values:
                assignments = ", ".join(f"{key}=?" for key in values)
                conn.execute(
                    f"UPDATE jobs SET {assignments} WHERE session_id=?",
                    (*values.values(), session_id),
                )

    def update_stage(
        self,
        session_id: str,
        stage: str,
        detail: str = "",
        current: int | None = None,
        total: int | None = None,
        occurred_at: datetime | None = None,
    ) -> None:
        """Aggiorna stage e avanzamento; registra un evento solo quando lo stage cambia."""
        at = _iso(occurred_at or now_tz())
        with self._write_lock, self._connect() as conn:
            row = conn.execute(
                "SELECT stage, progress_current FROM jobs WHERE session_id=?",
                (session_id,),
            ).fetchone()
            if row is None:
                conn.execute(
                    "INSERT INTO jobs (session_id, status, stage, detail, progress_current, progress_total,"
                    " stage_started_at, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
                    (session_id, JOB_QUEUED, stage, detail, current, total, at, at, at),
                )
                changed = True
            else:
                changed = row["stage"] != stage
                step_changed = current != row["progress_current"]
                if changed:
                    conn.execute(
                        "UPDATE jobs SET stage=?, detail=?, progress_current=?, progress_total=?,"
                        " tokens_generated=NULL, stage_started_at=?, updated_at=? WHERE session_id=?",
                        (stage, detail, current, total, at, at, session_id),
                    )
                elif step_changed:
                    conn.execute(
                        "UPDATE jobs SET detail=?, progress_current=?, progress_total=?,"
                        " tokens_generated=NULL, updated_at=? WHERE session_id=?",
                        (detail, current, total, at, session_id),
                    )
                else:
                    conn.execute(
                        "UPDATE jobs SET detail=?, progress_current=?, progress_total=?, updated_at=?"
                        " WHERE session_id=?",
                        (detail, current, total, at, session_id),
                    )
            if changed:
                conn.execute(
                    "INSERT INTO job_events (session_id, occurred_at, kind, stage, detail) VALUES (?,?,?,?,?)",
                    (session_id, at, "stage", stage, detail),
                )

    def add_event(self, session_id: str, kind: str, detail: str = "", stage: str | None = None) -> None:
        with self._write_lock, self._connect() as conn:
            conn.execute(
                "INSERT INTO job_events (session_id, occurred_at, kind, stage, detail) VALUES (?,?,?,?,?)",
                (session_id, _iso(now_tz()), kind, stage, detail),
            )

    append_event = add_event

    def get_job(self, session_id: str) -> dict[str, object] | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM jobs WHERE session_id=?", (session_id,)).fetchone()
            return dict(row) if row is not None else None

    def list_active_jobs(self) -> list[dict[str, object]]:
        """Job non conclusi (registrazione, coda, elaborazione, errore) più i completati di oggi o degli ultimi 30 min."""
        now = now_tz()
        recent_limit = _iso(now - timedelta(minutes=30))
        today_str = now.strftime("%Y-%m-%d")
        placeholders = ", ".join("?" for _ in ACTIVE_JOB_STATUSES)
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT * FROM jobs WHERE status IN ({placeholders})"
                " OR (status=? AND ("
                "   COALESCE(processing_ended_at, updated_at) >= ?"
                "   OR substr(COALESCE(call_started_at, processing_ended_at, created_at), 1, 10) = ?"
                " ))"
                " ORDER BY COALESCE(call_started_at, created_at)",
                (*ACTIVE_JOB_STATUSES, JOB_COMPLETED, recent_limit, today_str),
            ).fetchall()
            return [dict(row) for row in rows]

    def jobs_signature(self) -> tuple[int, str, str]:
        """Firma leggera dello stato della tabella jobs per rilevare variazioni da sincronizzare nella GUI."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS cnt, COALESCE(MAX(updated_at), '') AS max_upd,"
                " COALESCE(GROUP_CONCAT("
                "   session_id || ':' || status || ':' || COALESCE(stage, '') || ':' || COALESCE(title, '') || ':' || COALESCE(docx_path, ''), '|'"
                " ), '') AS summary"
                " FROM (SELECT session_id, status, stage, title, docx_path, updated_at FROM jobs ORDER BY session_id LIMIT 200)"
            ).fetchone()
            if row is None:
                return (0, "", "")
            return (int(row["cnt"]), str(row["max_upd"]), str(row["summary"]))

    def list_history(
        self,
        *,
        day: str | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
        status: str | None = None,
        text: str | None = None,
        limit: int = 1000,
    ) -> list[dict[str, object]]:
        """Storico filtrato per giorno (AAAA-MM-GG), periodo, esito e testo nel titolo."""
        clauses: list[str] = []
        params: list[object] = []
        if day:
            clauses.append("substr(COALESCE(call_started_at, created_at), 1, 10) = ?")
            params.append(day)
        if date_from:
            clauses.append("substr(COALESCE(call_started_at, created_at), 1, 10) >= ?")
            params.append(date_from)
        if date_to:
            clauses.append("substr(COALESCE(call_started_at, created_at), 1, 10) <= ?")
            params.append(date_to)
        if status:
            clauses.append("status = ?")
            params.append(status)
        if text:
            clauses.append("(LOWER(COALESCE(title,'')) LIKE ? OR LOWER(COALESCE(docx_path,'')) LIKE ?)")
            needle = f"%{text.lower()}%"
            params.extend([needle, needle])
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT * FROM jobs {where} ORDER BY COALESCE(call_started_at, created_at) DESC LIMIT ?",
                (*params, int(limit)),
            ).fetchall()
            return [dict(row) for row in rows]

    def get_events(self, session_id: str, limit: int = 500) -> list[dict[str, object]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM job_events WHERE session_id=? ORDER BY id DESC LIMIT ?",
                (session_id, int(limit)),
            ).fetchall()
            return [dict(row) for row in reversed(rows)]

    def stage_durations(self, session_id: str) -> list[tuple[str, float]]:
        """Durata di ogni fase in secondi, ricavata dalla sequenza degli eventi di stage."""
        events = [e for e in self.get_events(session_id, limit=5000) if e.get("kind") == "stage"]
        job = self.get_job(session_id) or {}
        end_marker = parse_iso(job.get("processing_ended_at")) or parse_iso(job.get("updated_at"))
        durations: list[tuple[str, float]] = []
        for index, event in enumerate(events):
            start = parse_iso(event.get("occurred_at"))
            nxt = parse_iso(events[index + 1].get("occurred_at")) if index + 1 < len(events) else end_marker
            if start is None or nxt is None:
                continue
            durations.append((str(event.get("stage")), max(0.0, (nxt - start).total_seconds())))
        return durations

    def delete_job(self, session_id: str) -> dict[str, object] | None:
        """Elimina completamente il job e i suoi eventi dal registro e segna il tombstone in deleted_jobs."""
        at = _iso(now_tz()) or ""
        with self._write_lock, self._connect() as conn:
            row = conn.execute("SELECT * FROM jobs WHERE session_id=?", (session_id,)).fetchone()
            existing = dict(row) if row is not None else None
            conn.execute("DELETE FROM job_events WHERE session_id=?", (session_id,))
            conn.execute("DELETE FROM jobs WHERE session_id=?", (session_id,))
            conn.execute(
                "INSERT OR REPLACE INTO deleted_jobs (session_id, deleted_at) VALUES (?, ?)",
                (session_id, at),
            )
            return existing

    def is_deleted(self, session_id: str) -> bool:
        """Restituisce True se la sessione è stata eliminata definitivamente dall'utente."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM deleted_jobs WHERE session_id=?",
                (session_id,),
            ).fetchone()
            return row is not None

    def purge_older_than(self, days: int) -> int:
        """Retention: elimina i job conclusi più vecchi di `days` giorni."""
        if days <= 0:
            return 0
        limit = _iso(now_tz() - timedelta(days=int(days)))
        with self._write_lock, self._connect() as conn:
            ids = [
                row["session_id"]
                for row in conn.execute(
                    "SELECT session_id FROM jobs WHERE status=? AND COALESCE(call_started_at, created_at) < ?",
                    (JOB_COMPLETED, limit),
                ).fetchall()
            ]
            for session_id in ids:
                conn.execute("DELETE FROM job_events WHERE session_id=?", (session_id,))
                conn.execute("DELETE FROM jobs WHERE session_id=?", (session_id,))
            return len(ids)

    # ------------------------------------------------------------ app status
    def set_status(self, values: Mapping[str, object]) -> None:
        """Aggiorna lo stato dell'applicazione mostrato nella barra del cruscotto."""
        at = _iso(now_tz())
        with self._write_lock, self._connect() as conn:
            conn.executemany(
                "INSERT INTO app_status(key, value, updated_at) VALUES (?,?,?)"
                " ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
                [(key, json.dumps(value, ensure_ascii=False), at) for key, value in values.items()],
            )

    def get_status(self) -> dict[str, object]:
        with self._connect() as conn:
            rows = conn.execute("SELECT key, value, updated_at FROM app_status").fetchall()
        result: dict[str, object] = {}
        for row in rows:
            try:
                result[row["key"]] = json.loads(row["value"]) if row["value"] is not None else None
            except json.JSONDecodeError:
                result[row["key"]] = row["value"]
            result[f"{row['key']}__updated_at"] = row["updated_at"]
        return result

    # ----------------------------------------------------------- ui commands
    def enqueue_command(self, command: str, payload: Mapping[str, object] | None = None) -> int:
        """Inserisce un comando per l'applicazione (usato dal cruscotto in un altro processo)."""
        with self._write_lock, self._connect() as conn:
            cursor = conn.execute(
                "INSERT INTO ui_commands (created_at, command, payload) VALUES (?,?,?)",
                (_iso(now_tz()), command, json.dumps(dict(payload or {}), ensure_ascii=False)),
            )
            return int(cursor.lastrowid or 0)

    def fetch_pending_commands(self, max_age_sec: float = 300.0) -> list[tuple[str, dict[str, object]]]:
        """Legge e marca come consumati i comandi in attesa; quelli troppo vecchi vengono scartati."""
        oldest = _iso(now_tz() - timedelta(seconds=max_age_sec))
        with self._write_lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT id, created_at, command, payload FROM ui_commands WHERE consumed_at IS NULL ORDER BY id"
            ).fetchall()
            if not rows:
                return []
            consumed_at = _iso(now_tz())
            conn.executemany(
                "UPDATE ui_commands SET consumed_at=? WHERE id=?",
                [(consumed_at, row["id"]) for row in rows],
            )
        commands: list[tuple[str, dict[str, object]]] = []
        for row in rows:
            if oldest is not None and str(row["created_at"]) < oldest:
                continue
            try:
                payload = json.loads(row["payload"] or "{}")
            except json.JSONDecodeError:
                payload = {}
            commands.append((str(row["command"]), payload if isinstance(payload, dict) else {}))
        return commands

    # ------------------------------------------------------------------ meta
    def get_meta(self, key: str) -> str | None:
        with self._connect() as conn:
            row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
            return str(row["value"]) if row is not None and row["value"] is not None else None

    def set_meta(self, key: str, value: str) -> None:
        with self._write_lock, self._connect() as conn:
            conn.execute(
                "INSERT INTO meta(key, value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, value),
            )

    # ---------------------------------------------------------------- export
    @staticmethod
    def export_csv(rows: Sequence[Mapping[str, object]], target: Path) -> Path:
        """Esporta le righe dello storico in CSV (separatore ';' per Excel in italiano)."""
        target.parent.mkdir(parents=True, exist_ok=True)
        columns = [
            "call_started_at",
            "title",
            "status",
            "call_ended_at",
            "processing_started_at",
            "processing_ended_at",
            "llm_model",
            "whisper_model",
            "llm_on_gpu",
            "attempts",
            "docx_path",
            "error",
        ]
        with target.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.writer(handle, delimiter=";")
            writer.writerow(columns)
            for row in rows:
                writer.writerow(["" if row.get(col) is None else row.get(col) for col in columns])
        return target

    # ------------------------------------------------------- import dal log
    def import_history_from_log(
        self,
        log_path: Path | None = None,
        output_dir: Path | None = None,
    ) -> int:
        """Importa una tantum dallo storico di app.log le sessioni reali (auto/manuali)."""
        if self.get_meta("log_import_done") == "1":
            return 0
        source = log_path if log_path is not None else (_config.LOG_DIR / "app.log")
        out_root = output_dir if output_dir is not None else _config.OUTPUT_DIR
        sessions = parse_sessions_from_log(source)
        imported = 0
        for session_id, info in sessions.items():
            if self.is_deleted(session_id) or self.get_job(session_id) is not None:
                continue
            name = info.get("docx_path")
            if isinstance(name, str) and name:
                # Il log riporta solo il nome: il file può essere già stato spostato nella cartella del giorno
                for candidate in (out_root / name[:10] / name, out_root / name):
                    if candidate.is_file():
                        info["docx_path"] = str(candidate.resolve())
                        break
            self.upsert_job(session_id, **info)
            imported += 1
        self.set_meta("log_import_done", "1")
        logger.info("Importate %d sessioni storiche da %s.", imported, source.name)
        return imported


_LOG_LINE_RE = re.compile(
    r"^(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}),\d+ \| (?P<level>\w+) \| [^|]+ \| [^|]+ \| "
    r"session=(?P<sid>\S+) \| (?P<msg>.*)$"
)
_SESSION_INIT_RE = re.compile(r"Sessione inizializzata: session_id=(?P<sid>\S+) mode=(?P<mode>\w+)")
_DOCX_DONE_RE = re.compile(r"Documento Word generato e verificato: (?P<name>.+\.docx)$")
_ERROR_RE = re.compile(r"Errore registrato nello stage (?P<stage>\w+) \((?P<type>\w+)\): (?P<msg>.*)$")
_TITLE_FROM_DOCX_RE = re.compile(r"^\d{4}-\d{2}-\d{2}_\d{2}-\d{2}_(?P<title>.+?)_Recap(?:_\d+)?\.docx$")
# Identificativo della fixture dei test: non è una riunione reale
_TEST_FIXTURE_SESSION = "20261023_140000_a1b2c3d4"


def parse_sessions_from_log(log_path: Path) -> dict[str, dict[str, object]]:
    """Ricostruisce dalle righe di app.log (e dai backup ruotati) le sessioni auto/manuali."""
    candidates = [log_path] + [log_path.with_name(f"{log_path.name}.{i}") for i in range(1, 10)]
    sessions: dict[str, dict[str, object]] = {}
    for path in reversed(candidates):
        if not path.is_file():
            continue
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        for line in lines:
            match = _LOG_LINE_RE.match(line)
            if match is None:
                continue
            ts = datetime.strptime(match.group("ts"), "%Y-%m-%d %H:%M:%S").astimezone()
            msg = match.group("msg")
            init = _SESSION_INIT_RE.search(msg)
            if init is not None:
                sid = init.group("sid")
                if init.group("mode") not in ("auto", "manual") or sid == _TEST_FIXTURE_SESSION:
                    continue
                sessions.setdefault(
                    sid,
                    {
                        "mode": init.group("mode"),
                        "status": JOB_FAILED,
                        "stage": "error",
                        "call_started_at": ts,
                        "title": "Riunione Teams",
                    },
                )
                continue
            sid = match.group("sid")
            info = sessions.get(sid)
            if info is None:
                continue
            docx = _DOCX_DONE_RE.search(msg)
            if docx is not None:
                name = docx.group("name").strip()
                title_match = _TITLE_FROM_DOCX_RE.match(name)
                info.update(
                    {
                        "status": JOB_COMPLETED,
                        "stage": "completed",
                        "processing_ended_at": ts,
                        "docx_path": name,
                        "error": None,
                    }
                )
                if title_match is not None:
                    info["title"] = title_match.group("title")
                continue
            err = _ERROR_RE.search(msg)
            if err is not None and info.get("status") != JOB_COMPLETED:
                info["error"] = f"{err.group('type')}: {err.group('msg')}"[:500]
    return sessions
