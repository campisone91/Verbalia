from __future__ import annotations

import gc
import queue
import threading
import time
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import TYPE_CHECKING

from config import (
    WHISPER_BEAM_SIZE,
    WHISPER_CONDITION_ON_PREVIOUS_TEXT,
    WHISPER_CPU_COMPUTE_TYPE,
    WHISPER_CPU_MODEL,
    WHISPER_CPU_THREADS,
    WHISPER_DEVICE,
    WHISPER_ENGINE_IDLE_UNLOAD_SEC,
    WHISPER_GPU_COMPUTE_TYPE,
    WHISPER_GPU_MODEL,
    WHISPER_LANGUAGE,
    WHISPER_MIN_SILENCE_DURATION_MS,
    WHISPER_SPEECH_PAD_MS,
    WHISPER_WORD_TIMESTAMPS,
)
from modules.contracts import (
    AudioChunk,
    ChunkStatus,
    SessionContext,
    ShutdownRequested,
    Stage,
    StageReporter,
    TrackLabel,
    TranscriptionArtifact,
    TranscriptionError,
    TranscriptSegment,
    atomic_write_text,
)
from modules.diarizer import SessionSpeakerTracker
from modules.logging_setup import get_logger, set_thread_log_session_id
from modules.transcript_utils import (
    apply_custom_glossary_normalization,
    format_chunk_transcript,
    resolve_glossary,
    validate_chunk_transcript_file,
)

if TYPE_CHECKING:
    from modules.state_manager import StateManager

logger = get_logger("transcriber")

_WAV_DELETE_MAX_RETRIES: int = 3
_WAV_DELETE_BASE_DELAY_SEC: float = 0.05
_TRANSCRIBER_QUEUE_MAXSIZE: int = 64

# Esito del controllo CUDA memorizzato per processo: evita 4-14 s persi a ogni sessione su PC senza NVIDIA
_CUDA_PROBE_LOCK = threading.Lock()
_CUDA_DEVICE_COUNT: int | None = None
_CUDA_LOAD_FAILED: bool = False


def cuda_device_count() -> int:
    """Restituisce il numero di GPU CUDA viste da CTranslate2 (0 se assenti), memorizzando l'esito."""
    global _CUDA_DEVICE_COUNT
    with _CUDA_PROBE_LOCK:
        if _CUDA_DEVICE_COUNT is None:
            try:
                import ctranslate2

                _CUDA_DEVICE_COUNT = int(ctranslate2.get_cuda_device_count())
            except Exception as exc:
                logger.debug("Rilevamento GPU CUDA non disponibile: %s", exc)
                _CUDA_DEVICE_COUNT = 0
            logger.info("GPU CUDA rilevate da CTranslate2: %d", _CUDA_DEVICE_COUNT)
        return _CUDA_DEVICE_COUNT


def _default_whisper_model_factory(
    model_name: str,
    device: str,
    compute_type: str,
    cpu_threads: int | None = None,
) -> object:
    """Inizializza un'istanza di faster_whisper.WhisperModel."""
    from faster_whisper import WhisperModel

    if device == "cpu":
        return WhisperModel(
            model_name,
            device=device,
            compute_type=compute_type,
            cpu_threads=int(cpu_threads if cpu_threads is not None else WHISPER_CPU_THREADS),
        )
    return WhisperModel(
        model_name,
        device=device,
        compute_type=compute_type,
    )


def _resolve_model_factory() -> Callable[..., object]:
    """Risolve la factory al momento del caricamento: i test possono sostituire quella di modulo."""
    return _default_whisper_model_factory


