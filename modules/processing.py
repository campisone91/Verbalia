from __future__ import annotations

import inspect
import re
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

from config import (
    CHUNK_OVERLAP_SEC,
    EXIT_APP_ERROR,
    EXIT_CLEANUP_FAILED,
    EXIT_SUCCESS,
    EXIT_UNRECOVERABLE_SESSION,
    OLLAMA_UNLOAD_AFTER_PIPELINE,
    PROCESSING_MAX_ATTEMPTS,
    PROCESSING_RETRY_BASE_DELAY_SEC,
)
from modules.audio_utils import get_wav_duration_sec, normalize_wav_with_ffmpeg
from modules.contracts import (
    AudioChunk,
    ChunkStatus,
    ResumeError,
    SessionContext,
    SessionLockedError,
    SessionMode,
    ShutdownRequested,
    Stage,
    StateValidationError,
    TranscriptionError,
    now_tz,
)
from modules.docx_builder import MeetingDocxBuilder, validate_docx_file
from modules.logging_setup import bind_log_session, get_logger
from modules.recorder import prevent_system_sleep, restore_system_sleep
from modules.session_runtime import (
    EVENT_JOB,
    EVENT_LAST_OUTPUT,
    EVENT_NOTIFY,
    EventBus,
    SessionRuntime,
    SessionStageReporter,
    StageEvent,
)
from modules.state_manager import get_state_manager, is_session_locked
from modules.transcriber import RollingTranscriber, WhisperEngine
from modules.transcript_utils import merge_chunk_transcripts, resolve_glossary

if TYPE_CHECKING:
    from modules.settings import AppSettings
    from modules.state_manager import StateManager

logger = get_logger("processing")

_HEADER_INTERVAL_RE = re.compile(r"^# Intervallo: (?P<start>\d{2,}:\d{2}:\d{2}) - (?P<end>\d{2,}:\d{2}:\d{2})$")


class SummarizerLike(Protocol):
    def run_map_phase(self, transcript_path: Path, metadata: Any, session_dir: Path) -> list[Path]: ...

    def run_reduce_detailed(self, map_paths: Any, metadata: Any, session_dir: Path) -> Path: ...

    def run_reduce_email(self, detailed_minutes_path: Path, metadata: Any, session_dir: Path) -> Path: ...

    def unload_model(self) -> None: ...


class _PolicyLock:
    """Lock la cui identità effettiva dipende dalla policy: proprio in parallelo, condiviso in sequenziale."""

    def __init__(self, arbiter: AiResourceArbiter, name: str) -> None:
        self._arbiter = arbiter
        self._own = threading.RLock()
        self._name = name
        self._held = threading.local()

    def acquire(self, blocking: bool = True, timeout: float = -1) -> bool:
        lock = self._own if self._arbiter.parallel else self._arbiter.shared_lock
        acquired = lock.acquire(blocking, timeout)
        if acquired:
            stack = getattr(self._held, "stack", None)
            if stack is None:
                stack = []
                self._held.stack = stack
            stack.append(lock)
        return acquired

    def release(self) -> None:
        stack = getattr(self._held, "stack", None)
        if not stack:
            raise RuntimeError(f"Release di {self._name} senza acquire corrispondente.")
        stack.pop().release()

    def __enter__(self) -> _PolicyLock:
        self.acquire()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.release()


class AiResourceArbiter:
    """Arbitraggio Whisper/LLM: parallelo se il modello Ollama risulta su GPU, altrimenti sequenziale.

    Con la policy "auto" la scelta viene rivalutata a ogni probe di /api/ps; "sequential" e
    "parallel" la fissano. In sequenziale whisper_lock e llm_lock condividono lo stesso lock,
    come il vecchio ai_lock.
    """

    def __init__(self, policy: str = "auto") -> None:
        self.policy = policy if policy in ("auto", "sequential", "parallel") else "auto"
        self.shared_lock = threading.RLock()
        self._parallel = self.policy == "parallel"
        self.last_gpu_percent: float | None = None
        self.whisper_lock = _PolicyLock(self, "whisper_lock")
        self.llm_lock = _PolicyLock(self, "llm_lock")

    @property
    def parallel(self) -> bool:
        return self._parallel

    def refresh(self, gpu_percent: float | None) -> None:
        """Aggiorna la policy auto con la quota del modello LLM in VRAM (None = modello non caricato)."""
        if gpu_percent is not None:
            self.last_gpu_percent = float(gpu_percent)
        if self.policy != "auto" or gpu_percent is None:
            return
        parallel = float(gpu_percent) > 0.0
        if parallel != self._parallel:
            logger.info(
                "Policy AI auto: %s (LLM su GPU al %.0f%%).",
                "Whisper e LLM in parallelo" if parallel else "esecuzione sequenziale",
                float(gpu_percent),
            )
        self._parallel = parallel


