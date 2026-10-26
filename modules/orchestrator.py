from __future__ import annotations

import ctypes
import json
import os
import platform
import queue
import shutil
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from datetime import timedelta
from pathlib import Path
from typing import Protocol

from bootstrap import (
    _fetch_ollama_models,
    _is_model_available,
    _validate_ffmpeg_binary,
    is_tkinter_available,
    load_env_marker,
    probe_ollama_gpu,
)
from config import (
    APP_NAME,
    APP_VERSION,
    BIN_DIR,
    CHUNK_OVERLAP_SEC,
    ENV_MARKER_PATH,
    EXIT_APP_ERROR,
    EXIT_MISSING_DEPENDENCY,
    EXIT_SUCCESS,
    EXIT_UNRECOVERABLE_SESSION,
    FFMPEG_LOCAL_PATH,
    HISTORY_RETENTION_DAYS,
    LOG_DIR,
    PRIVACY_NOTICE_TEXT,
    PROJECT_ROOT,
    PROMPTS_DIR,
    REQUIRE_MANUAL_CONFIRMATION_FOR_FIRST_RECORDING,
    SHOW_RECORDING_NOTIFICATION,
    STATE_SCHEMA_VERSION,
    TEMP_DIR,
    UI_COMMAND_POLL_INTERVAL_SEC,
    USER_CONSENT_PATH,
    WATCHER_POLL_INTERVAL_SEC,
    WHISPER_CPU_COMPUTE_TYPE,
    WHISPER_DEVICE,
    WHISPER_GPU_COMPUTE_TYPE,
    WHISPER_GPU_MODEL,
    ensure_runtime_directories,
    windows_creation_flags,
)
from modules.audio_utils import (
    create_silent_wav,
    discover_audio_devices,
    is_valid_normalized_wav,
    normalize_wav_with_ffmpeg,
    split_wav_file,
)
from modules.contracts import (
    AudioChunk,
    ChunkStatus,
    MeetingMetadata,
    SessionContext,
    SessionMode,
    ShutdownReason,
    Stage,
    StateValidationError,
    TrackLabel,
    TranscriptSegment,
    UiCommandType,
    atomic_write_json,
    atomic_write_text,
    generate_session_id,
    meeting_metadata_to_dict,
    now_tz,
    replace_with_retry,
    sha256_file,
    sha256_text,
)
from modules.docx_builder import (
    MeetingDocxBuilder,
    migrate_flat_output_to_daily,
    validate_docx_file,
)
from modules.job_registry import JobRegistry
from modules.logging_setup import (
    configure_logging,
    get_logger,
    set_log_session_id,
    set_thread_log_session_id,
)
from modules.processing import (
    AiResourceArbiter,
    ProcessingJob,
    ProcessingQueue,
    ProcessingWorker,
    SessionProcessor,
    SummarizerLike,
    build_runtime_from_disk,
    discover_recoverable_sessions,
    is_recoverable_mode,
)
from modules.recorder import DualTrackRecorder
from modules.session_runtime import (
    EVENT_JOB,
    EVENT_NOTIFY,
    EVENT_STATUS,
    AppStatusReporter,
    EventBus,
    EventWriter,
    SessionRuntime,
    SessionStageReporter,
    StageEvent,
)
from modules.settings import (
    AppSettings,
    load_settings,
    resolve_output_dir,
    resolve_settings_path,
    to_summarizer_options,
)
from modules.state_manager import get_state_manager, is_session_locked
from modules.summarizer import OllamaMapReduceSummarizer
from modules.transcriber import RollingTranscriber, WhisperEngine
from modules.transcript_utils import (
    format_chunk_transcript,
    merge_chunk_transcripts,
    resolve_glossary,
    validate_chunk_transcript_file,
)
from modules.tray_ui import StageMonitorTray
from modules.watcher import TeamsCallWatcher, fetch_outlook_meeting_metadata

DRY_RUN_MARKER: str = "CONTENUTO SINTETICO DI TEST"
_STATUS_PUBLISH_INTERVAL_SEC: float = 2.0
_GRACE_MEETING_PROBE_INTERVAL_SEC: float = 30.0


class RecorderLike(Protocol):
    """Interfaccia minima del registratore usata dal controller (permette registratori finti nei test)."""

    @property
    def is_recording(self) -> bool: ...

    @property
    def finalize_event(self) -> threading.Event: ...

    def start(self) -> None: ...

    def stop_capture(self) -> None: ...

    def wait_normalized(self, shutdown_event: threading.Event | None = None) -> list[AudioChunk]: ...