class _WhisperModelHolder:
    """Caricamento lazy del modello faster-whisper con fallback GPU->CPU e rilascio esplicito."""

    def __init__(
        self,
        *,
        model_factory: Callable[..., object] | None = None,
        cpu_model: str | None = None,
        cpu_threads: int | None = None,
    ) -> None:
        self._model_factory = model_factory
        self.cpu_model: str = cpu_model or WHISPER_CPU_MODEL
        self.cpu_threads: int = int(cpu_threads if cpu_threads is not None else WHISPER_CPU_THREADS)
        self.lock = threading.RLock()
        self.model: object | None = None
        self.active_model_name: str | None = None
        self.active_device: str | None = None
        self.active_compute_type: str | None = None

    def _call_factory(self, model_name: str, device: str, compute_type: str) -> object:
        factory = self._model_factory if self._model_factory is not None else _resolve_model_factory()
        if factory is _default_whisper_model_factory:
            return _default_whisper_model_factory(
                model_name, device, compute_type, cpu_threads=self.cpu_threads
            )
        return factory(model_name, device, compute_type)

    def ensure_loaded(self) -> object:
        """Carica il modello Whisper tentando CUDA solo se disponibile, altrimenti CPU int8."""
        global _CUDA_LOAD_FAILED
        with self.lock:
            if self.model is not None:
                return self.model

            device_pref = WHISPER_DEVICE.lower()
            prefer_gpu = device_pref == "cuda" or (
                device_pref == "auto" and not _CUDA_LOAD_FAILED and cuda_device_count() > 0
            )
            gpu_error: Exception | None = None

            if prefer_gpu:
                try:
                    logger.info(
                        "Tentativo caricamento Whisper su GPU: model=%s device=cuda compute_type=%s",
                        WHISPER_GPU_MODEL,
                        WHISPER_GPU_COMPUTE_TYPE,
                    )
                    model = self._call_factory(WHISPER_GPU_MODEL, "cuda", WHISPER_GPU_COMPUTE_TYPE)
                    self.model = model
                    self.active_model_name = WHISPER_GPU_MODEL
                    self.active_device = "cuda"
                    self.active_compute_type = WHISPER_GPU_COMPUTE_TYPE
                    logger.info("Modello Whisper GPU inizializzato con successo.")
                    return model
                except Exception as exc:
                    gpu_error = exc
                    _CUDA_LOAD_FAILED = True
                    logger.warning(
                        "Inizializzazione Whisper su GPU fallita (%s). Fallback su CPU int8.",
                        exc,
                    )

            try:
                started = time.monotonic()
                logger.info(
                    "Caricamento Whisper su CPU: model=%s device=cpu compute_type=%s cpu_threads=%d",
                    self.cpu_model,
                    WHISPER_CPU_COMPUTE_TYPE,
                    self.cpu_threads,
                )
                model = self._call_factory(self.cpu_model, "cpu", WHISPER_CPU_COMPUTE_TYPE)
                self.model = model
                self.active_model_name = self.cpu_model
                self.active_device = "cpu"
                self.active_compute_type = WHISPER_CPU_COMPUTE_TYPE
                logger.info(
                    "Modello Whisper CPU inizializzato in %.1fs.", time.monotonic() - started
                )
                return model
            except Exception as cpu_exc:
                raise TranscriptionError(
                    f"Impossibile caricare il modello Whisper (GPU error: {gpu_error}; CPU error: {cpu_exc})"
                ) from cpu_exc

    def release(self) -> bool:
        with self.lock:
            had_model = self.model is not None
            self.model = None
            self.active_model_name = None
            self.active_device = None
            self.active_compute_type = None
            gc.collect()
            return had_model