@dataclass
class ProcessingJob:
    """Elaborazione di una sessione: dal disco oppure passata in consegna dalla registrazione live."""

    session_id: str
    session_dir: Path
    enqueued_at: float = field(default_factory=time.monotonic)
    attempts: int = 0
    not_before: float = 0.0
    runtime: SessionRuntime | None = None


class ProcessingQueue:
    """Coda FIFO dei job; un job in attesa di retry non blocca quelli successivi."""

    def __init__(self) -> None:
        self._items: list[ProcessingJob] = []
        self._cond = threading.Condition()

    def put(self, job: ProcessingJob) -> None:
        with self._cond:
            if any(item.session_id == job.session_id for item in self._items):
                return
            self._items.append(job)
            self._cond.notify_all()

    def get(self, timeout: float) -> ProcessingJob | None:
        deadline = time.monotonic() + max(0.0, timeout)
        with self._cond:
            while True:
                now = time.monotonic()
                ready = [item for item in self._items if item.not_before <= now]
                if ready:
                    job = ready[0]
                    self._items.remove(job)
                    return job
                remaining = deadline - now
                if remaining <= 0:
                    return None
                upcoming = min((item.not_before - now for item in self._items), default=remaining)
                self._cond.wait(timeout=max(0.05, min(remaining, upcoming)))

    def contains(self, session_id: str) -> bool:
        with self._cond:
            return any(item.session_id == session_id for item in self._items)

    def remove(self, session_id: str) -> bool:
        with self._cond:
            before = len(self._items)
            self._items = [item for item in self._items if item.session_id != session_id]
            return len(self._items) != before

    def __len__(self) -> int:
        with self._cond:
            return len(self._items)

    def snapshot(self) -> list[ProcessingJob]:
        with self._cond:
            return list(self._items)


def _call_with_supported_kwargs(fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    """Invoca fn passando solo i kwargs che accetta (i summarizer di test hanno firme ridotte)."""
    try:
        params = inspect.signature(fn).parameters
    except (TypeError, ValueError):
        return fn(*args, **kwargs)
    if any(p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values()):
        return fn(*args, **kwargs)
    return fn(*args, **{k: v for k, v in kwargs.items() if k in params})


def _parse_hms(value: str) -> float:
    hours, minutes, seconds = (int(part) for part in value.split(":"))
    return float(hours * 3600 + minutes * 60 + seconds)


def _transcript_interval(path: Path) -> tuple[float, float] | None:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError):
        return None
    if len(lines) < 2:
        return None
    match = _HEADER_INTERVAL_RE.match(lines[1].strip())
    if match is None:
        return None
    return _parse_hms(match.group("start")), _parse_hms(match.group("end"))


