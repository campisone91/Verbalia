from __future__ import annotations

import hashlib
import json
import os
import time
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Protocol, runtime_checkable

from config import STATE_SCHEMA_VERSION


# 9.1 Enum Stage
class Stage(str, Enum):
    BOOTSTRAPPING = "bootstrapping"
    IDLE = "idle"
    START_DEBOUNCE = "start_debounce"
    RECORDING = "recording"
    RECORDING_AND_TRANSCRIBING = "recording_and_transcribing"
    GRACE_PERIOD = "grace_period"
    FINALIZING_AUDIO = "finalizing_audio"
    QUEUED = "queued"
    TRANSCRIBING_FINAL = "transcribing_final"
    RELEASING_WHISPER = "releasing_whisper"
    SUMMARIZING_MAP = "summarizing_map"
    SUMMARIZING_REDUCE = "summarizing_reduce"
    BUILDING_DOCX = "building_docx"
    VERIFYING_DOCX = "verifying_docx"
    CLEANING_UP = "cleaning_up"
    COMPLETED = "completed"
    ERROR = "error"
    SHUTTING_DOWN = "shutting_down"


# 9.2 ChunkStatus
class ChunkStatus(str, Enum):
    OPEN = "open"
    RAW_CLOSED = "raw_closed"
    NORMALIZING = "normalizing"
    NORMALIZED = "normalized"
    QUEUED = "queued"
    TRANSCRIBING = "transcribing"
    TRANSCRIBED = "transcribed"
    AUDIO_DELETED = "audio_deleted"
    FAILED = "failed"


# 9.3 TrackLabel
class TrackLabel(str, Enum):
    IO = "IO"
    OTHERS = "ALTRI"


# 9.4 SessionMode
class SessionMode(str, Enum):
    AUTO = "auto"
    MANUAL = "manual"
    RESUME = "resume"
    TEST_PIPELINE = "test_pipeline"
    DRY_RUN = "dry_run"


# 9.5 ShutdownReason
class ShutdownReason(str, Enum):
    USER_REQUEST = "user_request"
    CALL_ENDED = "call_ended"
    GRACE_EXPIRED = "grace_expired"
    MAX_DURATION = "max_duration"
    PROCESS_EXIT = "process_exit"
    FATAL_ERROR = "fatal_error"


# 9.6 UiCommandType
class UiCommandType(str, Enum):
    START_MANUAL = "start_manual"
    FINALIZE_NOW = "finalize_now"
    OPEN_LAST_DOCX = "open_last_docx"
    OPEN_OUTPUT = "open_output"
    OPEN_LOG = "open_log"
    OPEN_DASHBOARD = "open_dashboard"
    RETRY_JOB = "retry_job"
    EXIT = "exit"


def now_tz() -> datetime:
    """Restituisce il timestamp corrente timezone-aware."""
    return datetime.now().astimezone()


def generate_session_id(dt: datetime | None = None) -> str:
    """Genera un session_id nel formato YYYYMMDD_HHMMSS_<uuid8>."""
    ref = dt if dt is not None else now_tz()
    suffix = uuid.uuid4().hex[:8]
    return f"{ref.strftime('%Y%m%d_%H%M%S')}_{suffix}"


# 9.7 MeetingMetadata
@dataclass(slots=True)
class MeetingMetadata:
    title: str
    organizer: str = "Non disponibile"
    required_attendees: list[str] = field(default_factory=list)
    optional_attendees: list[str] = field(default_factory=list)
    agenda: str = ""
    scheduled_start: datetime | None = None
    scheduled_end: datetime | None = None
    source: str = "fallback"


# 9.8 DeviceDescriptor
@dataclass(slots=True)
class DeviceDescriptor:
    index: int
    name: str
    host_api: str
    sample_rate: int
    channels: int
    sample_width: int
    is_loopback: bool


# 9.9 CapturedBlock
@dataclass(slots=True)
class CapturedBlock:
    track: TrackLabel
    captured_at_ns: int
    frame_count: int
    sample_rate: int
    channels: int
    sample_width: int
    payload: bytes