class WhisperEngine:
    """Motore Whisper condiviso tra sessioni: un solo modello in memoria e un solo thread.

    Serve per primi i chunk del job in elaborazione (priorità 0) e poi quelli della call
    in registrazione (priorità 1); a parità di priorità vale l'ordine di arrivo. Dopo
    idle_unload_sec senza lavoro il modello viene scaricato per liberare RAM.
    """

    PRIORITY_PROCESSING: int = 0
    PRIORITY_LIVE: int = 1

    def __init__(
        self,
        *,
        whisper_lock: object | None = None,
        model_factory: Callable[..., object] | None = None,
        cpu_model: str | None = None,
        cpu_threads: int | None = None,
        beam_size: int | None = None,
        language: str | None = None,
        idle_unload_sec: float | None = None,
    ) -> None:
        self._holder = _WhisperModelHolder(
            model_factory=model_factory, cpu_model=cpu_model, cpu_threads=cpu_threads
        )
        self.whisper_lock: object = whisper_lock if whisper_lock is not None else threading.RLock()
        self.beam_size: int = int(beam_size if beam_size is not None else WHISPER_BEAM_SIZE)
        self.language: str = language or WHISPER_LANGUAGE
        self._idle_unload_sec: float = float(
            idle_unload_sec if idle_unload_sec is not None else WHISPER_ENGINE_IDLE_UNLOAD_SEC
        )
        self._cond = threading.Condition()
        self._items: list[tuple[int, RollingTranscriber, AudioChunk]] = []
        self._seq: int = 0
        self._busy: bool = False
        self._stopping: bool = False
        self._thread: threading.Thread | None = None
        self._last_activity: float = time.monotonic()

    @property
    def active_device(self) -> str | None:
        return self._holder.active_device

    @property
    def model_name(self) -> str:
        return self._holder.cpu_model

    @property
    def is_model_loaded(self) -> bool:
        return self._holder.model is not None

    def pending_count(self) -> int:
        with self._cond:
            return len(self._items) + (1 if self._busy else 0)

    def start(self) -> None:
        with self._cond:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stopping = False
            self._thread = threading.Thread(target=self._run, name="WhisperEngine", daemon=True)
            self._thread.start()
            logger.info(
                "WhisperEngine avviato (model=%s, cpu_threads=%d).",
                self._holder.cpu_model,
                self._holder.cpu_threads,
            )

    def stop(self, timeout: float = 10.0) -> None:
        with self._cond:
            self._stopping = True
            self._cond.notify_all()
            thread = self._thread
        if thread is not None and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=timeout)
        self.release_model()

    def ensure_model_loaded(self) -> object:
        return self._holder.ensure_loaded()

    def release_model(self) -> None:
        with self._holder.lock:
            if self._holder.release():
                logger.info("Modello Whisper del motore condiviso scaricato dalla memoria.")

    def submit(self, client: RollingTranscriber, chunk: AudioChunk) -> None:
        with self._cond:
            if self._stopping:
                raise TranscriptionError("WhisperEngine in arresto: chunk non accodato.")
            self._seq += 1
            self._items.append((self._seq, client, chunk))
            self._cond.notify_all()
        self.start()

    def cancel(self, client: RollingTranscriber) -> int:
        """Rimuove dalla coda i chunk non ancora avviati di un client; restituisce quanti erano."""
        with self._cond:
            kept = [item for item in self._items if item[1] is not client]
            removed = len(self._items) - len(kept)
            self._items = kept
        for _ in range(removed):
            client._mark_done()
        return removed

    def wake(self) -> None:
        """Rivaluta la coda (ad esempio dopo il cambio di priorità di un client)."""
        with self._cond:
            self._cond.notify_all()

    def _pick_next(self) -> tuple[RollingTranscriber, AudioChunk] | None:
        if not self._items:
            return None
        best_index = min(
            range(len(self._items)),
            key=lambda i: (self._items[i][1].priority, self._items[i][0]),
        )
        _seq, client, chunk = self._items.pop(best_index)
        return client, chunk

    def _run(self) -> None:
        while True:
            with self._cond:
                while not self._items and not self._stopping:
                    idle_for = time.monotonic() - self._last_activity
                    if self._holder.model is not None and idle_for >= self._idle_unload_sec:
                        break
                    remaining = (
                        max(1.0, self._idle_unload_sec - idle_for)
                        if self._holder.model is not None
                        else 30.0
                    )
                    self._cond.wait(timeout=remaining)
                if self._stopping:
                    return
                picked = self._pick_next()
                self._busy = picked is not None
            if picked is None:
                logger.info(
                    "WhisperEngine inattivo da %.0fs: scaricamento del modello.", self._idle_unload_sec
                )
                self.release_model()
                self._last_activity = time.monotonic()
                continue
            client, chunk = picked
            try:
                client._run_engine_item(chunk)
            finally:
                with self._cond:
                    self._busy = False
                    self._last_activity = time.monotonic()
                    self._cond.notify_all()