class SessionProcessor:
    """Elabora una sessione da disco: normalizzazione residua, trascrizione, MAP, REDUCE, DOCX, cleanup.

    Ogni passo è idempotente e riparte dal primo incompleto; gli errori restano confinati
    alla sessione elaborata (nessun riferimento a recorder o sessioni di altri).
    """

    def __init__(
        self,
        *,
        engine: WhisperEngine,
        arbiter: AiResourceArbiter,
        bus: EventBus,
        shutdown_event: threading.Event,
        docx_builder: MeetingDocxBuilder,
        summarizer_provider: Callable[[SessionRuntime], SummarizerLike],
        output_dir_provider: Callable[[SessionRuntime], Path],
        ffmpeg_provider: Callable[[], Path],
        gpu_probe: Callable[[str | None], dict[str, object]] | None = None,
        more_jobs_pending: Callable[[], bool] | None = None,
    ) -> None:
        self._engine = engine
        self._arbiter = arbiter
        self._bus = bus
        self._shutdown_event = shutdown_event
        self._docx_builder = docx_builder
        self._summarizer_provider = summarizer_provider
        self._output_dir_provider = output_dir_provider
        self._ffmpeg_provider = ffmpeg_provider
        self._gpu_probe = gpu_probe
        self._more_jobs_pending = more_jobs_pending
        self._current_summarizer: SummarizerLike | None = None
        self._gpu_hint_notified = False

    def cancel_current(self) -> None:
        """Annulla la chiamata Ollama in streaming del job in corso (shutdown)."""
        summarizer = self._current_summarizer
        cancel = getattr(summarizer, "cancel_active_request", None)
        if callable(cancel):
            cancel()

    def _publish_job(self, runtime: SessionRuntime, **fields: object) -> None:
        self._bus.publish(StageEvent(kind=EVENT_JOB, session_id=runtime.session_id, fields=dict(fields)))

    def _notify(self, title: str, message: str) -> None:
        self._bus.publish(StageEvent(kind=EVENT_NOTIFY, detail=message, fields={"title": title}))

    # ------------------------------------------------------------ preparazione
    def _chunk_start_offset(self, runtime: SessionRuntime, chunk_idx: int) -> float:
        """Offset del chunk nella call: dall'header del transcript precedente, altrimenti stimato."""
        if chunk_idx <= 0:
            return 0.0
        transcripts_dir = runtime.session_dir / "transcripts"
        previous = _transcript_interval(transcripts_dir / f"chunk_{chunk_idx - 1:03d}.txt")
        if previous is not None:
            return max(0.0, previous[1] - float(CHUNK_OVERLAP_SEC))
        chunk_duration = int(runtime.settings.audio.chunk_duration_sec)
        return float(chunk_idx * max(1, chunk_duration - CHUNK_OVERLAP_SEC))

    def prepare_job_from_disk(
        self,
        runtime: SessionRuntime,
        transcriber: RollingTranscriber | None = None,
    ) -> RollingTranscriber | None:
        """Normalizza i chunk raw residui e accoda a Whisper quelli non ancora trascritti.

        Con il client della registrazione live vengono accodati solo i chunk che non gli
        sono già stati inviati (ad esempio quelli la cui normalizzazione era fallita).
        """
        sm = runtime.state_manager
        context = sm.load()
        audio_dir = runtime.session_dir / "audio"
        ffmpeg_path = self._ffmpeg_provider()

        residual = [
            idx
            for idx in sorted(context.recorded_chunks)
            if idx not in context.normalized_chunks and idx not in context.transcribed_chunks
        ]
        for chunk_idx in residual:
            if self._shutdown_event.is_set():
                raise ShutdownRequested("Normalizzazione dei chunk residui interrotta.")
            raw_io = audio_dir / f"chunk_{chunk_idx:03d}_io_raw.wav"
            raw_others = audio_dir / f"chunk_{chunk_idx:03d}_altri_raw.wav"
            if not (raw_io.is_file() and raw_others.is_file()):
                continue
            runtime.reporter.set_stage(
                Stage.TRANSCRIBING_FINAL,
                f"Normalizzazione chunk residuo {chunk_idx + 1}",
            )
            normalize_wav_with_ffmpeg(ffmpeg_path, raw_io, audio_dir / f"chunk_{chunk_idx:03d}_io_16k.wav")
            normalize_wav_with_ffmpeg(
                ffmpeg_path, raw_others, audio_dir / f"chunk_{chunk_idx:03d}_altri_16k.wav"
            )

            def _add_norm(c: SessionContext, idx: int = chunk_idx) -> None:
                if idx not in c.normalized_chunks:
                    c.normalized_chunks.append(idx)
                    c.normalized_chunks.sort()

            context = sm.mutate(_add_norm)

        already_submitted = transcriber.submitted_indices if transcriber is not None else set()
        pending = [
            idx
            for idx in sorted(context.normalized_chunks)
            if idx not in context.transcribed_chunks and idx not in already_submitted
        ]
        if not pending:
            return transcriber

        if transcriber is None:
            transcriber = RollingTranscriber(
                session_dir=runtime.session_dir,
                state_manager=sm,
                stage_reporter=runtime.reporter,
                engine=self._engine,
                glossary=resolve_glossary(runtime.settings.output.glossary),
                session_id=runtime.session_id,
                priority=WhisperEngine.PRIORITY_PROCESSING,
            )
            runtime.transcriber = transcriber
        transcriber.start()
        for chunk_idx in pending:
            norm_io = audio_dir / f"chunk_{chunk_idx:03d}_io_16k.wav"
            norm_others = audio_dir / f"chunk_{chunk_idx:03d}_altri_16k.wav"
            start_offset = self._chunk_start_offset(runtime, chunk_idx)
            duration = (
                get_wav_duration_sec(norm_others)
                if norm_others.is_file()
                else float(runtime.settings.audio.chunk_duration_sec)
            )
            transcriber.submit(
                AudioChunk(
                    index=chunk_idx,
                    session_id=runtime.session_id,
                    start_offset_sec=start_offset,
                    end_offset_sec=start_offset + duration,
                    io_wav_path=norm_io,
                    others_wav_path=norm_others,
                    is_final=(chunk_idx == pending[-1]),
                    created_at=now_tz(),
                    status=ChunkStatus.NORMALIZED,
                )
            )
        return transcriber

    # ----------------------------------------------------------------- job
    def process(self, runtime: SessionRuntime) -> int:
        """Esegue l'intero job della sessione; solleva ShutdownRequested se interrotto dall'arresto."""
        with bind_log_session(runtime.session_id):
            try:
                return self._process(runtime)
            except ShutdownRequested:
                logger.info("Elaborazione della sessione interrotta dall'arresto: verrà ripresa.")
                self._publish_job(runtime, status="interrupted", detail="Interrotta allo spegnimento")
                raise
            except Exception as exc:
                exit_code = int(getattr(exc, "exit_code", EXIT_APP_ERROR))
                if exit_code == EXIT_CLEANUP_FAILED:
                    self._notify(
                        "Attenzione: Cleanup parziale",
                        f"Il file Word è pronto ma la cartella {runtime.session_dir.name} non è stata rimossa.",
                    )
                    self._publish_job(runtime, status="completed", error=str(exc)[:500])
                    return EXIT_CLEANUP_FAILED
                self.handle_pipeline_error(runtime, exc)
                return exit_code
            finally:
                self._current_summarizer = None

    def _process(self, runtime: SessionRuntime) -> int:
        sm = runtime.state_manager
        reporter = runtime.reporter
        session_dir = runtime.session_dir
        if reporter.stage != Stage.QUEUED:
            reporter.rebase(Stage.QUEUED, "Sessione presa in carico per l'elaborazione")

        transcriber = runtime.transcriber
        if transcriber is not None:
            # Consegna dalla registrazione live: si attende la normalizzazione dell'ultimo chunk,
            # dopo la quale tutti i chunk normalizzati sono già stati inviati al client live.
            if runtime.recorder is not None:
                runtime.recorder.wait_normalized(self._shutdown_event)
            transcriber.set_priority(WhisperEngine.PRIORITY_PROCESSING)
        transcriber = self.prepare_job_from_disk(runtime, transcriber)

        if transcriber is not None:
            reporter.set_stage(Stage.TRANSCRIBING_FINAL, "Completamento trascrizione ultimi chunk")
            transcriber.drain(self._shutdown_event)
        runtime.recorder = None

        transcripts_dir = session_dir / "transcripts"
        transcripts_dir.mkdir(parents=True, exist_ok=True)
        merged_transcript_path = transcripts_dir / "merged_transcript.txt"
        chunk_files = sorted(transcripts_dir.glob("chunk_*.txt"))
        if not chunk_files and not merged_transcript_path.is_file():
            raise TranscriptionError(
                f"Nessun file transcript trovato nella sessione {session_dir.name}."
            )
        if chunk_files:
            merge_chunk_transcripts(chunk_files, merged_transcript_path)

        context = sm.load()
        if context.ended_at is None:
            # Sessione recuperata dopo un crash: la durata è quella dell'audio registrato
            last_interval = _transcript_interval(chunk_files[-1]) if chunk_files else None
            audio_end = last_interval[1] if last_interval is not None else 0.0
            estimated_end = context.started_at + timedelta(seconds=max(1.0, audio_end))

            def _set_end(c: SessionContext) -> None:
                c.ended_at = estimated_end

            context = sm.mutate(_set_end)

        self._check_shutdown()
        summarizer = self._summarizer_provider(runtime)
        self._current_summarizer = summarizer
        reporter.set_stage(Stage.SUMMARIZING_MAP, "Analisi MAP blocchi conversazione")
        map_paths = summarizer.run_map_phase(
            transcript_path=merged_transcript_path,
            metadata=context.metadata,
            session_dir=session_dir,
        )
        self._after_first_inference(runtime)

        reporter.set_stage(Stage.SUMMARIZING_REDUCE, "Generazione verbale dettagliato e bozza mail")
        context = sm.load()
        detailed_minutes_path = session_dir / "reduce" / "detailed_minutes.md"
        if not (context.detailed_reduce_completed and detailed_minutes_path.is_file()):
            detailed_minutes_path = _call_with_supported_kwargs(
                summarizer.run_reduce_detailed,
                map_paths=map_paths,
                metadata=context.metadata,
                session_dir=session_dir,
                started_at=context.started_at,
                ended_at=context.ended_at,
            )

        recap_email_path = session_dir / "reduce" / "recap_email.md"
        if not (context.email_reduce_completed and recap_email_path.is_file()):
            recap_email_path = summarizer.run_reduce_email(
                detailed_minutes_path=detailed_minutes_path,
                metadata=context.metadata,
                session_dir=session_dir,
            )

        more_pending = self._more_jobs_pending() if self._more_jobs_pending is not None else False
        if OLLAMA_UNLOAD_AFTER_PIPELINE and not more_pending:
            summarizer.unload_model()

        reporter.set_stage(Stage.BUILDING_DOCX, "Costruzione documento Word")
        context = sm.load()
        ended_at = context.ended_at or now_tz()
        output_settings = runtime.settings.output
        final_docx_path = context.final_docx_path
        if final_docx_path is None or not final_docx_path.is_file():
            final_docx_path = _call_with_supported_kwargs(
                self._docx_builder.build,
                metadata=context.metadata,
                started_at=context.started_at,
                ended_at=ended_at,
                recap_email_path=recap_email_path,
                detailed_minutes_path=detailed_minutes_path,
                output_dir=self._output_dir_provider(runtime),
                transcript_path=merged_transcript_path if output_settings.include_transcript else None,
                user_display_name=output_settings.user_display_name,
                organize_by_day=output_settings.organize_by_day,
            )

        reporter.set_stage(Stage.VERIFYING_DOCX, f"Verifica integrità {final_docx_path.name}")
        validate_docx_file(final_docx_path)

        def _mark_verified(c: SessionContext) -> None:
            c.final_docx_path = final_docx_path.resolve()
            c.final_docx_verified = True

        sm.mutate(_mark_verified)

        reporter.set_stage(Stage.CLEANING_UP, "Pulizia artefatti temporanei di sessione")
        self._publish_job(runtime, docx_path=str(final_docx_path.resolve()))
        sm.cleanup_session_files(final_docx_path)

        self._bus.publish(StageEvent(kind=EVENT_LAST_OUTPUT, fields={"path": str(final_docx_path)}))
        self._publish_job(
            runtime,
            status="completed",
            stage=Stage.COMPLETED.value,
            detail=f"Verbale creato: {final_docx_path.name}",
            processing_ended_at=now_tz(),
            error=None,
        )
        self._notify("Verbale Teams Pronto", f"File generato con successo: {final_docx_path.name}")
        return EXIT_SUCCESS

    def _check_shutdown(self) -> None:
        if self._shutdown_event.is_set():
            raise ShutdownRequested("Elaborazione interrotta: arresto richiesto.")

    def _after_first_inference(self, runtime: SessionRuntime) -> None:
        """Dopo il MAP il modello è in memoria: si misura la quota su GPU e si aggiorna la policy."""
        if self._gpu_probe is None:
            return
        try:
            probe = self._gpu_probe(runtime.settings.ollama.model)
        except Exception as exc:
            logger.debug("Probe GPU Ollama non riuscito: %s", exc)
            return
        gpu_percent = probe.get("gpu_percent")
        gpu_value = float(gpu_percent) if isinstance(gpu_percent, (int, float)) else None
        self._arbiter.refresh(gpu_value)
        self._publish_job(runtime, llm_on_gpu=gpu_value)
        if gpu_value is not None:
            logger.info("Modello LLM caricato su GPU al %.0f%%.", gpu_value)
        if gpu_value == 0.0 and not self._gpu_hint_notified:
            self._gpu_hint_notified = True
            hint = str(probe.get("hint") or "Il modello LLM è in esecuzione solo su CPU.")
            self._notify("LLM senza GPU", hint)

    def handle_pipeline_error(self, runtime: SessionRuntime, exc: Exception) -> None:
        """Registra l'errore nella sola sessione interessata preservando gli artefatti per il resume."""
        logger.error("Errore nella pipeline di sessione: %s", exc, exc_info=True)
        sm = runtime.state_manager
        current_stage = runtime.reporter.stage
        if not sm.closed:
            for attempt in range(5):
                try:
                    sm.record_error(current_stage, exc, recoverable=True)
                    break
                except OSError as st_exc:
                    if attempt + 1 < 5:
                        time.sleep(0.03 * (attempt + 1))
                    else:
                        logger.warning("Impossibile salvare record_error: %s", st_exc)
                except Exception as st_exc:
                    logger.warning("Impossibile salvare record_error: %s", st_exc)
                    break
        runtime.reporter.mark_error(f"Errore: {type(exc).__name__}")
        self._publish_job(
            runtime,
            status="failed",
            error=f"{type(exc).__name__}: {exc}"[:500],
        )
        self._notify(
            "Errore Elaborazione Sessione",
            f"I dati sono stati preservati per il ripristino: {runtime.session_dir.name}",
        )