# 9.10 AudioChunk
@dataclass(slots=True)
class AudioChunk:
    index: int
    session_id: str
    start_offset_sec: float
    end_offset_sec: float
    io_wav_path: Path
    others_wav_path: Path
    is_final: bool
    created_at: datetime
    status: ChunkStatus
    discontinuities: list[dict[str, object]] = field(default_factory=list)


# 9.11 TranscriptSegment
@dataclass(slots=True)
class TranscriptSegment:
    track: TrackLabel
    start_sec: float
    end_sec: float
    text: str
    confidence: float | None = None


# 9.12 TranscriptionArtifact
@dataclass(slots=True)
class TranscriptionArtifact:
    chunk_index: int
    transcript_path: Path
    segment_count: int
    first_timestamp_sec: float | None
    last_timestamp_sec: float | None
    sha256: str
    verified: bool
    completed_at: datetime


# 9.13 StageUpdate
@dataclass(slots=True)
class StageUpdate:
    stage: Stage
    detail: str = ""
    current: int | None = None
    total: int | None = None
    occurred_at: datetime = field(default_factory=now_tz)


# 9.14 SessionContext
@dataclass(slots=True)
class SessionContext:
    schema_version: int
    session_id: str
    session_dir: Path
    mode: SessionMode
    metadata: MeetingMetadata
    started_at: datetime
    ended_at: datetime | None = None
    stage: Stage = Stage.IDLE
    recorded_chunks: list[int] = field(default_factory=list)
    normalized_chunks: list[int] = field(default_factory=list)
    transcribed_chunks: list[int] = field(default_factory=list)
    audio_deleted_chunks: list[int] = field(default_factory=list)
    mapped_blocks: list[int] = field(default_factory=list)
    detailed_reduce_completed: bool = False
    email_reduce_completed: bool = False
    final_docx_path: Path | None = None
    final_docx_verified: bool = False
    errors: list[dict[str, object]] = field(default_factory=list)
    discontinuities: list[dict[str, object]] = field(default_factory=list)
    last_updated_at: datetime = field(default_factory=now_tz)


# 10. Eccezioni applicative
class ScribeError(Exception):
    """Errore base dell'applicazione."""


class BootstrapError(ScribeError):
    """Errore durante il bootstrap."""


class ConfigurationError(ScribeError):
    """Configurazione non valida."""


class StateValidationError(ScribeError):
    """State file non valido."""


class InvalidStageTransitionError(ScribeError):
    """Transizione di stato non consentita."""


class AudioDeviceError(ScribeError):
    """Errore di periferica audio."""


class AudioProcessingError(ScribeError):
    """Errore di elaborazione audio."""


class TranscriptionError(ScribeError):
    """Errore durante la trascrizione."""


class OllamaError(ScribeError):
    """Errore nella pipeline Ollama."""


class DocumentBuildError(ScribeError):
    """Errore durante la generazione Word."""


class DocumentValidationError(ScribeError):
    """Documento Word non valido."""


class ResumeError(ScribeError):
    """Sessione non recuperabile."""


class ShutdownRequested(ScribeError):
    """Elaborazione interrotta da una richiesta di arresto: il job va ripreso al riavvio, non è un errore."""


class SessionLockedError(ScribeError):
    """La directory di sessione è già in uso da un altro processo o worker."""


# 11. Protocol condivisi
@runtime_checkable
class StageReporter(Protocol):
    def set_stage(
        self,
        stage: Stage,
        detail_text: str = "",
        current: int | None = None,
        total: int | None = None,
    ) -> None: ...


@runtime_checkable
class SessionStateStore(Protocol):
    def load(self) -> SessionContext: ...

    def save(
        self,
        context: SessionContext,
    ) -> None: ...

    def update_stage(
        self,
        stage: Stage,
        detail: str = "",
    ) -> None: ...


