from __future__ import annotations

import json
import os
import re
import shutil
import stat
import tempfile
import threading
from collections.abc import Callable
from pathlib import Path

from config import (
    DOCX_MIN_SIZE_BYTES,
    EXIT_CLEANUP_FAILED,
    OUTPUT_DIR,
    STATE_SCHEMA_VERSION,
    TEMP_DIR,
)
from modules.contracts import (
    ScribeError,
    SessionContext,
    SessionLockedError,
    Stage,
    StateValidationError,
    atomic_write_json,
    meeting_metadata_to_dict,
    now_tz,
    session_context_from_dict,
    session_context_to_dict,
    sha256_file,
    validate_stage_transition,
)
from modules.logging_setup import get_logger, sanitize_for_log

try:
    import msvcrt
except ImportError:  # pragma: no cover - piattaforme non Windows
    msvcrt = None  # type: ignore[assignment]

_DEFAULT_TEMP_DIR: Path = TEMP_DIR.resolve()
_DEFAULT_OUTPUT_DIR: Path = OUTPUT_DIR.resolve()

_SESSION_SUBDIRS: tuple[str, ...] = (
    "audio",
    "transcripts",
    "maps",
    "reduce",
    "metadata",
)

_TRANSCRIPT_SEGMENT_RE = re.compile(r"^\[\d{2}:\d{2}:\d{2}\]")
_RAW_IO_RE = re.compile(r"^chunk_(?P<idx>\d{3,})_io_raw\.wav$")
_NORM_IO_RE = re.compile(r"^chunk_(?P<idx>\d{3,})_io_16k\.wav$")
OWNER_LOCK_FILENAME: str = ".owner.lock"
_DAY_FOLDER_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_WAV_MIN_BYTES: int = 44


def _is_reparse_or_symlink(path: Path) -> bool:
    """Rileva se un percorso esistente è un symlink o una junction/reparse point NTFS."""
    try:
        if path.is_symlink():
            return True
        st = os.lstat(path)
    except OSError:
        return False

    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    file_attrs = getattr(st, "st_file_attributes", 0)
    return bool(file_attrs & reparse_flag)


def ensure_safe_child_path(base_dir: Path, target_path: Path) -> Path:
    """Verifica che target_path sia un discendente diretto e sicuro di base_dir."""
    if ".." in target_path.parts:
        raise StateValidationError(
            f"Rilevato path traversal ('..') nel percorso: {target_path}"
        )

    if _is_reparse_or_symlink(base_dir):
        raise StateValidationError(
            f"La directory base non può essere un symlink o junction: {base_dir}"
        )

    try:
        base_resolved = base_dir.resolve()
    except OSError as exc:
        raise StateValidationError(
            f"Impossibile risolvere la directory base {base_dir}: {exc}"
        ) from exc

    # Verifica che il target (o i suoi genitori intermedi) non siano symlink/junction
    candidate = (
        target_path if target_path.is_absolute() else (base_resolved / target_path)
    )
    current = candidate
    while True:
        if _is_reparse_or_symlink(current):
            raise StateValidationError(
                f"Symlink o junction non consentito nel percorso: {current}"
            )
        if current == base_resolved or current.parent == current:
            break
        current = current.parent

    try:
        target_resolved = candidate.resolve()
    except OSError as exc:
        raise StateValidationError(
            f"Impossibile risolvere il percorso {target_path}: {exc}"
        ) from exc

    if (
        target_resolved == base_resolved
        or base_resolved not in target_resolved.parents
    ):
        raise StateValidationError(
            f"Percorso esterno al perimetro consentito ({base_resolved}): {target_path}"
        )

    return target_resolved


def _resolve_effective_temp_root(
    session_dir: Path,
    explicit_temp_root: Path | None,
) -> Path:
    """Determina la root temporanea effettiva per la validazione di confinamento."""
    if explicit_temp_root is not None:
        return explicit_temp_root.resolve()

    current_temp_dir = TEMP_DIR.resolve()
    if current_temp_dir != _DEFAULT_TEMP_DIR:
        return current_temp_dir

    if ".." not in session_dir.parts and not _is_reparse_or_symlink(session_dir):
        try:
            resolved_session = session_dir.resolve()
            if current_temp_dir in resolved_session.parents:
                return current_temp_dir
            system_temp = Path(tempfile.gettempdir()).resolve()
            if (
                system_temp in resolved_session.parents
                and resolved_session.name.startswith("session_")
                and not _is_reparse_or_symlink(resolved_session.parent)
            ):
                return resolved_session.parent
        except OSError:
            return current_temp_dir

    return current_temp_dir