def build_runtime_from_disk(
    session_dir: Path,
    *,
    settings: AppSettings,
    bus: EventBus | None,
    temp_root: Path | None,
    output_root: Path | None,
    acquire_lock: bool = True,
) -> SessionRuntime:
    """Riconcilia una sessione su disco e costruisce il suo SessionRuntime.

    Solleva SessionLockedError se la sessione è già in uso, ResumeError se non contiene nulla
    di recuperabile e StateValidationError se state.json non è valido.
    """
    sm: StateManager = get_state_manager(session_dir, temp_root=temp_root, output_root=output_root)
    if acquire_lock:
        sm.acquire_owner_lock()
    try:
        context = sm.validate_resume_artifacts()
        audio_dir = sm.session_dir / "audio"
        transcripts_dir = sm.session_dir / "transcripts"
        has_any_data = bool(
            context.recorded_chunks
            or context.normalized_chunks
            or context.transcribed_chunks
            or list(audio_dir.glob("chunk_*.wav"))
            or list(transcripts_dir.glob("chunk_*.txt"))
            or (transcripts_dir / "merged_transcript.txt").is_file()
        )
        if not has_any_data:
            raise ResumeError(f"Nessun artefatto recuperabile nella sessione {sm.session_dir.name}")
    except BaseException:
        if acquire_lock:
            sm.release_owner_lock()
        raise
    reporter = SessionStageReporter(context.session_id, sm, bus, initial_stage=context.stage)
    return SessionRuntime(
        session_id=context.session_id,
        session_dir=sm.session_dir,
        state_manager=sm,
        reporter=reporter,
        mode=context.mode,
        metadata=context.metadata,
        started_at=context.started_at,
        settings=settings,
        ended_at=context.ended_at,
    )