# 13. Macchina a stati
ALLOWED_STAGE_TRANSITIONS: dict[Stage, set[Stage]] = {
    Stage.BOOTSTRAPPING: {
        Stage.IDLE,
        Stage.ERROR,
        Stage.SHUTTING_DOWN,
    },
    Stage.IDLE: {
        Stage.START_DEBOUNCE,
        Stage.RECORDING,
        Stage.ERROR,
        Stage.SHUTTING_DOWN,
    },
    Stage.START_DEBOUNCE: {
        Stage.IDLE,
        Stage.RECORDING,
        Stage.ERROR,
        Stage.SHUTTING_DOWN,
    },
    Stage.RECORDING: {
        Stage.RECORDING_AND_TRANSCRIBING,
        Stage.GRACE_PERIOD,
        Stage.FINALIZING_AUDIO,
        Stage.ERROR,
        Stage.SHUTTING_DOWN,
    },
    Stage.RECORDING_AND_TRANSCRIBING: {
        Stage.RECORDING,
        Stage.GRACE_PERIOD,
        Stage.FINALIZING_AUDIO,
        Stage.ERROR,
        Stage.SHUTTING_DOWN,
    },
    Stage.GRACE_PERIOD: {
        Stage.RECORDING,
        Stage.RECORDING_AND_TRANSCRIBING,
        Stage.FINALIZING_AUDIO,
        Stage.ERROR,
        Stage.SHUTTING_DOWN,
    },
    Stage.FINALIZING_AUDIO: {
        Stage.QUEUED,
        Stage.TRANSCRIBING_FINAL,
        Stage.ERROR,
        Stage.SHUTTING_DOWN,
    },
    # Un job in coda riparte dal primo passo incompleto: le fasi già concluse vengono saltate
    Stage.QUEUED: {
        Stage.TRANSCRIBING_FINAL,
        Stage.SUMMARIZING_MAP,
        Stage.SUMMARIZING_REDUCE,
        Stage.BUILDING_DOCX,
        Stage.VERIFYING_DOCX,
        Stage.CLEANING_UP,
        Stage.ERROR,
        Stage.SHUTTING_DOWN,
    },
    Stage.TRANSCRIBING_FINAL: {
        Stage.RELEASING_WHISPER,
        Stage.SUMMARIZING_MAP,
        Stage.ERROR,
        Stage.SHUTTING_DOWN,
    },
    Stage.RELEASING_WHISPER: {
        Stage.SUMMARIZING_MAP,
        Stage.ERROR,
        Stage.SHUTTING_DOWN,
    },
    Stage.SUMMARIZING_MAP: {
        Stage.SUMMARIZING_REDUCE,
        Stage.ERROR,
        Stage.SHUTTING_DOWN,
    },
    Stage.SUMMARIZING_REDUCE: {
        Stage.BUILDING_DOCX,
        Stage.ERROR,
        Stage.SHUTTING_DOWN,
    },
    Stage.BUILDING_DOCX: {
        Stage.VERIFYING_DOCX,
        Stage.ERROR,
        Stage.SHUTTING_DOWN,
    },
    Stage.VERIFYING_DOCX: {
        Stage.CLEANING_UP,
        Stage.ERROR,
        Stage.SHUTTING_DOWN,
    },
    Stage.CLEANING_UP: {
        Stage.COMPLETED,
        Stage.ERROR,
        Stage.SHUTTING_DOWN,
    },
    Stage.COMPLETED: {
        Stage.IDLE,
        Stage.SHUTTING_DOWN,
    },
    Stage.ERROR: {
        Stage.IDLE,
        Stage.QUEUED,
        Stage.SHUTTING_DOWN,
    },
    Stage.SHUTTING_DOWN: set(),
}


def validate_stage_transition(current: Stage, target: Stage) -> None:
    """Valida una transizione di stato secondo ALLOWED_STAGE_TRANSITIONS."""
    if current == target:
        return
    allowed = ALLOWED_STAGE_TRANSITIONS.get(current, set())
    if target not in allowed:
        raise InvalidStageTransitionError(
            f"Transizione di stato non consentita: {current.value} -> {target.value}"
        )