def _resolve_effective_output_root(
    final_docx: Path,
    session_dir: Path,
    explicit_output_root: Path | None,
) -> Path:
    """Determina la root di output effettiva per la validazione del DOCX finale."""
    if explicit_output_root is not None:
        return explicit_output_root.resolve()

    current_output_dir = OUTPUT_DIR.resolve()
    if current_output_dir != _DEFAULT_OUTPUT_DIR:
        return current_output_dir

    if ".." not in final_docx.parts and not _is_reparse_or_symlink(final_docx):
        try:
            resolved_docx = final_docx.resolve()
            if current_output_dir in resolved_docx.parents:
                return current_output_dir
            system_temp = Path(tempfile.gettempdir()).resolve()
            resolved_session = session_dir.resolve()
            if (
                system_temp in resolved_session.parents
                and system_temp in resolved_docx.parents
                and resolved_session not in resolved_docx.parents
                and resolved_docx != resolved_session
            ):
                # Con l'output per giorno il DOCX sta in output/AAAA-MM-GG/: la root è la cartella sopra
                candidate_root = resolved_docx.parent
                if _DAY_FOLDER_RE.match(candidate_root.name):
                    candidate_root = candidate_root.parent
                parent_name = candidate_root.name.lower()
                if parent_name in ("output", "out") or (
                    candidate_root == resolved_session.parent
                ):
                    return candidate_root
        except OSError:
            return current_output_dir

    return current_output_dir


class SessionOwnerLock:
    """Lock esclusivo su session_dir/.owner.lock (msvcrt.locking) per segnalare la sessione in uso.

    Il lock è legato all'handle aperto: se il processo muore il sistema operativo lo rilascia,
    quindi la recovery all'avvio può distinguere le sessioni orfane da quelle in uso.
    """

    def __init__(self, session_dir: Path) -> None:
        self.path: Path = session_dir / OWNER_LOCK_FILENAME
        self._handle: object | None = None

    @property
    def held(self) -> bool:
        return self._handle is not None

    def acquire(self) -> bool:
        if self._handle is not None:
            return True
        if msvcrt is None:
            return True
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = open(self.path, "a+b")  # noqa: SIM115 - l'handle resta aperto finché si detiene il lock
        try:
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            handle.close()
            return False
        self._handle = handle
        return True

    def release(self) -> None:
        handle = self._handle
        self._handle = None
        if handle is None or msvcrt is None:
            return
        try:
            handle.seek(0)  # type: ignore[attr-defined]
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)  # type: ignore[attr-defined]
        except OSError:
            pass
        finally:
            try:
                handle.close()  # type: ignore[attr-defined]
            except OSError:
                pass


def is_session_locked(session_dir: Path) -> bool:
    """True se un altro handle (thread, worker o processo) detiene il lock della sessione."""
    probe = SessionOwnerLock(session_dir)
    if not probe.path.is_file():
        return False
    if not probe.acquire():
        return True
    probe.release()
    return False


def acquire_session_owner_lock(session_dir: Path) -> SessionOwnerLock:
    """Acquisisce il lock esclusivo per la session_dir o solleva SessionLockedError."""
    lock = SessionOwnerLock(session_dir)
    if not lock.acquire():
        raise SessionLockedError(f"Impossibile acquisire il lock per {session_dir}")
    return lock


def release_session_owner_lock(session_dir: Path | SessionOwnerLock) -> None:
    """Rilascia il lock esclusivo sulla session_dir se attivo."""
    if isinstance(session_dir, SessionOwnerLock):
        session_dir.release()
    else:
        SessionOwnerLock(session_dir).release()


def save_session_state(context: SessionContext) -> None:
    """Inizializza e salva lo stato iniziale della sessione."""
    sm = StateManager(context.session_dir)
    sm.initialize(context)