class RollingTranscriber:
    """Trascrizione rolling dei chunk di una sessione con faster-whisper.

    In modalità autonoma ha un proprio thread e un proprio modello; con un WhisperEngine
    diventa un client della singola sessione che delega al motore condiviso.
    """

    def __init__(
        self,
        session_dir: Path,
        state_manager: StateManager | None = None,
        stage_reporter: StageReporter | None = None,
        *,
        ai_lock: threading.RLock | None = None,
        model_factory: Callable[[str, str, str], object] | None = None,
        on_artifact_ready: Callable[[TranscriptionArtifact], None] | None = None,
        glossary: str | None = None,
        session_id: str | None = None,
        engine: WhisperEngine | None = None,
        priority: int = WhisperEngine.PRIORITY_LIVE,
        speaker_tracker: SessionSpeakerTracker | None = None,
    ) -> None:
        self._session_dir: Path = session_dir.resolve()
        self._state_manager: StateManager | None = state_manager
        self._stage_reporter: StageReporter | None = stage_reporter
        self._engine: WhisperEngine | None = engine
        self._ai_lock: object = (
            engine.whisper_lock
            if engine is not None
            else (ai_lock if ai_lock is not None else threading.RLock())
        )
        self._holder = _WhisperModelHolder(model_factory=model_factory)
        self._glossary: str = resolve_glossary(glossary)
        self._session_id: str | None = session_id
        self._speaker_tracker: SessionSpeakerTracker | None = (
            speaker_tracker
            if speaker_tracker is not None
            else SessionSpeakerTracker(session_dir=self._session_dir)
        )
        self._beam_size: int = engine.beam_size if engine is not None else WHISPER_BEAM_SIZE
        self._language: str = engine.language if engine is not None else WHISPER_LANGUAGE
        self.priority: int = int(priority)
        self._on_artifact_ready: Callable[[TranscriptionArtifact], None] | None = (
            on_artifact_ready
        )

        self._model_lock: threading.RLock = threading.RLock()
        self._pending_cond = threading.Condition()
        self._pending: int = 0
        self._submitted: set[int] = set()

        self._queue: queue.Queue[AudioChunk | None] = queue.Queue(
            maxsize=_TRANSCRIBER_QUEUE_MAXSIZE
        )
        self._worker_thread: threading.Thread | None = None
        self._started: bool = False
        self._finished: bool = False
        self._worker_error: Exception | None = None
        self._artifacts: list[TranscriptionArtifact] = []

    @property
    def speaker_tracker(self) -> SessionSpeakerTracker | None:
        """Restituisce il SessionSpeakerTracker associato alla sessione."""
        return self._speaker_tracker

    @property
    def artifacts(self) -> list[TranscriptionArtifact]:
        """Restituisce la copia degli artefatti di trascrizione completati."""
        with self._model_lock:
            return list(self._artifacts)

    @property
    def last_error(self) -> Exception | None:
        """Restituisce l'ultimo errore fatale riscontrato dal worker, se presente."""
        return self._worker_error

    @property
    def active_device(self) -> str | None:
        """Restituisce il device Whisper attualmente inizializzato ('cuda' o 'cpu')."""
        if self._engine is not None:
            return self._engine.active_device
        return self._holder.active_device

    @property
    def pending(self) -> int:
        """Chunk accodati e non ancora trascritti."""
        with self._pending_cond:
            return self._pending

    @property
    def is_busy(self) -> bool:
        """True se ci sono chunk accodati o in fase di trascrizione."""
        return self.pending > 0

    @property
    def finished(self) -> bool:
        """True dopo finish()/cancel(): il client non accetta altri chunk."""
        return self._finished

    @property
    def submitted_indices(self) -> set[int]:
        """Indici dei chunk già accodati da questo client."""
        with self._pending_cond:
            return set(self._submitted)

    def cancel(self) -> None:
        """Chiude il client e annulla i chunk non ancora avviati nel motore condiviso."""
        self.finish()
        if self._engine is not None:
            removed = self._engine.cancel(self)
            if removed:
                logger.info("Annullati %d chunk in coda per la sessione.", removed)

    def set_priority(self, priority: int) -> None:
        """Cambia la priorità dei chunk di questa sessione nel motore condiviso."""
        self.priority = int(priority)
        if self._engine is not None:
            self._engine.wake()

    def start(self) -> None:
        """Avvia il thread worker di trascrizione (o il motore condiviso)."""
        with self._model_lock:
            if self._started:
                return
            self._started = True
            if self._engine is not None:
                self._engine.start()
                return
            self._worker_thread = threading.Thread(
                target=self._worker_loop,
                name="RollingTranscriberWorker",
                daemon=False,
            )
            self._worker_thread.start()
            logger.info(
                "RollingTranscriber avviato per session_dir=%s",
                self._session_dir.name,
            )

    def submit(self, chunk: AudioChunk) -> None:
        """Accoda un AudioChunk normalizzato per la trascrizione."""
        if self._finished:
            raise TranscriptionError(
                "Impossibile accodare un nuovo chunk dopo la chiamata a finish()."
            )
        chunk.status = ChunkStatus.QUEUED
        with self._pending_cond:
            self._pending += 1
            self._submitted.add(chunk.index)
        if self._engine is not None:
            self._engine.submit(self, chunk)
        else:
            self._queue.put(chunk)
        logger.info("Chunk %03d accodato per trascrizione.", chunk.index)

    def finish(self) -> None:
        """Segnala che non arriveranno altri chunk (sentinella None in modalità autonoma)."""
        with self._model_lock:
            if self._finished:
                return
            self._finished = True
            if self._engine is None:
                self._queue.put(None)
            logger.info("Fine dei chunk da trascrivere segnalata.")

    def _raise_worker_error(self) -> None:
        if self._worker_error is not None:
            if isinstance(self._worker_error, TranscriptionError):
                raise self._worker_error
            raise TranscriptionError(
                f"Errore nel worker di trascrizione: {self._worker_error}"
            ) from self._worker_error

    def join(self, timeout: float | None = None) -> None:
        """Attende la trascrizione di tutti i chunk accodati e propaga eventuali errori."""
        if self._engine is not None:
            deadline = None if timeout is None else time.monotonic() + timeout
            with self._pending_cond:
                while self._pending > 0:
                    remaining = None if deadline is None else deadline - time.monotonic()
                    if remaining is not None and remaining <= 0:
                        raise TranscriptionError(
                            "Timeout scaduto in attesa del termine della trascrizione."
                        )
                    self._pending_cond.wait(timeout=remaining)
            self._raise_worker_error()
            return

        worker = self._worker_thread
        if worker is not None:
            worker.join(timeout=timeout)
            if timeout is not None and worker.is_alive():
                raise TranscriptionError(
                    "Timeout scaduto in attesa del termine di RollingTranscriber."
                )
        self._raise_worker_error()

    def drain(self, shutdown_event: threading.Event | None = None, poll_interval_sec: float = 0.5) -> None:
        """Attende a polling la fine della trascrizione, interrompibile dallo shutdown (niente timeout fisso)."""
        self.finish()
        while True:
            if shutdown_event is not None and shutdown_event.is_set():
                if self.pending > 0 or (
                    self._worker_thread is not None and self._worker_thread.is_alive()
                ):
                    raise ShutdownRequested("Trascrizione interrotta: arresto richiesto.")
            try:
                self.join(timeout=poll_interval_sec)
                return
            except TranscriptionError as exc:
                if "Timeout scaduto" not in str(exc):
                    raise

    def release_model(self) -> None:
        """Rilascia il modello faster-whisper (in modalità client lo gestisce il motore condiviso)."""
        if self._engine is not None:
            logger.debug("Modello Whisper condiviso: rilascio demandato al WhisperEngine.")
            return
        with self._ai_lock, self._model_lock:  # type: ignore[attr-defined]
            had_model = self._holder.release()
            logger.info(
                "Modello Whisper rilasciato (era_caricato=%s).",
                had_model,
            )

    def _ensure_model_loaded(self) -> object:
        """Carica (o riusa) il modello Whisper."""
        if self._engine is not None:
            return self._engine.ensure_model_loaded()
        return self._holder.ensure_loaded()

    def _run_engine_item(self, chunk: AudioChunk) -> None:
        """Eseguito dal thread del WhisperEngine per un chunk di questa sessione."""
        set_thread_log_session_id(self._session_id)
        try:
            self.transcribe_chunk_sync(chunk)
        except Exception as exc:
            self._handle_worker_error(exc)
        finally:
            set_thread_log_session_id(None)
            self._mark_done()

    def _mark_done(self) -> None:
        with self._pending_cond:
            self._pending = max(0, self._pending - 1)
            self._pending_cond.notify_all()

    def _handle_worker_error(self, exc: Exception) -> None:
        self._worker_error = exc
        logger.error(
            "Errore durante la trascrizione nel worker: %s",
            exc,
            exc_info=True,
        )
        if self._state_manager is not None:
            record_err = getattr(self._state_manager, "record_error", None)
            if callable(record_err):
                try:
                    record_err(Stage.TRANSCRIBING_FINAL, exc, affects_stage=False)
                except Exception as state_exc:
                    logger.warning(
                        "Impossibile registrare l'errore nello StateManager: %s",
                        state_exc,
                    )

    def _transcribe_single_track(
        self,
        model: object,
        audio_path: Path,
        track_label: TrackLabel,
        start_offset_sec: float,
    ) -> list[TranscriptSegment]:
        """Trascrive un singolo file WAV 16kHz e converte i timestamp in assoluti."""
        if not audio_path.exists() or not audio_path.is_file():
            raise TranscriptionError(
                f"File audio per traccia {track_label.value} non trovato: {audio_path}"
            )

        transcribe_fn = getattr(model, "transcribe", None)
        if not callable(transcribe_fn):
            raise TranscriptionError(
                "L'istanza del modello Whisper non espone un metodo transcribe() valido."
            )

        try:
            result = transcribe_fn(
                str(audio_path),
                language=self._language,
                task="transcribe",
                beam_size=self._beam_size,
                vad_filter=True,
                vad_parameters={
                    "min_silence_duration_ms": WHISPER_MIN_SILENCE_DURATION_MS,
                    "speech_pad_ms": WHISPER_SPEECH_PAD_MS,
                },
                initial_prompt=self._glossary or None,
                condition_on_previous_text=WHISPER_CONDITION_ON_PREVIOUS_TEXT,
                word_timestamps=WHISPER_WORD_TIMESTAMPS,
            )
            if not isinstance(result, tuple) or len(result) < 1:
                raise TranscriptionError(
                    "Risultato inatteso da model.transcribe(): attesa tupla (segments, info)."
                )
            raw_segments_iter = result[0]
            if not isinstance(raw_segments_iter, Iterable):
                raise TranscriptionError(
                    "L'oggetto segments restituito da Whisper non è iterabile."
                )
            materialized_segments = list(raw_segments_iter)
        except TranscriptionError:
            raise
        except Exception as exc:
            raise TranscriptionError(
                f"Errore durante la trascrizione della traccia {track_label.value} ({audio_path.name}): {exc}"
            ) from exc

        parsed_segments: list[TranscriptSegment] = []
        for raw_seg in materialized_segments:
            raw_text = str(getattr(raw_seg, "text", "")).strip()
            if not raw_text:
                continue
            normalized_text = apply_custom_glossary_normalization(
                raw_text,
                self._glossary,
            )
            clean_text = " ".join(normalized_text.splitlines()).strip()
            if not clean_text:
                continue

            seg_start = float(getattr(raw_seg, "start", 0.0))
            seg_end = float(getattr(raw_seg, "end", seg_start))
            absolute_start = max(0.0, start_offset_sec + seg_start)
            absolute_end = max(absolute_start, start_offset_sec + seg_end)

            avg_logprob = getattr(raw_seg, "avg_logprob", None)
            confidence = (
                float(avg_logprob)
                if isinstance(avg_logprob, (int, float))
                and not isinstance(avg_logprob, bool)
                else None
            )

            parsed_segments.append(
                TranscriptSegment(
                    track=track_label,
                    start_sec=absolute_start,
                    end_sec=absolute_end,
                    text=clean_text,
                    confidence=confidence,
                )
            )

        return parsed_segments

    def _update_state_transcribed(self, chunk_index: int) -> None:
        """Registra il chunk come trascritto nello StateManager, se presente."""
        if self._state_manager is None:
            return

        counts: list[int] = [0, 0]

        def _apply(ctx: SessionContext) -> None:
            if chunk_index not in ctx.transcribed_chunks:
                ctx.transcribed_chunks.append(chunk_index)
                ctx.transcribed_chunks.sort()
            all_known = (
                set(ctx.recorded_chunks)
                | set(ctx.normalized_chunks)
                | set(ctx.transcribed_chunks)
            )
            counts[0] = len(ctx.transcribed_chunks)
            counts[1] = max(len(all_known), counts[0])

        mutate_fn = getattr(self._state_manager, "mutate", None)
        if callable(mutate_fn):
            mutate_fn(_apply)
        else:
            ctx = self._state_manager.load()
            _apply(ctx)
            self._state_manager.save(ctx)

        progress_fn = getattr(self._stage_reporter, "progress_steps", None)
        if callable(progress_fn) and counts[1] > 0:
            with self._pending_cond:
                total_known = max(counts[1], len(self._submitted))
            try:
                progress_fn(counts[0], total_known)
            except Exception as exc:
                logger.debug("Aggiornamento progress_steps trascrizione non riuscito: %s", exc)

    def _update_state_audio_deleted(self, chunk_index: int) -> None:
        """Registra l'avvenuta cancellazione dei WAV 16k del chunk nello StateManager."""
        if self._state_manager is None:
            return

        def _apply(ctx: SessionContext) -> None:
            if chunk_index not in ctx.audio_deleted_chunks:
                ctx.audio_deleted_chunks.append(chunk_index)
                ctx.audio_deleted_chunks.sort()

        mutate_fn = getattr(self._state_manager, "mutate", None)
        if callable(mutate_fn):
            mutate_fn(_apply)
        else:
            ctx = self._state_manager.load()
            _apply(ctx)
            self._state_manager.save(ctx)

    def _delete_wav_with_retry(self, wav_path: Path) -> bool:
        """Elimina un file WAV con retry e backoff in caso di file bloccato."""
        if not wav_path.exists():
            return True

        for attempt in range(_WAV_DELETE_MAX_RETRIES):
            try:
                wav_path.unlink()
                return True
            except OSError as exc:
                if attempt + 1 < _WAV_DELETE_MAX_RETRIES:
                    delay = _WAV_DELETE_BASE_DELAY_SEC * (2**attempt)
                    time.sleep(delay)
                else:
                    logger.warning(
                        "Impossibile eliminare il file WAV %s dopo %d tentativi: %s",
                        wav_path.name,
                        _WAV_DELETE_MAX_RETRIES,
                        exc,
                    )
        return not wav_path.exists()

    def transcribe_chunk_sync(
        self,
        chunk: AudioChunk,
        speaker_tracker: SessionSpeakerTracker | None = None,
    ) -> TranscriptionArtifact:
        """Trascrive in modo sincrono entrambe le tracce del chunk, valida l'output ed elimina i WAV."""
        started_monotonic = time.monotonic()
        with self._ai_lock:  # type: ignore[attr-defined]
            chunk.status = ChunkStatus.TRANSCRIBING
            try:
                if not chunk.io_wav_path.exists() or not chunk.io_wav_path.is_file():
                    raise TranscriptionError(
                        f"File WAV IO mancante per chunk {chunk.index:03d}: {chunk.io_wav_path}"
                    )
                if (
                    not chunk.others_wav_path.exists()
                    or not chunk.others_wav_path.is_file()
                ):
                    raise TranscriptionError(
                        f"File WAV ALTRI mancante per chunk {chunk.index:03d}: {chunk.others_wav_path}"
                    )

                model = self._ensure_model_loaded()

                io_segments = self._transcribe_single_track(
                    model,
                    chunk.io_wav_path,
                    TrackLabel.IO,
                    chunk.start_offset_sec,
                )
                others_segments = self._transcribe_single_track(
                    model,
                    chunk.others_wav_path,
                    TrackLabel.OTHERS,
                    chunk.start_offset_sec,
                )

                tracker = speaker_tracker if speaker_tracker is not None else self._speaker_tracker
                if tracker is not None and getattr(tracker, "enabled", False):
                    try:
                        chunk_offset = getattr(chunk, "offset_sec", chunk.start_offset_sec)
                        others_segments = tracker.assign_speakers_to_segments(
                            wav_path=chunk.others_wav_path,
                            segments=others_segments,
                            chunk_offset_sec=chunk_offset,
                        )
                    except Exception as diar_exc:
                        logger.warning(
                            "Diarizzazione chunk #%03d non riuscita (fallback [ALTRI]): %s",
                            chunk.index,
                            diar_exc,
                        )

                combined_segments = io_segments + others_segments
                combined_segments.sort(
                    key=lambda seg: (
                        seg.start_sec,
                        seg.end_sec,
                        0 if seg.track == TrackLabel.IO else 1,
                    )
                )

                transcript_content = format_chunk_transcript(
                    chunk_index=chunk.index,
                    session_id=chunk.session_id,
                    start_offset_sec=chunk.start_offset_sec,
                    end_offset_sec=chunk.end_offset_sec,
                    segments=combined_segments,
                )

                transcripts_dir = self._session_dir / "transcripts"
                transcript_path = transcripts_dir / f"chunk_{chunk.index:03d}.txt"

                atomic_write_text(transcript_path, transcript_content)
                artifact = validate_chunk_transcript_file(transcript_path)

                chunk.status = ChunkStatus.TRANSCRIBED
                self._update_state_transcribed(chunk.index)

                io_deleted = self._delete_wav_with_retry(chunk.io_wav_path)
                others_deleted = self._delete_wav_with_retry(chunk.others_wav_path)
                if io_deleted and others_deleted:
                    chunk.status = ChunkStatus.AUDIO_DELETED
                    self._update_state_audio_deleted(chunk.index)

                with self._model_lock:
                    self._artifacts.append(artifact)

                if self._on_artifact_ready is not None:
                    self._on_artifact_ready(artifact)

                elapsed = time.monotonic() - started_monotonic
                logger.info(
                    "Chunk %03d trascritto: segmenti=%d sha256=%s durata_sec=%.2f audio_eliminato=%s",
                    chunk.index,
                    artifact.segment_count,
                    artifact.sha256[:12],
                    elapsed,
                    chunk.status == ChunkStatus.AUDIO_DELETED,
                )
                return artifact
            except Exception as exc:
                chunk.status = ChunkStatus.FAILED
                if isinstance(exc, TranscriptionError):
                    raise
                raise TranscriptionError(
                    f"Fallimento trascrizione chunk {chunk.index:03d}: {exc}"
                ) from exc

    def _worker_loop(self) -> None:
        """Loop del worker che consuma AudioChunk dalla queue fino alla sentinella None."""
        set_thread_log_session_id(self._session_id)
        while True:
            item = self._queue.get()
            try:
                if item is None:
                    logger.info("Sentinella ricevuta: arresto del worker RollingTranscriber.")
                    break
                try:
                    self.transcribe_chunk_sync(item)
                finally:
                    self._mark_done()
            except Exception as exc:
                self._handle_worker_error(exc)
            finally:
                self._queue.task_done()

    _process_chunk = transcribe_chunk_sync