# 12. Serializzazione dei contratti
def _serialize_datetime(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.astimezone()
    return value.isoformat()


def _deserialize_datetime(value: object, field_name: str, *, allow_none: bool) -> datetime | None:
    if value is None:
        if allow_none:
            return None
        raise StateValidationError(f"Il campo '{field_name}' non può essere nullo.")
    if not isinstance(value, str) or not value.strip():
        raise StateValidationError(f"Il campo '{field_name}' deve essere una stringa ISO 8601.")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise StateValidationError(
            f"Formato data non valido per '{field_name}': {value}"
        ) from exc
    if parsed.tzinfo is None:
        parsed = parsed.astimezone()
    return parsed


def _require_str(payload: Mapping[str, object], key: str, *, allow_empty: bool = False) -> str:
    if key not in payload:
        raise StateValidationError(f"Campo obbligatorio mancante: '{key}'")
    val = payload[key]
    if not isinstance(val, str):
        raise StateValidationError(f"Il campo '{key}' deve essere una stringa.")
    if not allow_empty and not val.strip():
        raise StateValidationError(f"Il campo '{key}' non può essere vuoto.")
    return val


def _require_bool(payload: Mapping[str, object], key: str) -> bool:
    if key not in payload:
        raise StateValidationError(f"Campo obbligatorio mancante: '{key}'")
    val = payload[key]
    if not isinstance(val, bool):
        raise StateValidationError(f"Il campo '{key}' deve essere booleano.")
    return val


def _require_int_list(payload: Mapping[str, object], key: str) -> list[int]:
    if key not in payload:
        raise StateValidationError(f"Campo obbligatorio mancante: '{key}'")
    val = payload[key]
    if not isinstance(val, list):
        raise StateValidationError(f"Il campo '{key}' deve essere una lista.")
    result: list[int] = []
    for item in val:
        if isinstance(item, bool) or not isinstance(item, int):
            raise StateValidationError(
                f"Tutti gli elementi di '{key}' devono essere interi."
            )
        result.append(item)
    return result


def _require_str_list(payload: Mapping[str, object], key: str) -> list[str]:
    if key not in payload:
        raise StateValidationError(f"Campo obbligatorio mancante: '{key}'")
    val = payload[key]
    if not isinstance(val, list):
        raise StateValidationError(f"Il campo '{key}' deve essere una lista di stringhe.")
    result: list[str] = []
    for item in val:
        if not isinstance(item, str):
            raise StateValidationError(
                f"Tutti gli elementi di '{key}' devono essere stringhe."
            )
        result.append(item)
    return result


def _require_dict_list(
    payload: Mapping[str, object], key: str
) -> list[dict[str, object]]:
    if key not in payload:
        raise StateValidationError(f"Campo obbligatorio mancante: '{key}'")
    val = payload[key]
    if not isinstance(val, list):
        raise StateValidationError(f"Il campo '{key}' deve essere una lista di oggetti.")
    result: list[dict[str, object]] = []
    for item in val:
        if not isinstance(item, Mapping):
            raise StateValidationError(
                f"Tutti gli elementi di '{key}' devono essere dizionari."
            )
        result.append({str(k): v for k, v in item.items()})
    return result


def meeting_metadata_to_dict(metadata: MeetingMetadata) -> dict[str, object]:
    """Serializza MeetingMetadata in un dizionario JSON-compatibile."""
    return {
        "title": metadata.title,
        "organizer": metadata.organizer,
        "required_attendees": list(metadata.required_attendees),
        "optional_attendees": list(metadata.optional_attendees),
        "agenda": metadata.agenda,
        "scheduled_start": _serialize_datetime(metadata.scheduled_start),
        "scheduled_end": _serialize_datetime(metadata.scheduled_end),
        "source": metadata.source,
    }


def meeting_metadata_from_dict(payload: Mapping[str, object]) -> MeetingMetadata:
    """Deserializza e valida MeetingMetadata da un dizionario."""
    if not isinstance(payload, Mapping):
        raise StateValidationError("Il blocco metadata deve essere un dizionario.")
    title = _require_str(payload, "title", allow_empty=False)
    organizer = _require_str(payload, "organizer", allow_empty=True)
    required_attendees = _require_str_list(payload, "required_attendees")
    optional_attendees = _require_str_list(payload, "optional_attendees")
    agenda = _require_str(payload, "agenda", allow_empty=True)
    scheduled_start = _deserialize_datetime(
        payload.get("scheduled_start"), "scheduled_start", allow_none=True
    )
    scheduled_end = _deserialize_datetime(
        payload.get("scheduled_end"), "scheduled_end", allow_none=True
    )
    source = _require_str(payload, "source", allow_empty=False)
    return MeetingMetadata(
        title=title,
        organizer=organizer,
        required_attendees=required_attendees,
        optional_attendees=optional_attendees,
        agenda=agenda,
        scheduled_start=scheduled_start,
        scheduled_end=scheduled_end,
        source=source,
    )


def session_context_to_dict(context: SessionContext) -> dict[str, object]:
    """Serializza SessionContext in un dizionario pronto per JSON."""
    final_docx_str: str | None = (
        str(context.final_docx_path.resolve())
        if context.final_docx_path is not None
        else None
    )
    return {
        "schema_version": context.schema_version,
        "session_id": context.session_id,
        "session_dir": str(context.session_dir.resolve()),
        "mode": context.mode.value,
        "metadata": meeting_metadata_to_dict(context.metadata),
        "started_at": _serialize_datetime(context.started_at),
        "ended_at": _serialize_datetime(context.ended_at),
        "stage": context.stage.value,
        "recorded_chunks": list(context.recorded_chunks),
        "normalized_chunks": list(context.normalized_chunks),
        "transcribed_chunks": list(context.transcribed_chunks),
        "audio_deleted_chunks": list(context.audio_deleted_chunks),
        "mapped_blocks": list(context.mapped_blocks),
        "detailed_reduce_completed": context.detailed_reduce_completed,
        "email_reduce_completed": context.email_reduce_completed,
        "final_docx_path": final_docx_str,
        "final_docx_verified": context.final_docx_verified,
        "errors": [dict(err) for err in context.errors],
        "discontinuities": [dict(disc) for disc in context.discontinuities],
        "last_updated_at": _serialize_datetime(context.last_updated_at),
    }


def session_context_from_dict(payload: Mapping[str, object]) -> SessionContext:
    """Deserializza e valida SessionContext da un dizionario."""
    if not isinstance(payload, Mapping):
        raise StateValidationError("Il payload di SessionContext deve essere un dizionario.")

    if "schema_version" not in payload:
        raise StateValidationError("Campo obbligatorio mancante: 'schema_version'")
    schema_version = payload["schema_version"]
    if isinstance(schema_version, bool) or not isinstance(schema_version, int):
        raise StateValidationError("Il campo 'schema_version' deve essere un intero.")
    if schema_version != STATE_SCHEMA_VERSION:
        raise StateValidationError(
            f"Versione schema incompatibile: attesa {STATE_SCHEMA_VERSION}, "
            f"trovata {schema_version}."
        )

    session_id = _require_str(payload, "session_id", allow_empty=False)
    session_dir_raw = _require_str(payload, "session_dir", allow_empty=False)
    session_dir = Path(session_dir_raw)
    if not session_dir.is_absolute():
        raise StateValidationError(
            f"Il campo 'session_dir' deve essere un path assoluto: {session_dir_raw}"
        )

    mode_raw = _require_str(payload, "mode", allow_empty=False)
    try:
        mode = SessionMode(mode_raw)
    except ValueError as exc:
        raise StateValidationError(f"SessionMode non valido: {mode_raw}") from exc

    if "metadata" not in payload or not isinstance(payload["metadata"], Mapping):
        raise StateValidationError("Campo obbligatorio 'metadata' mancante o non valido.")
    metadata = meeting_metadata_from_dict(payload["metadata"])

    started_at = _deserialize_datetime(
        payload.get("started_at"), "started_at", allow_none=False
    )
    assert started_at is not None

    ended_at = _deserialize_datetime(
        payload.get("ended_at"), "ended_at", allow_none=True
    )

    stage_raw = _require_str(payload, "stage", allow_empty=False)
    try:
        stage = Stage(stage_raw)
    except ValueError as exc:
        raise StateValidationError(f"Stage non valido: {stage_raw}") from exc

    recorded_chunks = _require_int_list(payload, "recorded_chunks")
    normalized_chunks = _require_int_list(payload, "normalized_chunks")
    transcribed_chunks = _require_int_list(payload, "transcribed_chunks")
    audio_deleted_chunks = _require_int_list(payload, "audio_deleted_chunks")
    mapped_blocks = _require_int_list(payload, "mapped_blocks")

    detailed_reduce_completed = _require_bool(payload, "detailed_reduce_completed")
    email_reduce_completed = _require_bool(payload, "email_reduce_completed")

    if "final_docx_path" not in payload:
        raise StateValidationError("Campo obbligatorio mancante: 'final_docx_path'")
    final_docx_raw = payload["final_docx_path"]
    final_docx_path: Path | None
    if final_docx_raw is None:
        final_docx_path = None
    elif isinstance(final_docx_raw, str) and final_docx_raw.strip():
        final_docx_path = Path(final_docx_raw)
        if not final_docx_path.is_absolute():
            raise StateValidationError(
                f"Il campo 'final_docx_path' deve essere assoluto: {final_docx_raw}"
            )
    else:
        raise StateValidationError(
            "Il campo 'final_docx_path' deve essere una stringa valida o null."
        )

    final_docx_verified = _require_bool(payload, "final_docx_verified")
    errors = _require_dict_list(payload, "errors")
    discontinuities = _require_dict_list(payload, "discontinuities")

    last_updated_at = _deserialize_datetime(
        payload.get("last_updated_at"), "last_updated_at", allow_none=False
    )
    assert last_updated_at is not None

    return SessionContext(
        schema_version=schema_version,
        session_id=session_id,
        session_dir=session_dir,
        mode=mode,
        metadata=metadata,
        started_at=started_at,
        ended_at=ended_at,
        stage=stage,
        recorded_chunks=recorded_chunks,
        normalized_chunks=normalized_chunks,
        transcribed_chunks=transcribed_chunks,
        audio_deleted_chunks=audio_deleted_chunks,
        mapped_blocks=mapped_blocks,
        detailed_reduce_completed=detailed_reduce_completed,
        email_reduce_completed=email_reduce_completed,
        final_docx_path=final_docx_path,
        final_docx_verified=final_docx_verified,
        errors=errors,
        discontinuities=discontinuities,
        last_updated_at=last_updated_at,
    )


# 15. Utility atomiche
_REPLACE_MAX_ATTEMPTS: int = 10
_REPLACE_BASE_DELAY_SEC: float = 0.02


def replace_with_retry(
    src: Path,
    dst: Path,
    *,
    attempts: int = _REPLACE_MAX_ATTEMPTS,
) -> None:
    """Esegue os.replace ritentando sui PermissionError transitori di Windows (Defender, indexer, file aperti)."""
    for attempt in range(max(1, attempts)):
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            if attempt + 1 >= attempts:
                raise
            time.sleep(_REPLACE_BASE_DELAY_SEC * (attempt + 1))


def unique_temp_path(path: Path) -> Path:
    """Restituisce un percorso temporaneo univoco accanto a path, sicuro con scrittori concorrenti."""
    return path.with_name(f"{path.name}.{os.getpid()}.{uuid.uuid4().hex[:8]}.tmp")


def atomic_write_text(
    path: Path,
    content: str,
) -> None:
    """Scrive un file di testo in modo atomico con flush, fsync e os.replace."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = unique_temp_path(path)

    try:
        with temp_path.open(
            "w",
            encoding="utf-8",
            newline="\n",
        ) as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())

        replace_with_retry(temp_path, path)
    except BaseException:
        try:
            temp_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def atomic_write_json(
    path: Path,
    payload: Mapping[str, object],
) -> None:
    """Scrive un oggetto JSON in modo atomico e ne verifica la rilettura."""
    content = json.dumps(
        payload,
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
    )

    atomic_write_text(path, content)

    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise StateValidationError(f"Il JSON scritto è corrotto: {path}") from exc

    if not isinstance(loaded, dict):
        raise StateValidationError("Il JSON scritto non è un oggetto.")


def sha256_file(path: Path) -> str:
    """Calcola lo SHA-256 di un file leggendolo a blocchi."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(65536)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def sha256_text(text: str) -> str:
    """Calcola lo SHA-256 di una stringa codificata in UTF-8."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()