def discover_recoverable_sessions(temp_dir: Path) -> list[Path]:
    """Cartelle di sessione con state.json, in ordine cronologico (il nome inizia con data e ora)."""
    if not temp_dir.is_dir():
        return []
    return sorted(
        path.parent for path in temp_dir.glob("session_*/state.json") if path.parent.is_dir()
    )


class ProcessingWorker(threading.Thread):
    """Unico worker che serve la coda: le elaborazioni restano sequenziali (RAM e GPU)."""

    def __init__(
        self,
        *,
        queue: ProcessingQueue,
        processor: SessionProcessor,
        bus: EventBus,
        shutdown_event: threading.Event,
        runtime_factory: Callable[[Path], SessionRuntime],
        max_attempts: int = PROCESSING_MAX_ATTEMPTS,
        retry_base_delay_sec: float = float(PROCESSING_RETRY_BASE_DELAY_SEC),
    ) -> None:
        super().__init__(name="ProcessingWorker", daemon=True)
        self._queue = queue
        self._processor = processor
        self._bus = bus
        self._shutdown_event = shutdown_event
        self._runtime_factory = runtime_factory
        self._max_attempts = max(1, int(max_attempts))
        self._retry_base_delay_sec = max(0.0, float(retry_base_delay_sec))
        self._current: ProcessingJob | None = None
        self._lock = threading.Lock()
        self._sleep_blocked = False
        self.idle_event = threading.Event()
        self.idle_event.set()

    @property
    def current_session_id(self) -> str | None:
        with self._lock:
            return self._current.session_id if self._current is not None else None

    def run(self) -> None:
        try:
            while not self._shutdown_event.is_set():
                job = self._queue.get(timeout=0.5)
                if job is None:
                    if self._sleep_blocked and len(self._queue) == 0:
                        restore_system_sleep()
                        self._sleep_blocked = False
                    continue
                self.idle_event.clear()
                with self._lock:
                    self._current = job
                try:
                    self.run_job(job)
                finally:
                    with self._lock:
                        self._current = None
                    if len(self._queue) == 0:
                        self.idle_event.set()
        finally:
            if self._sleep_blocked:
                restore_system_sleep()
                self._sleep_blocked = False

    def _publish(self, session_id: str, **fields: object) -> None:
        self._bus.publish(StageEvent(kind=EVENT_JOB, session_id=session_id, fields=dict(fields)))

    def run_job(self, job: ProcessingJob, *, retry: bool = True) -> int:
        """Esegue un job nel thread corrente.

        Con retry=False (esecuzioni sincrone di --resume e --test-pipeline) viene fatto un solo
        tentativo e il blocco dello standby è rilasciato a fine job.
        """
        if not self._sleep_blocked:
            # ES_SYSTEM_REQUIRED vale per il thread che lo imposta: il worker lo tiene finché ha lavoro
            prevent_system_sleep()
            self._sleep_blocked = True
        try:
            return self._run_job(job, retry=retry)
        finally:
            if not retry and self._sleep_blocked:
                restore_system_sleep()
                self._sleep_blocked = False

    def _run_job(self, job: ProcessingJob, *, retry: bool) -> int:
        runtime = job.runtime
        try:
            if runtime is None:
                runtime = self._runtime_factory(job.session_dir)
        except SessionLockedError:
            logger.info("Sessione %s in uso da un altro processo.", job.session_dir.name)
            if retry:
                job.not_before = time.monotonic() + 30.0
                self._queue.put(job)
            return EXIT_APP_ERROR
        except (ResumeError, StateValidationError, OSError) as exc:
            logger.error("Sessione non recuperabile (%s): %s", job.session_dir.name, exc)
            self._publish(job.session_id, status="parked", error=f"Sessione non recuperabile: {exc}"[:500])
            return EXIT_UNRECOVERABLE_SESSION

        runtime.attempts = job.attempts + 1
        self._publish(
            runtime.session_id,
            status="processing",
            attempts=runtime.attempts,
            processing_started_at=now_tz(),
            next_retry_at=None,
        )
        try:
            rc = self._processor.process(runtime)
        except ShutdownRequested:
            runtime.state_manager.release_owner_lock()
            return EXIT_APP_ERROR
        except Exception as exc:  # pragma: no cover - process gestisce già gli errori attesi
            logger.error("Errore imprevisto nel worker di elaborazione: %s", exc, exc_info=True)
            rc = EXIT_APP_ERROR

        if rc in (EXIT_SUCCESS, EXIT_CLEANUP_FAILED):
            runtime.state_manager.release_owner_lock()
            return rc

        # Errore: i chunk ancora in coda nel motore condiviso appartengono a un client ormai inutile
        if runtime.transcriber is not None:
            runtime.transcriber.cancel()
        runtime.state_manager.release_owner_lock()
        job.attempts += 1
        job.runtime = None
        if not retry:
            return rc
        if job.attempts < self._max_attempts and not self._shutdown_event.is_set():
            delay = self._retry_base_delay_sec * (2 ** (job.attempts - 1))
            job.not_before = time.monotonic() + delay
            self._publish(
                runtime.session_id,
                status="failed",
                next_retry_at=now_tz() + timedelta(seconds=delay),
            )
            logger.warning(
                "Elaborazione fallita (tentativo %d/%d): nuovo tentativo tra %.0fs.",
                job.attempts,
                self._max_attempts,
                delay,
            )
            self._queue.put(job)
        else:
            self._publish(runtime.session_id, status="parked", next_retry_at=None)
            logger.error(
                "Elaborazione fallita %d volte: job parcheggiato, serve \"Riprova\" dal cruscotto.",
                job.attempts,
            )
        return rc


def is_recoverable_mode(mode: SessionMode) -> bool:
    """Le sessioni dry-run non vanno riprese automaticamente."""
    return mode != SessionMode.DRY_RUN


__all__ = [
    "AiResourceArbiter",
    "ProcessingJob",
    "ProcessingQueue",
    "ProcessingWorker",
    "SessionProcessor",
    "build_runtime_from_disk",
    "discover_recoverable_sessions",
    "is_recoverable_mode",
    "is_session_locked",
]