class StateManager:
    """Gestore thread-safe e crash-safe dello stato di sessione su disco."""

    def __init__(
        self,
        session_dir: Path,
        *,
        temp_root: Path | None = None,
        output_root: Path | None = None,
    ) -> None:
        self._lock = threading.RLock()
        self._logger = get_logger("state_manager")
        self._explicit_temp_root = temp_root
        self._explicit_output_root = output_root
        self._temp_root: Path = _resolve_effective_temp_root(
            session_dir, temp_root
        )
        self.session_dir: Path = ensure_safe_child_path(
            self._temp_root, session_dir
        )
        self.state_path: Path = ensure_safe_child_path(
            self.session_dir, self.session_dir / "state.json"
        )
        self._closed: bool = False
        self._owner_lock = SessionOwnerLock(self.session_dir)

    @property
    def closed(self) -> bool:
        """True dopo il cleanup: la directory è stata rimossa e non va più scritta."""
        with self._lock:
            return self._closed

    def acquire_owner_lock(self) -> None:
        """Acquisisce il lock della sessione; solleva SessionLockedError se è già in uso altrove."""
        with self._lock:
            if not self._owner_lock.acquire():
                raise SessionLockedError(
                    f"La sessione {self.session_dir.name} è già in uso da un altro processo."
                )

    def release_owner_lock(self) -> None:
        """Rilascia il lock della sessione, se detenuto."""
        with self._lock:
            self._owner_lock.release()

    @property
    def owns_lock(self) -> bool:
        with self._lock:
            return self._owner_lock.held

    def initialize(self, context: SessionContext) -> None:
        """Crea la struttura directory della sessione e scrive atomicamente state.json e meeting.json."""
        with self._lock:
            ensure_safe_child_path(self._temp_root, self.session_dir)

            if context.schema_version != STATE_SCHEMA_VERSION:
                raise StateValidationError(
                    f"Versione schema non valida: {context.schema_version}"
                )

            if context.session_dir.resolve() != self.session_dir:
                raise StateValidationError(
                    "Il campo session_dir del contesto non coincide con la directory gestita."
                )

            self.session_dir.mkdir(parents=True, exist_ok=True)
            for subdir_name in _SESSION_SUBDIRS:
                subdir_path = ensure_safe_child_path(
                    self.session_dir, self.session_dir / subdir_name
                )
                subdir_path.mkdir(parents=True, exist_ok=True)

            context.session_dir = self.session_dir
            context.last_updated_at = now_tz()

            meeting_path = ensure_safe_child_path(
                self.session_dir,
                self.session_dir / "metadata" / "meeting.json",
            )
            atomic_write_json(
                meeting_path,
                meeting_metadata_to_dict(context.metadata),
            )

            self.save(context)
            self._logger.info(
                "Sessione inizializzata: session_id=%s mode=%s stage=%s",
                sanitize_for_log(context.session_id),
                context.mode.value,
                context.stage.value,
            )

    def load(self) -> SessionContext:
        """Carica e valida SessionContext da state.json."""
        with self._lock:
            ensure_safe_child_path(self._temp_root, self.session_dir)
            safe_state_path = ensure_safe_child_path(
                self.session_dir, self.state_path
            )

            if not safe_state_path.is_file():
                raise StateValidationError(
                    f"File state.json non trovato: {safe_state_path}"
                )

            try:
                raw_text = safe_state_path.read_text(encoding="utf-8")
                payload = json.loads(raw_text)
            except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise StateValidationError(
                    f"File state.json illeggibile o corrotto: {safe_state_path}"
                ) from exc

            if not isinstance(payload, dict):
                raise StateValidationError(
                    "Il contenuto di state.json deve essere un oggetto JSON."
                )

            context = session_context_from_dict(payload)
            if context.session_dir.resolve() != self.session_dir:
                raise StateValidationError(
                    "Il session_dir dichiarato in state.json non coincide con la directory della sessione."
                )
            return context

    def save(self, context: SessionContext) -> None:
        """Serializza, valida e salva atomicamente SessionContext in state.json."""
        with self._lock:
            if self._closed:
                # Dopo il cleanup una scrittura ricreerebbe la cartella di sessione appena rimossa
                raise StateValidationError(
                    f"Sessione {self.session_dir.name} già chiusa: scrittura di state.json ignorata."
                )
            ensure_safe_child_path(self._temp_root, self.session_dir)
            safe_state_path = ensure_safe_child_path(
                self.session_dir, self.state_path
            )

            if context.session_dir.resolve() != self.session_dir:
                raise StateValidationError(
                    "Impossibile salvare un contesto appartenente a un'altra directory di sessione."
                )

            context.session_dir = self.session_dir
            context.last_updated_at = now_tz()
            payload = session_context_to_dict(context)
            # Verifica preventiva di round-trip
            _ = session_context_from_dict(payload)
            atomic_write_json(safe_state_path, payload)

    def update_stage(self, stage: Stage, detail: str = "") -> None:
        """Valida la transizione di stato e aggiorna atomicamente state.json."""
        with self._lock:
            context = self.load()
            previous_stage = context.stage
            validate_stage_transition(previous_stage, stage)
            context.stage = stage
            context.last_updated_at = now_tz()
            self.save(context)
            self._logger.info(
                "Transizione stage: %s -> %s (detail=%s)",
                previous_stage.value,
                stage.value,
                sanitize_for_log(detail) if detail else "-",
            )

    def mutate(
        self,
        callback: Callable[[SessionContext], None],
    ) -> SessionContext:
        """Applica una mutazione atomica e thread-safe a SessionContext."""
        with self._lock:
            context = self.load()
            callback(context)
            context.last_updated_at = now_tz()
            self.save(context)
            return context

    def record_error(
        self,
        stage: Stage,
        exc: Exception,
        *,
        recoverable: bool = True,
        affects_stage: bool = True,
    ) -> None:
        """Registra un errore sintetico in state.json preservando i dettagli nel log.

        Con affects_stage=False l'errore viene solo annotato (es. fallimento su un singolo
        chunk durante la registrazione) senza portare l'intera sessione in ERROR.
        """
        with self._lock:
            message = sanitize_for_log(
                str(exc).strip() or type(exc).__name__, max_len=240
            )
            error_entry: dict[str, object] = {
                "stage": stage.value,
                "type": type(exc).__name__,
                "message": message,
                "occurred_at": now_tz().isoformat(),
                "recoverable": bool(recoverable),
                "affects_stage": bool(affects_stage),
            }
            self._logger.error(
                "Errore registrato nello stage %s (%s): %s",
                stage.value,
                type(exc).__name__,
                message,
            )

            if self.state_path.is_file():
                context = self.load()
                context.errors.append(error_entry)
                if affects_stage and context.stage != Stage.SHUTTING_DOWN:
                    context.stage = Stage.ERROR
                self.save(context)

    def mark_email_reduce_completed(self, completed: bool = True) -> SessionContext:
        """Imposta il flag email_reduce_completed nel context della sessione."""
        def _mut(c: SessionContext) -> None:
            c.email_reduce_completed = completed
        return self.mutate(_mut)

    def _is_valid_wav_pair(self, path_a: Path, path_b: Path) -> bool:
        """Verifica che entrambi i file WAV di un chunk esistano e abbiano dimensione valida."""
        try:
            safe_a = ensure_safe_child_path(self.session_dir, path_a)
            safe_b = ensure_safe_child_path(self.session_dir, path_b)
        except StateValidationError:
            return False

        if not (safe_a.is_file() and safe_b.is_file()):
            return False
        try:
            return (
                safe_a.stat().st_size >= _WAV_MIN_BYTES
                and safe_b.stat().st_size >= _WAV_MIN_BYTES
            )
        except OSError:
            return False

    def _is_valid_transcript_artifact(self, chunk_index: int) -> bool:
        """Verifica che il file transcript di un chunk esista, sia UTF-8 valido e rispetti l'header."""
        transcript_path = (
            self.session_dir / "transcripts" / f"chunk_{chunk_index:03d}.txt"
        )
        try:
            safe_path = ensure_safe_child_path(self.session_dir, transcript_path)
        except StateValidationError:
            return False

        if not safe_path.is_file():
            return False

        try:
            if safe_path.stat().st_size == 0:
                return False
            content = safe_path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return False

        lines = [line.strip() for line in content.splitlines() if line.strip()]
        if not lines:
            return False

        has_chunk_header = any(line.startswith("# Chunk") for line in lines[:5])
        has_body = any(
            _TRANSCRIPT_SEGMENT_RE.match(line) is not None
            or line == "[NESSUN PARLATO RILEVATO]"
            for line in lines
        )
        if not (has_chunk_header and has_body):
            return False

        # Verifica eventuale sidecar di checksum se presente
        sha_sidecar = safe_path.with_suffix(".txt.sha256")
        if sha_sidecar.is_file():
            try:
                expected_sha = sha_sidecar.read_text(encoding="utf-8").strip().split()[0]
                if expected_sha.lower() != sha256_file(safe_path).lower():
                    return False
            except (OSError, UnicodeDecodeError, IndexError):
                return False

        meta_sidecar = safe_path.with_suffix(".meta.json")
        if meta_sidecar.is_file():
            try:
                meta_payload = json.loads(meta_sidecar.read_text(encoding="utf-8"))
                if isinstance(meta_payload, dict) and isinstance(
                    meta_payload.get("sha256"), str
                ):
                    if (
                        meta_payload["sha256"].strip().lower()
                        != sha256_file(safe_path).lower()
                    ):
                        return False
            except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                return False

        return True

    def _is_valid_map_artifact(self, block_index: int) -> bool:
        """Verifica che map_block_NNN.md e map_block_NNN.meta.json siano presenti e coerenti per SHA-256."""
        maps_dir = self.session_dir / "maps"
        md_path = maps_dir / f"map_block_{block_index:03d}.md"
        meta_path = maps_dir / f"map_block_{block_index:03d}.meta.json"

        try:
            safe_md = ensure_safe_child_path(self.session_dir, md_path)
            safe_meta = ensure_safe_child_path(self.session_dir, meta_path)
        except StateValidationError:
            return False

        if not (safe_md.is_file() and safe_meta.is_file()):
            return False

        try:
            if safe_md.stat().st_size == 0:
                return False
            md_text = safe_md.read_text(encoding="utf-8")
            if not md_text.strip():
                return False
            meta_raw = safe_meta.read_text(encoding="utf-8")
            meta = json.loads(meta_raw)
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            return False

        if not isinstance(meta, dict):
            return False

        if meta.get("block_index") != block_index:
            return False

        for required_key in ("input_sha256", "prompt_sha256", "model", "output_sha256"):
            val = meta.get(required_key)
            if not isinstance(val, str) or not val.strip():
                return False

        actual_output_sha = sha256_file(safe_md)
        expected_output_sha = str(meta["output_sha256"]).strip()
        if actual_output_sha.lower() != expected_output_sha.lower():
            return False

        return True

    def _is_valid_markdown_file(self, rel_path: str) -> bool:
        """Verifica che un file Markdown in session_dir esista e contenga testo UTF-8 non vuoto."""
        target = self.session_dir / rel_path
        try:
            safe_target = ensure_safe_child_path(self.session_dir, target)
        except StateValidationError:
            return False

        if not safe_target.is_file():
            return False
        try:
            text = safe_target.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return False
        return bool(text.strip())

    def validate_resume_artifacts(self) -> SessionContext:
        """Riconcilia state.json con gli artefatti realmente presenti e validi su disco senza cancellare nulla."""
        with self._lock:
            context = self.load()
            audio_dir = self.session_dir / "audio"

            declared_transcribed = sorted(set(context.transcribed_chunks))
            declared_normalized = sorted(set(context.normalized_chunks))
            declared_recorded = sorted(set(context.recorded_chunks))
            declared_mapped = sorted(set(context.mapped_blocks))

            # Artefatti audio presenti su disco ma non dichiarati: il chunk ancora aperto quando il
            # processo è morto (raw) o un chunk normalizzato prima dell'aggiornamento di state.json.
            undeclared_raw, undeclared_norm = self._scan_undeclared_audio(
                audio_dir,
                set(declared_recorded) | set(declared_normalized) | set(declared_transcribed),
            )
            if undeclared_norm:
                self._logger.warning(
                    "Chunk normalizzati non dichiarati recuperati da disco: %s", sorted(undeclared_norm)
                )
                declared_normalized = sorted(set(declared_normalized) | undeclared_norm)
            if undeclared_raw:
                self._logger.warning(
                    "Chunk audio grezzi non dichiarati (chunk aperto) recuperati da disco: %s",
                    sorted(undeclared_raw),
                )
                declared_recorded = sorted(set(declared_recorded) | undeclared_raw | undeclared_norm)
            elif undeclared_norm:
                declared_recorded = sorted(set(declared_recorded) | undeclared_norm)

            valid_transcribed: list[int] = [
                idx
                for idx in declared_transcribed
                if self._is_valid_transcript_artifact(idx)
            ]
            valid_transcribed_set = set(valid_transcribed)

            valid_normalized: list[int] = []
            for idx in sorted(set(declared_normalized) | valid_transcribed_set):
                norm_io = audio_dir / f"chunk_{idx:03d}_io_16k.wav"
                norm_others = audio_dir / f"chunk_{idx:03d}_altri_16k.wav"
                if idx in valid_transcribed_set or self._is_valid_wav_pair(
                    norm_io, norm_others
                ):
                    if idx in declared_normalized or idx in valid_transcribed_set:
                        valid_normalized.append(idx)
            valid_normalized_set = set(valid_normalized)

            valid_recorded: list[int] = []
            for idx in sorted(
                set(declared_recorded) | valid_normalized_set | valid_transcribed_set
            ):
                raw_io = audio_dir / f"chunk_{idx:03d}_io_raw.wav"
                raw_others = audio_dir / f"chunk_{idx:03d}_altri_raw.wav"
                if (
                    idx in valid_normalized_set
                    or idx in valid_transcribed_set
                    or self._is_valid_wav_pair(raw_io, raw_others)
                ):
                    if (
                        idx in declared_recorded
                        or idx in valid_normalized_set
                        or idx in valid_transcribed_set
                    ):
                        valid_recorded.append(idx)
            valid_recorded_set = set(valid_recorded)

            valid_audio_deleted: list[int] = [
                idx
                for idx in sorted(set(context.audio_deleted_chunks))
                if idx in valid_transcribed_set
            ]

            context.recorded_chunks = valid_recorded
            context.normalized_chunks = valid_normalized
            context.transcribed_chunks = valid_transcribed
            context.audio_deleted_chunks = valid_audio_deleted

            all_expected_chunks = (
                valid_recorded_set
                | valid_normalized_set
                | set(declared_transcribed)
            )
            transcription_complete = bool(valid_transcribed_set) and (
                valid_transcribed_set == all_expected_chunks
            )

            if not transcription_complete:
                context.mapped_blocks = []
                context.detailed_reduce_completed = False
                context.email_reduce_completed = False
                context.final_docx_path = None
                context.final_docx_verified = False
                if valid_normalized_set and valid_normalized_set >= valid_recorded_set:
                    context.stage = Stage.TRANSCRIBING_FINAL
                else:
                    context.stage = Stage.FINALIZING_AUDIO
                self.save(context)
                return context

            # Verifica blocchi MAP in ordine sequenziale
            valid_mapped: list[int] = []
            for block_idx in declared_mapped:
                if self._is_valid_map_artifact(block_idx):
                    valid_mapped.append(block_idx)
                else:
                    break
            context.mapped_blocks = valid_mapped

            maps_complete = bool(valid_mapped) and (valid_mapped == declared_mapped)
            if not maps_complete:
                context.detailed_reduce_completed = False
                context.email_reduce_completed = False
                context.final_docx_path = None
                context.final_docx_verified = False
                context.stage = Stage.SUMMARIZING_MAP
                self.save(context)
                return context

            # Verifica REDUCE dettagliato
            if context.detailed_reduce_completed and not self._is_valid_markdown_file(
                "reduce/detailed_minutes.md"
            ):
                context.detailed_reduce_completed = False

            if not context.detailed_reduce_completed:
                context.email_reduce_completed = False
                context.final_docx_path = None
                context.final_docx_verified = False
                context.stage = Stage.SUMMARIZING_REDUCE
                self.save(context)
                return context

            # Verifica REDUCE email
            if context.email_reduce_completed and not self._is_valid_markdown_file(
                "reduce/recap_email.md"
            ):
                context.email_reduce_completed = False

            if not context.email_reduce_completed:
                context.final_docx_path = None
                context.final_docx_verified = False
                context.stage = Stage.SUMMARIZING_REDUCE
                self.save(context)
                return context

            # Verifica DOCX finale
            docx_valid = False
            if context.final_docx_path is not None:
                try:
                    docx_candidate = context.final_docx_path
                    if (
                        not _is_reparse_or_symlink(docx_candidate)
                        and docx_candidate.is_file()
                        and docx_candidate.stat().st_size >= DOCX_MIN_SIZE_BYTES
                    ):
                        docx_valid = True
                except OSError:
                    docx_valid = False

            if not docx_valid:
                context.final_docx_path = None
                context.final_docx_verified = False
                context.stage = Stage.BUILDING_DOCX
            elif not context.final_docx_verified:
                context.stage = Stage.VERIFYING_DOCX
            else:
                context.stage = Stage.CLEANING_UP

            self.save(context)
            return context

    def _scan_undeclared_audio(
        self,
        audio_dir: Path,
        declared: set[int],
    ) -> tuple[set[int], set[int]]:
        """Restituisce gli indici dei chunk raw e normalizzati validi su disco ma non dichiarati."""
        raw_found: set[int] = set()
        norm_found: set[int] = set()
        if not audio_dir.is_dir():
            return raw_found, norm_found
        for entry in audio_dir.iterdir():
            raw_match = _RAW_IO_RE.match(entry.name)
            if raw_match is not None:
                idx = int(raw_match.group("idx"))
                if idx not in declared and self._is_valid_wav_pair(
                    entry, audio_dir / f"chunk_{idx:03d}_altri_raw.wav"
                ):
                    raw_found.add(idx)
                continue
            norm_match = _NORM_IO_RE.match(entry.name)
            if norm_match is not None:
                idx = int(norm_match.group("idx"))
                if idx not in declared and self._is_valid_wav_pair(
                    entry, audio_dir / f"chunk_{idx:03d}_altri_16k.wav"
                ):
                    norm_found.add(idx)
        return raw_found - norm_found, norm_found

    def cleanup_session_files(self, final_docx: Path) -> None:
        """Valida il DOCX finale e rimuove in sicurezza la directory temporanea della sessione."""
        with self._lock:
            ensure_safe_child_path(self._temp_root, self.session_dir)

            if ".." in final_docx.parts or _is_reparse_or_symlink(final_docx):
                raise StateValidationError(
                    f"Percorso DOCX non sicuro: {final_docx}"
                )

            if not final_docx.exists() or not final_docx.is_file():
                raise StateValidationError(
                    f"Il documento Word finale non esiste: {final_docx}"
                )

            try:
                docx_size = final_docx.stat().st_size
            except OSError as exc:
                raise StateValidationError(
                    f"Impossibile verificare la dimensione del DOCX {final_docx}: {exc}"
                ) from exc

            if docx_size < DOCX_MIN_SIZE_BYTES:
                raise StateValidationError(
                    f"Dimensione DOCX ({docx_size} byte) inferiore alla soglia minima "
                    f"({DOCX_MIN_SIZE_BYTES} byte)."
                )

            docx_resolved = final_docx.resolve()
            if (
                docx_resolved == self.session_dir
                or self.session_dir in docx_resolved.parents
            ):
                raise StateValidationError(
                    "Il documento Word finale non può risiedere dentro la directory temporanea della sessione."
                )

            effective_output_root = _resolve_effective_output_root(
                final_docx, self.session_dir, self._explicit_output_root
            )
            ensure_safe_child_path(effective_output_root, final_docx)

            if self.state_path.is_file():
                context = self.load()
                context.final_docx_path = docx_resolved
                context.final_docx_verified = True
                context.stage = Stage.CLEANING_UP
                context.ended_at = context.ended_at or now_tz()
                self.save(context)

            # Il lock va rilasciato prima: su Windows un file aperto blocca la rimozione della cartella
            self._owner_lock.release()
            try:
                shutil.rmtree(self.session_dir)
            except OSError as exc:
                self.record_error(Stage.CLEANING_UP, exc, recoverable=True)
                cleanup_err = ScribeError(
                    f"Rimozione directory temporanea fallita (exit_code={EXIT_CLEANUP_FAILED}): {exc}"
                )
                setattr(cleanup_err, "exit_code", EXIT_CLEANUP_FAILED)
                raise cleanup_err from exc

            self._closed = True
            release_state_manager(self.session_dir)
            self._logger.info(
                "Cleanup completato per session_dir=%s (docx=%s)",
                sanitize_for_log(self.session_dir.name),
                sanitize_for_log(docx_resolved.name),
            )


_REGISTRY: dict[Path, StateManager] = {}
_REGISTRY_LOCK = threading.Lock()


def get_state_manager(
    session_dir: Path,
    *,
    temp_root: Path | None = None,
    output_root: Path | None = None,
) -> StateManager:
    """Restituisce l'unico StateManager (e quindi l'unico lock) associato a session_dir."""
    key = session_dir.resolve()
    with _REGISTRY_LOCK:
        existing = _REGISTRY.get(key)
        if existing is not None and not existing.closed:
            return existing
        manager = StateManager(key, temp_root=temp_root, output_root=output_root)
        _REGISTRY[manager.session_dir] = manager
        return manager


def release_state_manager(session_dir: Path) -> None:
    """Rimuove dal registro lo StateManager di una sessione conclusa."""
    with _REGISTRY_LOCK:
        _REGISTRY.pop(session_dir.resolve(), None)