def has_valid_user_consent(consent_path: Path = USER_CONSENT_PATH) -> bool:
    """Verifica se l'utente ha già confermato l'avviso privacy locale."""
    if not consent_path.is_file():
        return False
    try:
        payload = json.loads(consent_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return False
    if not isinstance(payload, dict):
        return False
    return payload.get("accepted") is True


def save_user_consent(consent_path: Path = USER_CONSENT_PATH) -> None:
    """Salva atomicamente la conferma dell'avviso privacy senza alcun dato sensibile."""
    payload: dict[str, object] = {
        "schema_version": STATE_SCHEMA_VERSION,
        "app_version": APP_VERSION,
        "accepted": True,
        "accepted_at": now_tz().isoformat(),
    }
    atomic_write_json(consent_path, payload)


def _default_consent_prompt(notice_text: str) -> bool:
    """Richiede conferma dell'avviso privacy tramite MessageBox Windows o input console."""
    if os.environ.get("TEAMS_SCRIBE_AUTO_ACCEPT_CONSENT") == "1":
        return True

    user32 = getattr(ctypes, "windll", None)
    if user32 is not None and hasattr(user32, "user32"):
        try:
            mb_okcancel = 0x00000001
            mb_iconinformation = 0x00000040
            idok = 1
            prompt_msg = (
                f"{notice_text}\n\n"
                "Premere OK per confermare e avviare la registrazione locale, "
                "oppure Annulla per interrompere."
            )
            result = int(
                user32.user32.MessageBoxW(
                    0,
                    prompt_msg,
                    f"{APP_NAME} - Avviso Privacy e Conformità",
                    mb_okcancel | mb_iconinformation,
                )
            )
            return result == idok
        except Exception as exc:
            get_logger("orchestrator").debug(
                "MessageBoxW non disponibile per il prompt privacy: %s",
                exc,
            )

    if sys.stdin is not None and sys.stdin.isatty():
        sys.stdout.write(f"\n{notice_text}\nConfermi l'avvio della registrazione? [s/N]: ")
        sys.stdout.flush()
        answer = sys.stdin.readline().strip().lower()
        return answer in ("s", "si", "sì", "y", "yes")

    return True


def verify_or_request_user_consent(
    consent_path: Path = USER_CONSENT_PATH,
    *,
    require_confirmation: bool = REQUIRE_MANUAL_CONFIRMATION_FOR_FIRST_RECORDING,
    prompt_callback: Callable[[str], bool] | None = None,
) -> bool:
    """Verifica o richiede il consenso privacy iniziale prima della prima registrazione (Sezione 42)."""
    if not require_confirmation:
        return True
    if has_valid_user_consent(consent_path):
        return True

    callback = prompt_callback if prompt_callback is not None else _default_consent_prompt
    accepted = bool(callback(PRIVACY_NOTICE_TEXT))
    if accepted:
        save_user_consent(consent_path)
    return accepted


def resolve_ffmpeg_executable() -> Path | None:
    """Individua il percorso di FFmpeg verificando marker, bin/ffmpeg.exe e PATH."""
    marker = load_env_marker(ENV_MARKER_PATH)
    if marker is not None:
        raw_ffmpeg = marker.get("ffmpeg_path")
        if isinstance(raw_ffmpeg, str) and raw_ffmpeg.strip():
            candidate = Path(raw_ffmpeg)
            if candidate.is_file():
                return candidate.resolve()

    if FFMPEG_LOCAL_PATH.is_file():
        return FFMPEG_LOCAL_PATH.resolve()

    which_ffmpeg = shutil.which("ffmpeg")
    if which_ffmpeg:
        return Path(which_ffmpeg).resolve()
    return None


class RecordingController:
    """Gestisce l'inizio e la fine delle call nel main loop di run_auto.

    Le callback arrivano dal watcher dopo il rilascio del suo lock. A fine call la cattura viene
    fermata subito, la sessione passa in coda di elaborazione e il watcher torna in attesa:
    il sistema è pronto per la call successiva mentre il worker elabora la precedente.
    """

    def __init__(self, app: MeetingScribeApplication) -> None:
        self._app = app
        self._logger = get_logger("controller")
        self._grace_lock = threading.Lock()
        self._grace_meeting: MeetingMetadata | None = None
        self._grace_thread: threading.Thread | None = None

    def on_call_started(self, metadata: MeetingMetadata, is_manual: bool) -> None:
        app = self._app
        if app.live is not None:
            self._logger.warning("Richiesta di avvio ignorata: una registrazione è già attiva.")
            return
        mode = SessionMode.MANUAL if is_manual else SessionMode.AUTO
        started = app.start_live_session(metadata, mode)
        if not started and app.watcher is not None:
            app.watcher.reset_to_idle()

    def on_call_resumed(self) -> None:
        with self._grace_lock:
            self._grace_meeting = None

    def on_call_suspected_end(self, _remaining_sec: float) -> None:
        live = self._app.live
        if live is None or live.metadata.source != "outlook":
            return
        with self._grace_lock:
            if self._grace_thread is not None and self._grace_thread.is_alive():
                return
            self._grace_thread = threading.Thread(
                target=self._probe_grace_meeting,
                name="GraceMeetingProbe",
                daemon=True,
            )
            self._grace_thread.start()

    def _probe_grace_meeting(self) -> None:
        """Durante il grace legge periodicamente il meeting Outlook in corso (per lo split B6)."""
        app = self._app
        watcher = app.watcher
        while watcher is not None and watcher.stage == Stage.GRACE_PERIOD and not app.shutdown_event.is_set():
            try:
                current = fetch_outlook_meeting_metadata(
                    reference_time=now_tz(),
                    tolerance_min=app.settings.detection.outlook_match_tolerance_min,
                )
            except Exception as exc:
                self._logger.debug("Lettura meeting Outlook durante il grace non riuscita: %s", exc)
                current = None
            with self._grace_lock:
                self._grace_meeting = current
            if app.shutdown_event.wait(_GRACE_MEETING_PROBE_INTERVAL_SEC):
                return

    def grace_meeting_changed(self) -> bool:
        """True se durante il grace il meeting Outlook in corso è diverso da quello della sessione.

        Invocato dal watcher sotto il suo lock: usa solo il valore già letto dal thread di probe.
        """
        live = self._app.live
        with self._grace_lock:
            current = self._grace_meeting
        if live is None or current is None:
            return False
        if live.metadata.source != "outlook" or current.source != "outlook":
            return False
        return (current.title, current.scheduled_start) != (
            live.metadata.title,
            live.metadata.scheduled_start,
        )

    def on_call_ended(self, reason: ShutdownReason) -> None:
        app = self._app
        with self._grace_lock:
            self._grace_meeting = None
        app.finalize_live_session(reason)
        watcher = app.watcher
        # Nello split B6 il watcher è già in RECORDING per la nuova call: non va riportato in IDLE
        if watcher is not None and watcher.stage == Stage.FINALIZING_AUDIO and not app.shutdown_event.is_set():
            watcher.reset_to_idle()
            app.apply_watcher_settings()


class MeetingScribeApplication:
    """Orchestratore centrale del ciclo di vita delle sessioni e dei componenti (Sezione 28)."""

    def __init__(
        self,
        *,
        headless: bool = False,
        output_dir: Path | None = None,
        temp_dir: Path | None = None,
        consent_path: Path | None = None,
        consent_prompt_callback: Callable[[str], bool] | None = None,
        settings: AppSettings | None = None,
        registry: JobRegistry | None = None,
        recorder_factory: Callable[..., RecorderLike] | None = None,
    ) -> None:
        ensure_runtime_directories()
        configure_logging()
        self.logger = get_logger("orchestrator")

        self.settings: AppSettings = settings if settings is not None else load_settings()
        self.output_dir: Path = (
            output_dir if output_dir is not None else resolve_output_dir(self.settings)
        ).resolve()
        self.temp_dir: Path = (
            temp_dir if temp_dir is not None else TEMP_DIR
        ).resolve()
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.temp_dir.mkdir(parents=True, exist_ok=True)

        self.headless: bool = bool(headless)
        self.consent_path: Path = (
            consent_path if consent_path is not None else USER_CONSENT_PATH
        )
        self._consent_prompt_callback = consent_prompt_callback

        # Componenti posseduti (Sezione 28.2)
        self.shutdown_event = threading.Event()
        self.finalize_event = threading.Event()
        self.ui_queue: queue.Queue[UiCommandType] = queue.Queue(maxsize=64)
        self.bus = EventBus()
        self.registry: JobRegistry | None = registry if registry is not None else self._open_registry()

        self.tray = StageMonitorTray(
            command_queue=self.ui_queue,
            output_dir=self.output_dir,
            headless=self.headless,
            on_open_dashboard=self.launch_dashboard,
        )
        self.arbiter = AiResourceArbiter(self.settings.execution.parallel_policy)
        self.engine = WhisperEngine(
            whisper_lock=self.arbiter.whisper_lock,
            cpu_model=self.settings.whisper.model,
            cpu_threads=self.settings.whisper.cpu_threads,
            beam_size=self.settings.whisper.beam_size,
            language=self.settings.whisper.language,
        )
        self.docx_builder = MeetingDocxBuilder()
        self.summarizer: SummarizerLike | None = None
        self.recorder_factory: Callable[..., RecorderLike] = (
            recorder_factory if recorder_factory is not None else DualTrackRecorder
        )

        self.processing_queue = ProcessingQueue()
        self.processor = SessionProcessor(
            engine=self.engine,
            arbiter=self.arbiter,
            bus=self.bus,
            shutdown_event=self.shutdown_event,
            docx_builder=self.docx_builder,
            summarizer_provider=self._summarizer_for,
            output_dir_provider=lambda _runtime: self.output_dir,
            ffmpeg_provider=lambda: resolve_ffmpeg_executable() or FFMPEG_LOCAL_PATH,
            gpu_probe=lambda model: probe_ollama_gpu(self.settings.ollama.base_url, model),
            more_jobs_pending=lambda: len(self.processing_queue) > 0,
        )
        self.event_writer: EventWriter | None = None
        self.worker: ProcessingWorker | None = None
        self.live: SessionRuntime | None = None
        self.watcher: TeamsCallWatcher | None = None
        self.controller = RecordingController(self)
        self._shutdown_reason: ShutdownReason = ShutdownReason.USER_REQUEST
        self._services_started = False

        try:
            migrate_flat_output_to_daily(self.output_dir)
        except OSError as exc:
            self.logger.warning("Migrazione dell'output per giorno non riuscita: %s", exc)

    # ------------------------------------------------------------- servizi
    def _open_registry(self) -> JobRegistry | None:
        try:
            return JobRegistry()
        except Exception as exc:
            self.logger.warning("Storico SQLite non disponibile (%s): cruscotto senza dati.", exc)
            return None

    def _start_services(self) -> None:
        """Avvia tray e writer degli eventi (rieseguibile dopo uno shutdown, come nei test)."""
        self.shutdown_event.clear()
        self.finalize_event.clear()
        self.tray.start()
        if self.event_writer is None or not self.event_writer.is_alive():
            self.event_writer = EventWriter(
                self.bus,
                self.tray,
                self.registry,
                live_session_getter=lambda: self.live.session_id if self.live is not None else None,
            )
            self.event_writer.start()
        self._services_started = True

    def _make_worker(self) -> ProcessingWorker:
        return ProcessingWorker(
            queue=self.processing_queue,
            processor=self.processor,
            bus=self.bus,
            shutdown_event=self.shutdown_event,
            runtime_factory=self._runtime_from_disk,
        )

    def _runtime_from_disk(self, session_dir: Path) -> SessionRuntime:
        return build_runtime_from_disk(
            session_dir,
            settings=self.settings,
            bus=self.bus,
            temp_root=self.temp_dir,
            output_root=self.output_dir,
        )

    def reload_settings(self) -> AppSettings:
        """Rilegge settings.json: i nuovi valori valgono dalla prossima sessione o dal prossimo job."""
        self.settings = load_settings()
        return self.settings

    def apply_watcher_settings(self) -> None:
        if self.watcher is None:
            return
        detection = self.settings.detection
        self.watcher.apply_settings(
            start_debounce_sec=detection.start_debounce_sec,
            stop_grace_period_sec=detection.stop_grace_period_sec,
            use_mic_signal=detection.use_mic_signal,
        )

    def _publish(self, event: StageEvent) -> None:
        self.bus.publish(event)

    def _publish_job(self, session_id: str, **fields: object) -> None:
        self.bus.publish(StageEvent(kind=EVENT_JOB, session_id=session_id, fields=dict(fields)))

    def _notify(self, title: str, message: str) -> None:
        self.bus.publish(StageEvent(kind=EVENT_NOTIFY, detail=message, fields={"title": title}))

    def launch_dashboard(self, tab: str | None = None) -> None:
        """Avvia il cruscotto Tkinter in un processo separato (istanza singola gestita dal cruscotto)."""
        command = [sys.executable, str(PROJECT_ROOT / "main.py"), "--dashboard"]
        if tab:
            command += ["--tab", tab]
        subprocess.Popen(
            command,
            cwd=str(PROJECT_ROOT),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=windows_creation_flags(),
            close_fds=True,
        )

    def _summarizer_for(self, runtime: SessionRuntime) -> SummarizerLike:
        if self.summarizer is not None:
            summarizer = self.summarizer
            if hasattr(summarizer, "set_state_manager"):
                summarizer.set_state_manager(runtime.state_manager)
            elif hasattr(summarizer, "_state_manager"):
                summarizer._state_manager = runtime.state_manager
            if hasattr(summarizer, "set_stage_reporter"):
                summarizer.set_stage_reporter(runtime.reporter)
            if hasattr(summarizer, "set_progress_callback"):
                summarizer.set_progress_callback(runtime.reporter.progress_tokens)
            return summarizer
        ollama = runtime.settings.ollama
        return OllamaMapReduceSummarizer(
            base_url=ollama.base_url,
            model=ollama.model,
            prompts_dir=PROMPTS_DIR,
            state_manager=runtime.state_manager,
            stage_reporter=runtime.reporter,
            shutdown_event=self.shutdown_event,
            ai_lock=self.arbiter.llm_lock,  # type: ignore[arg-type]
            options=to_summarizer_options(runtime.settings),
            on_progress=runtime.reporter.progress_tokens,
        )

    def request_shutdown(
        self,
        reason: ShutdownReason = ShutdownReason.USER_REQUEST,
    ) -> None:
        """Richiede l'arresto ordinato dell'applicazione senza troncare i file (Sezione 28.5)."""
        self._shutdown_reason = reason
        self.shutdown_event.set()
        self.finalize_event.set()
        self.processor.cancel_current()
        self.logger.info("Richiesto shutdown applicativo: reason=%s", reason.value)

    def _ensure_consent_before_recording(self) -> bool:
        """Verifica il consenso privacy prima di iniziare la registrazione."""
        prompt_cb = self._consent_prompt_callback
        if prompt_cb is None and self.headless:
            def _headless_accept(_notice: str) -> bool:
                return True

            prompt_cb = _headless_accept
        accepted = verify_or_request_user_consent(
            self.consent_path,
            require_confirmation=REQUIRE_MANUAL_CONFIRMATION_FOR_FIRST_RECORDING,
            prompt_callback=prompt_cb,
        )
        if not accepted:
            self.logger.warning("Consenso privacy rifiutato dall'utente: registrazione annullata.")
            self._notify(
                "Registrazione annullata",
                "È necessario confermare l'avviso privacy per avviare la registrazione.",
            )
        return accepted

    # ------------------------------------------------------------- sessioni
    def _create_session(
        self,
        mode: SessionMode,
        metadata: MeetingMetadata,
        initial_stage: Stage,
        settings: AppSettings | None = None,
    ) -> SessionRuntime:
        """Crea la cartella di sessione con il proprio StateManager, lock e reporter."""
        started_at = now_tz()
        session_id = generate_session_id(started_at)
        session_dir = (self.temp_dir / f"session_{session_id}").resolve()
        state_manager = get_state_manager(
            session_dir,
            temp_root=self.temp_dir,
            output_root=self.output_dir,
        )
        context = SessionContext(
            schema_version=STATE_SCHEMA_VERSION,
            session_id=session_id,
            session_dir=session_dir,
            mode=mode,
            metadata=metadata,
            started_at=started_at,
            stage=initial_stage,
        )
        state_manager.initialize(context)
        state_manager.acquire_owner_lock()
        snapshot = settings if settings is not None else self.settings
        reporter = SessionStageReporter(session_id, state_manager, self.bus, initial_stage=initial_stage)
        runtime = SessionRuntime(
            session_id=session_id,
            session_dir=state_manager.session_dir,
            state_manager=state_manager,
            reporter=reporter,
            mode=mode,
            metadata=metadata,
            started_at=started_at,
            settings=snapshot,
        )
        self._publish_job(
            session_id,
            title=metadata.title,
            mode=mode.value,
            status="recording" if initial_stage == Stage.RECORDING else "queued",
            stage=initial_stage.value,
            call_started_at=started_at,
            session_dir=str(runtime.session_dir),
            whisper_model=snapshot.whisper.model,
            llm_model=snapshot.ollama.model,
            attempts=0,
        )
        return runtime

    def _on_chunk_normalized(self, runtime: SessionRuntime, chunk: AudioChunk) -> None:
        """Callback del recorder: un chunk normalizzato da FFmpeg passa a Whisper."""
        transcriber = runtime.transcriber
        if transcriber is None or chunk.status != ChunkStatus.NORMALIZED:
            return
        if transcriber.finished:
            # Arresto in corso: il chunk resta normalizzato su disco e verrà trascritto alla ripresa
            self.logger.info("Chunk %03d normalizzato dopo la chiusura del client: rinviato alla ripresa.", chunk.index)
            return
        # Solo durante la registrazione: a sessione finalizzata lo stage non deve tornare indietro (B2)
        if runtime.reporter.stage == Stage.RECORDING:
            runtime.reporter.set_stage(
                Stage.RECORDING_AND_TRANSCRIBING,
                f"Trascrizione chunk {chunk.index + 1} in background",
            )
        transcriber.submit(chunk)

    def _start_recording(self, runtime: SessionRuntime) -> None:
        settings = runtime.settings
        transcriber = RollingTranscriber(
            session_dir=runtime.session_dir,
            state_manager=runtime.state_manager,
            stage_reporter=runtime.reporter,
            engine=self.engine,
            glossary=resolve_glossary(settings.output.glossary),
            session_id=runtime.session_id,
            priority=WhisperEngine.PRIORITY_LIVE,
        )
        runtime.transcriber = transcriber
        ffmpeg_path = resolve_ffmpeg_executable() or FFMPEG_LOCAL_PATH
        recorder = self.recorder_factory(
            session_id=runtime.session_id,
            session_dir=runtime.session_dir,
            on_chunk_ready=lambda chunk: self._on_chunk_normalized(runtime, chunk),
            stage_reporter=runtime.reporter,
            state_manager=runtime.state_manager,
            ffmpeg_path=ffmpeg_path,
            chunk_duration_sec=settings.audio.chunk_duration_sec,
            max_session_duration_sec=settings.audio.max_session_duration_sec,
        )
        runtime.recorder = recorder  # type: ignore[assignment]
        transcriber.start()
        recorder.start()

    def start_live_session(self, metadata: MeetingMetadata, mode: SessionMode) -> bool:
        """Avvia subito la registrazione con metadati di fallback; Outlook viene letto dopo (B25)."""
        if self.live is not None:
            return False
        if not self._ensure_consent_before_recording():
            return False
        settings = self.reload_settings()
        runtime = self._create_session(mode, metadata, Stage.RECORDING, settings)
        set_log_session_id(runtime.session_id)
        try:
            self._start_recording(runtime)
        except Exception as exc:
            self.logger.error("Avvio della registrazione non riuscito: %s", exc, exc_info=True)
            if runtime.recorder is not None:
                try:
                    runtime.recorder.stop_capture()
                except Exception as stop_exc:
                    self.logger.debug("Arresto del recorder dopo errore di avvio: %s", stop_exc)
            if runtime.transcriber is not None:
                runtime.transcriber.cancel()
            self.processor.handle_pipeline_error(runtime, exc)
            self._publish_job(runtime.session_id, status="parked")
            runtime.state_manager.release_owner_lock()
            set_log_session_id(None)
            return False

        self.live = runtime
        if metadata.source == "fallback":
            self._fetch_metadata_async(runtime)
        if SHOW_RECORDING_NOTIFICATION:
            self._notify("Registrazione Avviata", f"Sessione attiva per: {metadata.title}")
        return True

    def _fetch_metadata_async(self, runtime: SessionRuntime) -> None:
        """Legge i metadati Outlook in un thread breve con il proprio CoInitialize, a registrazione già avviata."""

        def _worker() -> None:
            set_thread_log_session_id(runtime.session_id)
            try:
                metadata = fetch_outlook_meeting_metadata(
                    reference_time=runtime.started_at,
                    tolerance_min=runtime.settings.detection.outlook_match_tolerance_min,
                )
            except Exception as exc:
                self.logger.warning("Lettura metadati Outlook non riuscita: %s", exc)
                return
            if metadata.source != "outlook":
                return
            runtime.metadata = metadata

            def _apply(c: SessionContext) -> None:
                c.metadata = metadata

            try:
                runtime.state_manager.mutate(_apply)
                atomic_write_json(
                    runtime.session_dir / "metadata" / "meeting.json",
                    meeting_metadata_to_dict(metadata),
                )
            except (StateValidationError, OSError) as exc:
                self.logger.debug("Metadati Outlook non salvati (sessione già chiusa?): %s", exc)
                return
            self._publish_job(runtime.session_id, title=metadata.title)
            self.logger.info("Riunione abbinata da Outlook: %s", metadata.title)
            self._notify("Riunione riconosciuta", f"Registrazione di: {metadata.title}")

        threading.Thread(target=_worker, name="OutlookMetadata", daemon=True).start()

    def finalize_live_session(self, reason: ShutdownReason = ShutdownReason.CALL_ENDED) -> None:
        """Ferma subito la cattura e mette la sessione in coda di elaborazione."""
        runtime = self.live
        if runtime is None:
            return
        self.live = None
        self.logger.info(
            "Finalizzazione sessione live (reason=%s, session=%s)",
            reason.value,
            runtime.session_dir.name,
        )
        # La durata finisce all'ultimo istante in cui la call era attiva, non a fine grace (B18)
        inactive_for = (
            self.watcher.seconds_since_last_active()
            if self.watcher is not None and reason in (ShutdownReason.GRACE_EXPIRED, ShutdownReason.CALL_ENDED)
            else 0.0
        )
        ended_at = now_tz() - timedelta(seconds=inactive_for)
        if ended_at < runtime.started_at:
            ended_at = now_tz()
        runtime.ended_at = ended_at
        runtime.reporter.set_stage(Stage.FINALIZING_AUDIO, "Chiusura tracce e normalizzazione ultimo chunk")
        if runtime.recorder is not None:
            try:
                runtime.recorder.stop_capture()
            except Exception as exc:
                self.logger.error("Errore nella chiusura della registrazione: %s", exc, exc_info=True)

        def _set_ended(c: SessionContext) -> None:
            c.ended_at = ended_at

        try:
            runtime.state_manager.mutate(_set_ended)
        except (StateValidationError, OSError) as exc:
            self.logger.warning("Impossibile salvare ended_at: %s", exc)
        runtime.reporter.set_stage(Stage.QUEUED, "In coda per l'elaborazione")
        self._publish_job(runtime.session_id, status="queued", call_ended_at=ended_at)
        self.processing_queue.put(
            ProcessingJob(session_id=runtime.session_id, session_dir=runtime.session_dir, runtime=runtime)
        )
        set_log_session_id(None)

    def retry_job(self, session_id: str) -> bool:
        """Rimette in coda una sessione fallita o parcheggiata (comando "Riprova" del cruscotto)."""
        session_dir = (self.temp_dir / f"session_{session_id}").resolve()
        if not (session_dir / "state.json").is_file():
            self._notify("Riprova non possibile", "La cartella della sessione non esiste più.")
            return False
        if self.processing_queue.contains(session_id) or (
            self.worker is not None and self.worker.current_session_id == session_id
        ):
            return False
        self._publish_job(session_id, status="queued", attempts=0, next_retry_at=None, error=None)
        self.processing_queue.put(ProcessingJob(session_id=session_id, session_dir=session_dir))
        return True

    def _recover_pending_sessions(self) -> int:
        """Accoda in ordine cronologico le sessioni rimaste su disco e non in uso da altri."""
        recovered = 0
        for session_dir in discover_recoverable_sessions(self.temp_dir):
            session_id = session_dir.name.removeprefix("session_")
            if self.processing_queue.contains(session_id) or is_session_locked(session_dir):
                continue
            job_info = self.registry.get_job(session_id) if self.registry is not None else None
            if job_info is not None and job_info.get("status") == "parked":
                continue
            try:
                context = get_state_manager(
                    session_dir, temp_root=self.temp_dir, output_root=self.output_dir
                ).load()
            except (StateValidationError, OSError) as exc:
                self.logger.warning("Sessione %s non leggibile, ignorata: %s", session_dir.name, exc)
                continue
            if not is_recoverable_mode(context.mode):
                continue
            self._publish_job(
                session_id,
                title=context.metadata.title,
                mode=context.mode.value,
                status="queued",
                call_started_at=context.started_at,
                call_ended_at=context.ended_at,
                session_dir=str(session_dir),
            )
            self.processing_queue.put(ProcessingJob(session_id=session_id, session_dir=session_dir))
            recovered += 1
        if recovered:
            self.logger.info("Recovery all'avvio: %d sessioni rimesse in coda.", recovered)
            self._notify("Sessioni recuperate", f"{recovered} sessioni interrotte verranno elaborate.")
        return recovered

    def _run_queued_jobs_synchronously(self) -> int:
        """Esegue nel thread corrente i job in coda, un solo tentativo ciascuno (--resume, --test-pipeline)."""
        worker = self._make_worker()
        last_rc = EXIT_SUCCESS
        while True:
            job = self.processing_queue.get(timeout=0.0)
            if job is None:
                return last_rc
            last_rc = worker.run_job(job, retry=False)

    # ------------------------------------------------------------- comandi UI
    def _drain_ui_commands_in_auto(self) -> None:
        """Consuma i comandi inviati dalla System Tray durante il loop automatico."""
        while True:
            try:
                cmd = self.ui_queue.get_nowait()
            except queue.Empty:
                break
            self._handle_command(cmd.value, {})

    def _poll_registry_commands(self) -> None:
        """Comandi inviati dal cruscotto (altro processo) tramite la tabella ui_commands."""
        if self.registry is None:
            return
        try:
            commands = self.registry.fetch_pending_commands()
        except Exception as exc:
            self.logger.debug("Lettura comandi del cruscotto non riuscita: %s", exc)
            return
        for command, payload in commands:
            self._handle_command(command, payload)

    def _handle_command(self, command: str, payload: dict[str, object]) -> None:
        if command == UiCommandType.START_MANUAL.value:
            if self.watcher is not None:
                self.watcher.request_manual_start()
        elif command == UiCommandType.FINALIZE_NOW.value:
            if self.watcher is not None:
                self.watcher.request_finalize()
            self.finalize_event.set()
        elif command == UiCommandType.RETRY_JOB.value:
            session_id = payload.get("session_id")
            if isinstance(session_id, str) and session_id:
                self.retry_job(session_id)
        elif command == "reload_settings":
            self.reload_settings()
            if self.live is None:
                self.apply_watcher_settings()
        elif command == UiCommandType.EXIT.value:
            self.request_shutdown(ShutdownReason.USER_REQUEST)

    def _publish_app_status(self) -> None:
        live = self.live
        worker = self.worker
        self._publish(
            StageEvent(
                kind=EVENT_STATUS,
                fields={
                    "heartbeat": now_tz().isoformat(),
                    "pid": os.getpid(),
                    "queue_length": len(self.processing_queue),
                    "processing_session": worker.current_session_id if worker is not None else None,
                    "live_session": live.session_id if live is not None else None,
                    "recording_started_at": live.started_at.isoformat() if live is not None else None,
                    "llm_gpu_percent": self.arbiter.last_gpu_percent,
                    "ai_parallel": self.arbiter.parallel,
                    "ai_policy": self.arbiter.policy,
                    "whisper_model": self.engine.model_name,
                    "llm_model": self.settings.ollama.model,
                },
            )
        )

    # ------------------------------------------------------------------ run
    def run_auto(self) -> int:
        """Modalità automatica: tray, watcher e registrazione concorrente all'elaborazione (Sezione 28.1)."""
        self._start_services()
        self.tray.set_recording_status(Stage.IDLE, "In attesa di call Microsoft Teams")
        if self.registry is not None:
            try:
                self.registry.purge_older_than(HISTORY_RETENTION_DAYS)
                self.registry.import_history_from_log(output_dir=self.output_dir)
            except Exception as exc:
                self.logger.warning("Manutenzione dello storico non riuscita: %s", exc)

        self._recover_pending_sessions()
        self.worker = self._make_worker()
        self.worker.start()

        detection = self.settings.detection
        self.watcher = TeamsCallWatcher(
            stage_reporter=AppStatusReporter(self.bus),
            on_call_started=self.controller.on_call_started,
            on_call_resumed=self.controller.on_call_resumed,
            on_call_suspected_end=self.controller.on_call_suspected_end,
            on_call_ended=self.controller.on_call_ended,
            shutdown_event=self.shutdown_event,
            start_debounce_sec=detection.start_debounce_sec,
            stop_grace_period_sec=detection.stop_grace_period_sec,
            use_mic_signal=detection.use_mic_signal,
            split_checker=self.controller.grace_meeting_changed,
        )

        next_watch = 0.0
        next_commands = 0.0
        next_status = 0.0
        try:
            while not self.shutdown_event.is_set():
                self._drain_ui_commands_in_auto()
                now_mono = time.monotonic()
                if now_mono >= next_commands:
                    self._poll_registry_commands()
                    next_commands = now_mono + UI_COMMAND_POLL_INTERVAL_SEC
                if self.shutdown_event.is_set():
                    break
                if now_mono >= next_watch:
                    self.watcher.step()
                    next_watch = now_mono + WATCHER_POLL_INTERVAL_SEC
                live = self.live
                if (
                    live is not None
                    and live.recorder is not None
                    and live.recorder.finalize_event.is_set()
                    and self.watcher.is_call_active
                ):
                    self.watcher.request_finalize()
                    next_watch = 0.0
                if now_mono >= next_status:
                    self._publish_app_status()
                    next_status = now_mono + _STATUS_PUBLISH_INTERVAL_SEC
                self.shutdown_event.wait(timeout=0.1)
        except KeyboardInterrupt:
            self.request_shutdown(ShutdownReason.USER_REQUEST)
        finally:
            self._perform_coordinated_shutdown()

        return EXIT_SUCCESS

    def run_record_now(self) -> int:
        """Avvia immediatamente una sessione di registrazione manuale (Sezione 28.1 / 29)."""
        self._start_services()
        try:
            metadata = MeetingMetadata(title="Riunione Teams", source="fallback")
            if not self.start_live_session(metadata, SessionMode.MANUAL):
                return EXIT_APP_ERROR

            while not self.shutdown_event.is_set() and not self.finalize_event.is_set():
                try:
                    cmd = self.ui_queue.get(timeout=0.25)
                    if cmd == UiCommandType.FINALIZE_NOW:
                        self.finalize_event.set()
                    elif cmd == UiCommandType.EXIT:
                        self.request_shutdown(ShutdownReason.USER_REQUEST)
                        break
                except queue.Empty:
                    pass

                live = self.live
                if live is not None and live.recorder is not None and live.recorder.finalize_event.is_set():
                    self.finalize_event.set()

            if self.shutdown_event.is_set():
                return EXIT_APP_ERROR
            self.finalize_live_session(ShutdownReason.USER_REQUEST)
            return self._run_queued_jobs_synchronously()
        except KeyboardInterrupt:
            self.request_shutdown(ShutdownReason.USER_REQUEST)
            return EXIT_APP_ERROR
        finally:
            self._perform_coordinated_shutdown()

    def run_resume(self, session_dir: Path) -> int:
        """Riconcilia state.json e gli artefatti su disco riprendendo dal primo step incompleto (Sezione 27.4)."""
        self._start_services()
        try:
            resolved_dir = session_dir.resolve()
            if not resolved_dir.exists() or not resolved_dir.is_dir():
                self.logger.error("Directory di sessione inesistente per --resume: %s", resolved_dir)
                return EXIT_UNRECOVERABLE_SESSION
            session_id = resolved_dir.name.removeprefix("session_")
            worker = self._make_worker()
            return worker.run_job(
                ProcessingJob(session_id=session_id, session_dir=resolved_dir),
                retry=False,
            )
        except Exception as exc:
            self.logger.error("Errore durante run_resume: %s", exc, exc_info=True)
            return EXIT_APP_ERROR
        finally:
            self._perform_coordinated_shutdown()

    def run_test_pipeline(self, audio_file: Path) -> int:
        """Esegue la pipeline completa su una copia di un file audio esterno preservando l'originale (Sezione 30)."""
        self._start_services()
        runtime: SessionRuntime | None = None
        try:
            if not audio_file.exists():
                self.logger.error("File audio non trovato per --test-pipeline: %s", audio_file)
                return EXIT_APP_ERROR
            if audio_file.is_dir():
                self.logger.error("Il percorso specificato per --test-pipeline è una directory: %s", audio_file)
                return EXIT_APP_ERROR

            original_path = audio_file.resolve()
            original_sha256 = sha256_file(original_path)

            metadata = MeetingMetadata(
                title=f"Test Pipeline {original_path.stem}",
                organizer="Test Pipeline Runner",
                agenda="Sessione di validazione end-to-end da file audio locale.",
                source="test_pipeline",
            )
            runtime = self._create_session(SessionMode.TEST_PIPELINE, metadata, Stage.FINALIZING_AUDIO)
            set_log_session_id(runtime.session_id)
            state_manager = runtime.state_manager
            audio_dir = runtime.session_dir / "audio"

            copied_input = audio_dir / f"source_input{original_path.suffix or '.wav'}"
            shutil.copy2(original_path, copied_input)

            ffmpeg_path = resolve_ffmpeg_executable() or FFMPEG_LOCAL_PATH
            working_wav = audio_dir / "source_converted.wav"
            if copied_input.suffix.lower() == ".wav":
                working_wav = copied_input
            else:
                normalize_wav_with_ffmpeg(
                    ffmpeg_path,
                    copied_input,
                    working_wav,
                    delete_raw_on_success=True,
                )

            chunk_duration = runtime.settings.audio.chunk_duration_sec
            split_chunks = split_wav_file(
                input_wav=working_wav,
                output_dir=audio_dir,
                chunk_duration_sec=chunk_duration,
                overlap_sec=CHUNK_OVERLAP_SEC,
            )
            if working_wav.exists() and working_wav != original_path:
                try:
                    working_wav.unlink()
                except OSError as unlink_exc:
                    self.logger.debug(
                        "Impossibile eliminare copia intermedia %s: %s",
                        working_wav,
                        unlink_exc,
                    )

            for idx, (raw_others_path, start_sec, end_sec) in enumerate(split_chunks):
                norm_others_path = audio_dir / f"chunk_{idx:03d}_altri_16k.wav"
                norm_io_path = audio_dir / f"chunk_{idx:03d}_io_16k.wav"

                if is_valid_normalized_wav(raw_others_path) and not ffmpeg_path.is_file():
                    replace_with_retry(raw_others_path, norm_others_path)
                else:
                    normalize_wav_with_ffmpeg(
                        ffmpeg_path,
                        raw_others_path,
                        norm_others_path,
                        delete_raw_on_success=True,
                    )

                duration_sec = max(0.1, end_sec - start_sec)
                create_silent_wav(norm_io_path, duration_sec=duration_sec)

                def _record_chunk_idx(c: SessionContext, c_idx: int = idx) -> None:
                    if c_idx not in c.recorded_chunks:
                        c.recorded_chunks.append(c_idx)
                    if c_idx not in c.normalized_chunks:
                        c.normalized_chunks.append(c_idx)

                state_manager.mutate(_record_chunk_idx)

            total_audio = split_chunks[-1][2] if split_chunks else 0.0
            ended_at = runtime.started_at + timedelta(seconds=max(1.0, total_audio))

            def _set_ended(c: SessionContext) -> None:
                c.ended_at = ended_at

            state_manager.mutate(_set_ended)
            runtime.reporter.set_stage(Stage.QUEUED, "Audio di test pronto per l'elaborazione")
            worker = self._make_worker()
            exit_code = worker.run_job(
                ProcessingJob(session_id=runtime.session_id, session_dir=runtime.session_dir, runtime=runtime),
                retry=False,
            )

            # Verifica tassativa che il file originale esista ancora e sia intatto (AC23)
            if not original_path.exists() or sha256_file(original_path) != original_sha256:
                self.logger.error("Violazione integrità file audio originale in --test-pipeline!")
                return EXIT_APP_ERROR

            return exit_code
        except Exception as exc:
            if runtime is not None:
                self.processor.handle_pipeline_error(runtime, exc)
                runtime.state_manager.release_owner_lock()
            else:
                self.logger.error("Errore in --test-pipeline: %s", exc, exc_info=True)
            return EXIT_APP_ERROR
        finally:
            set_log_session_id(None)
            self._perform_coordinated_shutdown()

    def run_dry_run(self) -> int:
        """Esegue un test sintetico completo end-to-end senza toccare hardware o modelli AI (Sezione 31)."""
        self._start_services()
        runtime: SessionRuntime | None = None
        try:
            started_at = now_tz()
            metadata = MeetingMetadata(
                title=f"Dry Run {DRY_RUN_MARKER}",
                organizer=f"Organizer ({DRY_RUN_MARKER})",
                required_attendees=["Partecipante Sintetico A", "Partecipante Sintetico B"],
                agenda=f"{DRY_RUN_MARKER} - Verifica architetturale offline della pipeline.",
                scheduled_start=started_at,
                scheduled_end=started_at + timedelta(minutes=30),
                source="dry_run",
            )
            runtime = self._create_session(SessionMode.DRY_RUN, metadata, Stage.IDLE)
            set_log_session_id(runtime.session_id)
            state_manager = runtime.state_manager
            session_dir = state_manager.session_dir

            # 1. Test transizioni State Machine e Tray
            for next_stage, detail in (
                (Stage.START_DEBOUNCE, f"{DRY_RUN_MARKER}: debounce simulato"),
                (Stage.RECORDING, f"{DRY_RUN_MARKER}: registrazione sintetica"),
                (Stage.RECORDING_AND_TRANSCRIBING, f"{DRY_RUN_MARKER}: trascrizione rolling sintetica"),
                (Stage.GRACE_PERIOD, f"{DRY_RUN_MARKER}: grace period simulato"),
                (Stage.RECORDING, f"{DRY_RUN_MARKER}: ripresa call simulata"),
                (Stage.FINALIZING_AUDIO, f"{DRY_RUN_MARKER}: finalizzazione audio sintetica"),
                (Stage.QUEUED, f"{DRY_RUN_MARKER}: sessione in coda di elaborazione"),
            ):
                state_manager.update_stage(next_stage, detail=detail)
                self.tray.set_stage(next_stage, detail_text=detail)

            # 2. Generazione chunk sintetici e transcript sintetici
            audio_dir = session_dir / "audio"
            transcripts_dir = session_dir / "transcripts"
            chunk_paths: list[Path] = []

            for chunk_idx in range(2):
                io_wav = create_silent_wav(
                    audio_dir / f"chunk_{chunk_idx:03d}_io_16k.wav",
                    duration_sec=1.0,
                )
                others_wav = create_silent_wav(
                    audio_dir / f"chunk_{chunk_idx:03d}_altri_16k.wav",
                    duration_sec=1.0,
                )
                start_offset = float(chunk_idx * 900)
                end_offset = start_offset + 903.0
                segments = [
                    TranscriptSegment(
                        track=TrackLabel.IO,
                        start_sec=start_offset + 5.0,
                        end_sec=start_offset + 10.0,
                        text=f"{DRY_RUN_MARKER} - Intervento locale nel blocco {chunk_idx}.",
                    ),
                    TranscriptSegment(
                        track=TrackLabel.OTHERS,
                        start_sec=start_offset + 12.0,
                        end_sec=start_offset + 20.0,
                        text=f"{DRY_RUN_MARKER} - Risposta remota confermata nel blocco {chunk_idx}.",
                    ),
                ]
                chunk_txt = format_chunk_transcript(
                    chunk_index=chunk_idx,
                    session_id=runtime.session_id,
                    start_offset_sec=start_offset,
                    end_offset_sec=end_offset,
                    segments=segments,
                )
                chunk_txt_path = transcripts_dir / f"chunk_{chunk_idx:03d}.txt"
                atomic_write_text(chunk_txt_path, chunk_txt)
                validate_chunk_transcript_file(chunk_txt_path)
                io_wav.unlink()
                others_wav.unlink()
                chunk_paths.append(chunk_txt_path)

                def _update_chunk(c: SessionContext, idx: int = chunk_idx) -> None:
                    c.recorded_chunks.append(idx)
                    c.normalized_chunks.append(idx)
                    c.transcribed_chunks.append(idx)
                    c.audio_deleted_chunks.append(idx)

                state_manager.mutate(_update_chunk)

            state_manager.update_stage(
                Stage.TRANSCRIBING_FINAL,
                detail=f"{DRY_RUN_MARKER}: unione transcript sintetici",
            )
            self.tray.set_stage(Stage.TRANSCRIBING_FINAL, DRY_RUN_MARKER)

            merged_path = transcripts_dir / "merged_transcript.txt"
            merge_chunk_transcripts(chunk_paths, merged_path)

            # 3. MAP sintetico
            state_manager.update_stage(
                Stage.SUMMARIZING_MAP,
                detail=f"{DRY_RUN_MARKER}: generazione MAP sintetico",
            )
            self.tray.set_stage(Stage.SUMMARIZING_MAP, DRY_RUN_MARKER, current=1, total=1)

            maps_dir = session_dir / "maps"
            map_md_path = maps_dir / "map_block_000.md"
            map_meta_path = maps_dir / "map_block_000.meta.json"
            rolling_path = maps_dir / "rolling_context_000.md"

            synthetic_map = (
                "## Resoconto dettagliato\n"
                f"### Verifica sintetica della pipeline ({DRY_RUN_MARKER})\n"
                "Validazione della state machine, delle scritture atomiche e del builder DOCX.\n\n"
                "## Decisioni\n- Confermata l'integrità del flusso dry-run.\n\n"
                "## Action items\n- Responsabile: Da assegnare | Azione: Verificare il file Word generato"
                " | Priorità: Media | Scadenza: Non specificata\n\n"
                "## Questioni aperte\n- Nessuna questione aperta.\n\n"
                "## Rischi\n- Nessun rischio rilevato nel test sintetico.\n\n"
                f"## Rolling Context\nSintesi {DRY_RUN_MARKER} completata.\n"
            )
            atomic_write_text(map_md_path, synthetic_map)
            atomic_write_text(rolling_path, f"Rolling context {DRY_RUN_MARKER}\n")
            atomic_write_json(
                map_meta_path,
                {
                    "block_index": 0,
                    "block_start_sec": 0.0,
                    "block_end_sec": 1800.0,
                    "input_sha256": sha256_file(merged_path),
                    "prompt_sha256": sha256_text("DRY_RUN_PROMPT"),
                    "model": "dry-run-synthetic",
                    "output_sha256": sha256_file(map_md_path),
                    "completed_at": now_tz().isoformat(),
                },
            )

            def _mark_map(c: SessionContext) -> None:
                c.mapped_blocks = [0]

            state_manager.mutate(_mark_map)

            # 4. REDUCE sintetico
            state_manager.update_stage(
                Stage.SUMMARIZING_REDUCE,
                detail=f"{DRY_RUN_MARKER}: generazione REDUCE sintetico",
            )
            self.tray.set_stage(Stage.SUMMARIZING_REDUCE, DRY_RUN_MARKER)

            reduce_dir = session_dir / "reduce"
            detailed_minutes_path = reduce_dir / "detailed_minutes.md"
            recap_email_path = reduce_dir / "recap_email.md"

            synthetic_minutes = (
                f"## 1. Executive Summary\nDocumento generato in modalità **{DRY_RUN_MARKER}** "
                "per validare l'intera architettura senza componenti hardware o AI reali.\n\n"
                f"## 2. Dati della Riunione\n- Modalità: {DRY_RUN_MARKER}\n- Stato: Verificato\n\n"
                "## 3. Resoconto dettagliato per argomento\n"
                "### 00:00:00 – 00:30:00\n"
                "#### Verifica della pipeline\n"
                "Test della macchina a stati e persistenza JSON atomica.\n"
                "Test del generatore e validatore Microsoft Word.\n\n"
                "## 4. Decisioni Confermate\n1. L'architettura supera la validazione sintetica.\n\n"
                "## 5. Questioni Aperte\n- Nessuna questione aperta.\n\n"
                "## 6. Rischi\n- Nessun rischio operativo nel test offline.\n\n"
                "## 7. Dipendenze\n- Nessuna dipendenza esterna attivata.\n\n"
                "## 8. Action Items\n"
                "| Responsabile | Task / Azione | Priorità | Scadenza / Note |\n"
                "| --- | --- | --- | --- |\n"
                f"| Da assegnare | Verifica documento {DRY_RUN_MARKER} | Media | Immediata |\n"
            )
            synthetic_email = (
                f"## Oggetto\nRecap Riunione - {DRY_RUN_MARKER}\n\n"
                "## Introduzione\nBuongiorno a tutti, di seguito la sintesi generata automaticamente "
                f"in modalità **{DRY_RUN_MARKER}**.\n\n"
                f"## TL;DR\nLa pipeline sintetica ({DRY_RUN_MARKER}) ha completato tutti gli stage previsti.\n\n"
                "## Decisioni Principali\n- Validazione offline completata con successo.\n\n"
                "## Next Steps\n- Da assegnare: Controllo del file Word nella cartella output.\n\n"
                "## Chiusura\nCordiali saluti,\nVerbalia\n"
            )
            atomic_write_text(detailed_minutes_path, synthetic_minutes)
            atomic_write_text(recap_email_path, synthetic_email)

            def _mark_reduce(c: SessionContext) -> None:
                c.detailed_reduce_completed = True
                c.email_reduce_completed = True
                c.ended_at = now_tz()

            context = state_manager.mutate(_mark_reduce)

            # 5. Costruzione e validazione reale del DOCX + Cleanup
            state_manager.update_stage(
                Stage.BUILDING_DOCX,
                detail=f"{DRY_RUN_MARKER}: creazione DOCX",
            )
            self.tray.set_stage(Stage.BUILDING_DOCX, DRY_RUN_MARKER)

            ended_at = context.ended_at or now_tz()
            final_docx = self.docx_builder.build(
                metadata=metadata,
                started_at=started_at,
                ended_at=ended_at,
                recap_email_path=recap_email_path,
                detailed_minutes_path=detailed_minutes_path,
                output_dir=self.output_dir,
                transcript_path=merged_path,
                user_display_name=self.settings.output.user_display_name,
                organize_by_day=self.settings.output.organize_by_day,
            )

            state_manager.update_stage(
                Stage.VERIFYING_DOCX,
                detail=f"{DRY_RUN_MARKER}: verifica DOCX",
            )
            self.tray.set_stage(Stage.VERIFYING_DOCX, DRY_RUN_MARKER)
            validate_docx_file(final_docx)

            state_manager.cleanup_session_files(final_docx)
            self.tray.set_last_output_file(final_docx)
            self.tray.set_stage(Stage.COMPLETED, f"Dry-run completato: {final_docx.name}")
            self._publish_job(
                runtime.session_id,
                status="completed",
                stage=Stage.COMPLETED.value,
                docx_path=str(final_docx),
                processing_ended_at=now_tz(),
            )
            return EXIT_SUCCESS
        except Exception as exc:
            if runtime is not None:
                self.processor.handle_pipeline_error(runtime, exc)
                runtime.state_manager.release_owner_lock()
            else:
                self.logger.error("Errore nel dry-run: %s", exc, exc_info=True)
            return EXIT_APP_ERROR
        finally:
            set_log_session_id(None)
            self._perform_coordinated_shutdown()

    def collect_diagnostics_report(self) -> dict[str, object]:
        """Raccoglie i dati diagnostici sanitizzati senza esporre alcun contenuto sensibile (Sezione 32)."""
        in_venv = sys.prefix != getattr(sys, "base_prefix", sys.prefix)

        dir_status: dict[str, bool] = {}
        for name, d_path in (
            ("bin", BIN_DIR),
            ("logs", LOG_DIR),
            ("temp", self.temp_dir),
            ("output", self.output_dir),
        ):
            try:
                d_path.mkdir(parents=True, exist_ok=True)
                probe = d_path / f".diag_probe_{os.getpid()}.tmp"
                probe.write_text("ok", encoding="utf-8")
                probe.unlink()
                dir_status[name] = True
            except OSError:
                dir_status[name] = False

        try:
            disk_usage = shutil.disk_usage(str(self.temp_dir))
            free_disk_gb = round(disk_usage.free / (1024**3), 2)
        except OSError:
            free_disk_gb = 0.0

        ffmpeg_exe = resolve_ffmpeg_executable()
        ffmpeg_version = (
            _validate_ffmpeg_binary(ffmpeg_exe)
            if ffmpeg_exe is not None
            else None
        )

        ollama_cfg = self.settings.ollama
        ollama_models = _fetch_ollama_models(ollama_cfg.base_url, timeout=3.0)
        ollama_reachable = ollama_models is not None
        ollama_model_ready = (
            _is_model_available(ollama_models, ollama_cfg.model)
            if ollama_models is not None
            else False
        )
        gpu_probe = probe_ollama_gpu(ollama_cfg.base_url, ollama_cfg.model) if ollama_reachable else {}

        pyaudio_available = False
        mic_summary = "Non disponibile"
        output_summary = "Non disponibile"
        loopback_summary = "Non disponibile"
        try:
            import pyaudiowpatch as pawp

            pyaudio_available = True
            mic_dev, loop_dev = discover_audio_devices()
            mic_summary = f"{mic_dev.name} ({mic_dev.sample_rate}Hz, {mic_dev.channels}ch)"
            loopback_summary = f"{loop_dev.name} ({loop_dev.sample_rate}Hz, {loop_dev.channels}ch)"
            output_summary = loop_dev.host_api
            _ = pawp
        except Exception as audio_exc:
            mic_summary = f"Non rilevato ({type(audio_exc).__name__})"
            loopback_summary = f"Non rilevato ({type(audio_exc).__name__})"

        outlook_com_available = False
        try:
            import pythoncom
            import win32com.client

            pythoncom.CoInitialize()
            try:
                _ = win32com.client.Dispatch("Outlook.Application")
                outlook_com_available = True
            finally:
                pythoncom.CoUninitialize()
        except Exception:
            outlook_com_available = False

        marker = load_env_marker(ENV_MARKER_PATH)
        whisper_cfg = {
            "configured_device": WHISPER_DEVICE,
            "gpu_model": f"{WHISPER_GPU_MODEL} ({WHISPER_GPU_COMPUTE_TYPE})",
            "cpu_model": f"{self.settings.whisper.model} ({WHISPER_CPU_COMPUTE_TYPE})",
            "cpu_threads": self.settings.whisper.cpu_threads,
            "marker_device": marker.get("whisper_device") if marker else None,
            "marker_model": marker.get("whisper_model") if marker else None,
        }

        all_dirs_ok = all(dir_status.values())
        ffmpeg_ok = ffmpeg_exe is not None and ffmpeg_version is not None
        overall_ok = all_dirs_ok and ffmpeg_ok and ollama_reachable and ollama_model_ready

        return {
            "app_name": APP_NAME,
            "app_version": APP_VERSION,
            "python_version": platform.python_version(),
            "architecture": platform.machine(),
            "windows_version": platform.platform(),
            "in_virtualenv": in_venv,
            "directories_writable": dir_status,
            "free_disk_gb": free_disk_gb,
            "ffmpeg_path": str(ffmpeg_exe) if ffmpeg_exe else None,
            "ffmpeg_version": ffmpeg_version,
            "ollama_reachable": ollama_reachable,
            "ollama_available_models": ollama_models or [],
            "ollama_configured_model": ollama_cfg.model,
            "ollama_model_present": ollama_model_ready,
            "ollama_gpu": gpu_probe,
            "ai_parallel_policy": self.settings.execution.parallel_policy,
            "pyaudiowpatch_available": pyaudio_available,
            "default_microphone": mic_summary,
            "default_output": output_summary,
            "wasapi_loopback": loopback_summary,
            "outlook_com_available": outlook_com_available,
            "whisper_configuration": whisper_cfg,
            "tkinter_available": is_tkinter_available(),
            "settings_path": str(resolve_settings_path()),
            "history_db": str(self.registry.db_path) if self.registry is not None else None,
            "overall_status": "OK" if overall_ok else "DEGRADED",
        }

    def run_diagnostics(self) -> int:
        """Stampa a schermo il report diagnostico completo (Sezione 32)."""
        report = self.collect_diagnostics_report()
        raw_models = report.get("ollama_available_models")
        models_list = (
            [str(m) for m in raw_models]
            if isinstance(raw_models, list)
            else []
        )
        gpu = report.get("ollama_gpu")
        gpu_info = gpu if isinstance(gpu, dict) else {}
        gpu_percent = gpu_info.get("gpu_percent")
        if not report["ollama_reachable"]:
            gpu_line = "Non verificabile (Ollama non raggiungibile)"
        elif gpu_percent is None:
            gpu_line = "Nessun modello caricato in memoria al momento (eseguire un'inferenza e riprovare)"
        else:
            gpu_line = f"{gpu_percent}% del modello in VRAM"
        lines = [
            f"=== DIAGNOSTICA {APP_NAME.upper()} v{APP_VERSION} ===",
            f"Versione Applicazione : {report['app_version']}",
            f"Python / Architettura : {report['python_version']} ({report['architecture']})",
            f"Sistema Operativo     : {report['windows_version']}",
            f"Virtual Environment   : {'Attivo' if report['in_virtualenv'] else 'Non attivo'}",
            f"Scrivibilità Cartelle : {report['directories_writable']}",
            f"Spazio Disco Libero   : {report['free_disk_gb']} GB",
            f"FFmpeg Path           : {report['ffmpeg_path'] or 'NON TROVATO'}",
            f"FFmpeg Versione       : {report['ffmpeg_version'] or 'NON DISPONIBILE'}",
            f"Ollama Raggiungibile  : {'Sì' if report['ollama_reachable'] else 'No'} ({self.settings.ollama.base_url})",
            f"Modelli Ollama        : {', '.join(models_list) if models_list else 'Nessuno'}",
            f"Modello Configurato   : {report['ollama_configured_model']} (Presente: {'Sì' if report['ollama_model_present'] else 'No'})",
            f"LLM su GPU            : {gpu_line}",
            f"Policy Whisper/LLM    : {report['ai_parallel_policy']}",
            f"PyAudioWPatch         : {'Disponibile' if report['pyaudiowpatch_available'] else 'Assente'}",
            f"Default Microphone    : {report['default_microphone']}",
            f"Default Output API    : {report['default_output']}",
            f"WASAPI Loopback       : {report['wasapi_loopback']}",
            f"Outlook COM           : {'Disponibile' if report['outlook_com_available'] else 'Non disponibile (fallback attivo)'}",
            f"Configurazione Whisper: {report['whisper_configuration']}",
            f"Tkinter (cruscotto)   : {'Disponibile' if report['tkinter_available'] else 'Assente'}",
            f"Impostazioni          : {report['settings_path']}",
            f"Storico               : {report['history_db'] or 'Non disponibile'}",
            f"Esito Complessivo     : {report['overall_status']}",
        ]
        hint = gpu_info.get("hint")
        if hint:
            lines.append(f"Suggerimento GPU      : {hint}")
        sys.stdout.write("\n".join(lines) + "\n")
        sys.stdout.flush()

        if not report["ffmpeg_version"] or not report["ollama_reachable"]:
            return EXIT_MISSING_DEPENDENCY
        return EXIT_SUCCESS

    def _perform_coordinated_shutdown(self) -> None:
        """Arresto coordinato senza troncare WAV o corrompere state.json (Sezione 28.5).

        La sessione live viene chiusa e resta riprendibile; il job in elaborazione si ferma al
        primo checkpoint con ShutdownRequested. Tutto viene ripreso al riavvio successivo.
        """
        self.shutdown_event.set()
        self.finalize_event.set()
        self.processor.cancel_current()

        live = self.live
        self.live = None
        if live is not None:
            try:
                if live.recorder is not None and live.recorder.is_recording:
                    live.recorder.stop_capture()
            except Exception as exc:
                self.logger.warning("Errore chiusura recorder in shutdown: %s", exc)
            if live.transcriber is not None:
                live.transcriber.cancel()
            live.reporter.set_stage(Stage.SHUTTING_DOWN, f"Shutdown ({self._shutdown_reason.value})")
            self._publish_job(live.session_id, status="interrupted", detail="Registrazione interrotta allo spegnimento")
            live.state_manager.release_owner_lock()

        worker = self.worker
        if worker is not None and worker.is_alive():
            worker.join(timeout=20.0)
            if worker.is_alive():
                self.logger.warning("Il worker di elaborazione non si è fermato entro il timeout.")
        self.worker = None

        for job in self.processing_queue.snapshot():
            self.processing_queue.remove(job.session_id)
            if job.runtime is not None:
                if job.runtime.transcriber is not None:
                    job.runtime.transcriber.cancel()
                job.runtime.state_manager.release_owner_lock()

        self.engine.stop(timeout=10.0)

        writer = self.event_writer
        if writer is not None:
            writer.flush(timeout=3.0)
            writer.stop(timeout=3.0)
        self.event_writer = None

        self.tray.set_stage(Stage.SHUTTING_DOWN, "Arresto completato")
        self.tray.stop()
        self._services_started = False
