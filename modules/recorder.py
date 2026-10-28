from __future__ import annotations

import collections
import ctypes
import queue
import shutil
import threading
import time
import wave
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING

from config import (
    AUDIO_BLOCK_MS,
    AUDIO_QUEUE_SECONDS,
    CHUNK_DURATION_SEC,
    CHUNK_OVERLAP_SEC,
    DEVICE_RECONNECT_DELAY_SEC,
    DEVICE_RECONNECT_MAX_ATTEMPTS,
    FFMPEG_LOCAL_PATH,
    LOOPBACK_GAP_TOLERANCE_MS,
    MAX_SESSION_DURATION_SEC,
    MAX_SILENCE_INJECTION_MS,
    MAX_TRACK_DRIFT_MS,
)
from modules.audio_utils import (
    calculate_missing_frames,
    compute_silence_injection_frames,
    compute_track_drift_ms,
    discover_audio_devices,
    generate_silence_bytes,
    normalize_wav_with_ffmpeg,
)
from modules.contracts import (
    AudioChunk,
    AudioDeviceError,
    AudioProcessingError,
    CapturedBlock,
    ChunkStatus,
    DeviceDescriptor,
    ShutdownRequested,
    Stage,
    StageReporter,
    TrackLabel,
    now_tz,
)
from modules.logging_setup import get_logger, set_thread_log_session_id

if TYPE_CHECKING:
    from modules.state_manager import StateManager

try:
    import pyaudiowpatch
except ImportError:  # pragma: no cover - gestito in ambienti senza PyAudioWPatch
    pyaudiowpatch = None  # type: ignore[assignment]

logger = get_logger("recorder")

ES_CONTINUOUS: int = 0x80000000
ES_SYSTEM_REQUIRED: int = 0x00000001
PA_CONTINUE: int = int(getattr(pyaudiowpatch, "paContinue", 0))
PA_INT16: int = int(getattr(pyaudiowpatch, "paInt16", 8))
PA_INPUT_UNDERFLOW: int = int(getattr(pyaudiowpatch, "paInputUnderflow", 0x1))
PA_INPUT_OVERFLOW: int = int(getattr(pyaudiowpatch, "paInputOverflow", 0x2))
THREAD_PRIORITY_ABOVE_NORMAL: int = 1
_STATUS_EVENTS_MAXLEN: int = 4096


def raise_current_thread_priority(priority: int = THREAD_PRIORITY_ABOVE_NORMAL) -> bool:
    """Alza la priorità del thread corrente (cattura e assembler non devono perdere campioni)."""
    try:
        windll = getattr(ctypes, "windll", None)
        if windll is None or not hasattr(windll, "kernel32"):
            return False
        kernel32 = windll.kernel32
        return bool(kernel32.SetThreadPriority(kernel32.GetCurrentThread(), int(priority)))
    except Exception as exc:
        logger.debug("SetThreadPriority non riuscito: %s", exc)
        return False


def describe_pa_status_flags(flags: int) -> str:
    """Traduce i bit di status_flags di PortAudio in una descrizione leggibile."""
    names: list[str] = []
    if flags & PA_INPUT_OVERFLOW:
        names.append("input_overflow")
    if flags & PA_INPUT_UNDERFLOW:
        names.append("input_underflow")
    remaining = flags & ~(PA_INPUT_OVERFLOW | PA_INPUT_UNDERFLOW)
    if remaining:
        names.append(f"flags_0x{remaining:x}")
    return "+".join(names) if names else "none"


def prevent_system_sleep() -> None:
    """Impedisce lo standby di sistema durante la registrazione (Sezione 20.10)."""
    try:
        kernel32 = getattr(ctypes, "windll", None)
        if kernel32 is not None and hasattr(kernel32, "kernel32"):
            kernel32.kernel32.SetThreadExecutionState(
                ES_CONTINUOUS | ES_SYSTEM_REQUIRED
            )
    except Exception as exc:
        logger.warning("Impossibile impostare SetThreadExecutionState(prevent): %s", exc)


def restore_system_sleep() -> None:
    """Ripristina le normali impostazioni di standby del sistema (Sezione 20.10)."""
    try:
        kernel32 = getattr(ctypes, "windll", None)
        if kernel32 is not None and hasattr(kernel32, "kernel32"):
            kernel32.kernel32.SetThreadExecutionState(ES_CONTINUOUS)
    except Exception as exc:
        logger.warning("Impossibile ripristinare SetThreadExecutionState(restore): %s", exc)


def compute_audio_queue_size(
    block_ms: int = AUDIO_BLOCK_MS,
    queue_seconds: int = AUDIO_QUEUE_SECONDS,
) -> int:
    """Calcola la capacità massima della queue audio bounded (Sezione 20.2)."""
    blocks_per_second = max(1, 1000 // max(1, block_ms))
    return max(1, blocks_per_second * max(1, queue_seconds))


def enqueue_captured_block(
    block_queue: queue.Queue[CapturedBlock],
    block: CapturedBlock,
    discontinuities: list[dict[str, object]],
    lock: threading.RLock | None = None,
) -> bool:
    """Accoda in modo non bloccante un CapturedBlock; se la coda è piena registra una discontinuità."""
    try:
        block_queue.put_nowait(block)
        return True
    except queue.Full:
        entry: dict[str, object] = {
            "track": block.track.value,
            "reason": "queue_full",
            "captured_at_ns": block.captured_at_ns,
            "dropped_frames": block.frame_count,
            "occurred_at": now_tz().isoformat(),
        }
        if lock is not None:
            with lock:
                discontinuities.append(entry)
        else:
            discontinuities.append(entry)
        return False


class _BaseCaptureThread(threading.Thread):
    """Classe base per i thread di acquisizione audio PyAudioWPatch (Sezione 20.4)."""

    def __init__(
        self,
        track: TrackLabel,
        device: DeviceDescriptor,
        block_queue: queue.Queue[CapturedBlock],
        stop_event: threading.Event,
        discontinuities: list[dict[str, object]],
        *,
        on_device_error: Callable[[TrackLabel, Exception], None] | None = None,
        pyaudio_instance: object | None = None,
        block_ms: int = AUDIO_BLOCK_MS,
        lock: threading.RLock | None = None,
    ) -> None:
        super().__init__(
            name=f"CaptureThread-{track.value}",
            daemon=True,
        )
        self.track = track
        self.device = device
        self.block_queue = block_queue
        self.stop_event = stop_event
        self.discontinuities = discontinuities
        self.on_device_error = on_device_error
        self.pyaudio_instance = pyaudio_instance
        self.block_ms = max(1, block_ms)
        self._lock = lock if lock is not None else threading.RLock()
        self._suspend_event = threading.Event()
        self._paused_event = threading.Event()
        self._stream: object | None = None
        self.log_session_id: str | None = None
        # deque.append è thread-safe e non blocca: il callback PortAudio non deve mai attendere lock
        self.status_events: collections.deque[tuple[int, int, int]] = collections.deque(
            maxlen=_STATUS_EVENTS_MAXLEN
        )

    @property
    def is_paused(self) -> bool:
        return self._paused_event.is_set()

    def pause(self) -> None:
        self._paused_event.set()

    def resume(self) -> None:
        self._paused_event.clear()

    def _enqueue_block(self, block: CapturedBlock) -> None:
        if self._paused_event.is_set():
            return
        enqueue_captured_block(self.block_queue, block, self.discontinuities, self._lock)

    @property
    def frames_per_buffer(self) -> int:
        return max(1, (self.device.sample_rate * self.block_ms) // 1000)

    def suspend(self) -> None:
        """Richiede la sospensione immediata dello stream audio."""
        self._suspend_event.set()

    def create_stream_callback(
        self,
    ) -> Callable[[bytes | None, int, object, int], tuple[None, int]]:
        """Restituisce il callback minimale per PyAudio (Sezione 20.4).

        Il callback acquisisce esclusivamente il timestamp, costruisce
        CapturedBlock, accoda senza bloccare e restituisce paContinue.
        """
        track = self.track
        sample_rate = self.device.sample_rate
        channels = self.device.channels
        sample_width = self.device.sample_width
        frame_bytes = max(1, channels * sample_width)
        target_queue = self.block_queue
        disc_list = self.discontinuities
        lock = self._lock
        status_events = self.status_events

        def _callback(
            in_data: bytes | None,
            frame_count: int,
            _time_info: object,
            status_flags: int,
        ) -> tuple[None, int]:
            captured_at_ns = time.monotonic_ns()
            if status_flags:
                status_events.append((captured_at_ns, int(status_flags), int(frame_count)))
            payload = bytes(in_data) if in_data is not None else b""
            actual_frames = (
                int(frame_count)
                if frame_count > 0
                else len(payload) // frame_bytes
            )
            if self._paused_event.is_set():
                return (None, PA_CONTINUE)
            if actual_frames > 0 and payload:
                block = CapturedBlock(
                    track=track,
                    captured_at_ns=captured_at_ns,
                    frame_count=actual_frames,
                    sample_rate=sample_rate,
                    channels=channels,
                    sample_width=sample_width,
                    payload=payload,
                )
                self._enqueue_block(block)
            return (None, PA_CONTINUE)

        return _callback

    def _close_stream_safely(self) -> None:
        stream = self._stream
        self._stream = None
        if stream is None:
            return
        try:
            if hasattr(stream, "is_active") and getattr(stream, "is_active")():
                if hasattr(stream, "stop_stream"):
                    getattr(stream, "stop_stream")()
        except Exception as exc:
            logger.debug("Errore stop_stream per %s: %s", self.track.value, exc)
        try:
            if hasattr(stream, "close"):
                getattr(stream, "close")()
        except Exception as exc:
            logger.debug("Errore close stream per %s: %s", self.track.value, exc)

    def run(self) -> None:
        set_thread_log_session_id(self.log_session_id)
        raise_current_thread_priority()
        if self.pyaudio_instance is None or not hasattr(self.pyaudio_instance, "open"):
            err = AudioDeviceError(
                f"Istanza PyAudio non valida per la traccia {self.track.value}"
            )
            if self.on_device_error is not None:
                self.on_device_error(self.track, err)
            return

        try:
            open_fn = getattr(self.pyaudio_instance, "open")
            self._stream = open_fn(
                format=PA_INT16,
                channels=self.device.channels,
                rate=self.device.sample_rate,
                input=True,
                input_device_index=self.device.index,
                frames_per_buffer=self.frames_per_buffer,
                stream_callback=self.create_stream_callback(),
            )
            if hasattr(self._stream, "start_stream"):
                getattr(self._stream, "start_stream")()

            while not self.stop_event.is_set() and not self._suspend_event.is_set():
                if hasattr(self._stream, "is_active"):
                    is_active = bool(getattr(self._stream, "is_active")())
                    if not is_active and not self.device.is_loopback:
                        raise AudioDeviceError(
                            f"Lo stream della traccia {self.track.value} è diventato inattivo."
                        )
                if self.stop_event.wait(timeout=0.05):
                    break
        except Exception as exc:
            logger.error(
                "Errore cattura audio sulla traccia %s (%s): %s",
                self.track.value,
                self.device.name,
                exc,
            )
            if (
                not self.stop_event.is_set()
                and not self._suspend_event.is_set()
                and self.on_device_error is not None
            ):
                self.on_device_error(self.track, exc)
        finally:
            self._close_stream_safely()


class MicCaptureThread(_BaseCaptureThread):
    """Thread di acquisizione per il microfono dell'utente (traccia IO)."""

    def __init__(
        self,
        device: DeviceDescriptor,
        block_queue: queue.Queue[CapturedBlock],
        stop_event: threading.Event,
        discontinuities: list[dict[str, object]],
        *,
        on_device_error: Callable[[TrackLabel, Exception], None] | None = None,
        pyaudio_instance: object | None = None,
        block_ms: int = AUDIO_BLOCK_MS,
        lock: threading.RLock | None = None,
    ) -> None:
        super().__init__(
            track=TrackLabel.IO,
            device=device,
            block_queue=block_queue,
            stop_event=stop_event,
            discontinuities=discontinuities,
            on_device_error=on_device_error,
            pyaudio_instance=pyaudio_instance,
            block_ms=block_ms,
            lock=lock,
        )


class LoopbackCaptureThread(_BaseCaptureThread):
    """Thread di acquisizione per il loopback WASAPI di sistema (traccia ALTRI)."""

    def __init__(
        self,
        device: DeviceDescriptor,
        block_queue: queue.Queue[CapturedBlock],
        stop_event: threading.Event,
        discontinuities: list[dict[str, object]],
        *,
        on_device_error: Callable[[TrackLabel, Exception], None] | None = None,
        pyaudio_instance: object | None = None,
        block_ms: int = AUDIO_BLOCK_MS,
        lock: threading.RLock | None = None,
    ) -> None:
        super().__init__(
            track=TrackLabel.OTHERS,
            device=device,
            block_queue=block_queue,
            stop_event=stop_event,
            discontinuities=discontinuities,
            on_device_error=on_device_error,
            pyaudio_instance=pyaudio_instance,
            block_ms=block_ms,
            lock=lock,
        )


class _TrackBufferState:
    """Stato interno di scrittura WAV e ring buffer di overlap per singola traccia."""

    def __init__(
        self,
        track: TrackLabel,
        device: DeviceDescriptor,
        overlap_sec: int,
    ) -> None:
        self.track = track
        self.device = device
        self.overlap_sec = max(0, overlap_sec)
        self.wav_handle: wave.Wave_write | None = None
        self.wav_path: Path | None = None
        self.written_frames: int = 0
        self.overlap_frames_written: int = 0
        self.last_frame_monotonic: float | None = None
        self._ring_chunks: collections.deque[tuple[int, bytes]] = collections.deque()
        self._ring_frames: int = 0

    @property
    def max_overlap_frames(self) -> int:
        return self.overlap_sec * self.device.sample_rate

    @property
    def timeline_frames(self) -> int:
        """Frame scritti dall'inizio del chunk al netto dell'overlap iniziale."""
        return max(0, self.written_frames - self.overlap_frames_written)

    def open_wav(self, path: Path, write_overlap: bool) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        handle = wave.open(str(path), "wb")
        handle.setnchannels(self.device.channels)
        handle.setsampwidth(self.device.sample_width)
        handle.setframerate(self.device.sample_rate)
        self.wav_handle = handle
        self.wav_path = path
        self.written_frames = 0
        self.overlap_frames_written = 0

        if write_overlap and self._ring_frames > 0:
            overlap_payload = b"".join(chunk for _, chunk in self._ring_chunks)
            frame_bytes = max(1, self.device.channels * self.device.sample_width)
            overlap_frames = len(overlap_payload) // frame_bytes
            if overlap_frames > 0:
                handle.writeframes(overlap_payload)
                self.written_frames += overlap_frames
                self.overlap_frames_written = overlap_frames

    def append_frames(self, payload: bytes, frame_count: int) -> None:
        if self.wav_handle is None or frame_count <= 0 or not payload:
            return
        self.wav_handle.writeframes(payload)
        self.written_frames += frame_count
        self.last_frame_monotonic = time.monotonic()
        self._update_ring_buffer(payload, frame_count)

    def _update_ring_buffer(self, payload: bytes, frame_count: int) -> None:
        max_frames = self.max_overlap_frames
        if max_frames <= 0:
            self._ring_chunks.clear()
            self._ring_frames = 0
            return

        self._ring_chunks.append((frame_count, payload))
        self._ring_frames += frame_count
        frame_bytes = max(1, self.device.channels * self.device.sample_width)

        while self._ring_frames > max_frames and self._ring_chunks:
            excess_frames = self._ring_frames - max_frames
            oldest_frames, oldest_bytes = self._ring_chunks[0]
            if oldest_frames <= excess_frames:
                self._ring_chunks.popleft()
                self._ring_frames -= oldest_frames
            else:
                trim_bytes = excess_frames * frame_bytes
                trimmed_payload = oldest_bytes[trim_bytes:]
                remaining_frames = oldest_frames - excess_frames
                self._ring_chunks[0] = (remaining_frames, trimmed_payload)
                self._ring_frames -= excess_frames

    def close_wav(self) -> None:
        handle = self.wav_handle
        self.wav_handle = None
        if handle is not None:
            try:
                handle.close()
            except Exception as exc:
                logger.warning(
                    "Errore chiudendo WAV per traccia %s (%s): %s",
                    self.track.value,
                    self.wav_path,
                    exc,
                )

    def clear_ring_buffer(self) -> None:
        self._ring_chunks.clear()
        self._ring_frames = 0


class ChunkAssembler:
    """Assembla i blocchi delle due tracce in WAV allineati, gestendo silenzio, drift e rotazione (Sezione 20)."""

    def __init__(
        self,
        session_id: str,
        audio_dir: Path,
        io_device: DeviceDescriptor,
        others_device: DeviceDescriptor,
        *,
        chunk_duration_sec: int = CHUNK_DURATION_SEC,
        chunk_overlap_sec: int = CHUNK_OVERLAP_SEC,
        gap_tolerance_ms: int = LOOPBACK_GAP_TOLERANCE_MS,
        max_silence_injection_ms: int = MAX_SILENCE_INJECTION_MS,
        max_track_drift_ms: int = MAX_TRACK_DRIFT_MS,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self.session_id = session_id
        self.audio_dir = audio_dir
        self._chunk_duration_sec = max(1, int(chunk_duration_sec))
        self._chunk_overlap_sec = max(0, int(chunk_overlap_sec))
        self._gap_tolerance_ms = max(0, int(gap_tolerance_ms))
        self._max_silence_injection_ms = max(1, int(max_silence_injection_ms))
        self._max_track_drift_ms = max(1, int(max_track_drift_ms))
        self._clock: Callable[[], float] = clock if clock is not None else time.monotonic

        self._lock = threading.RLock()
        self._io_state = _TrackBufferState(
            TrackLabel.IO, io_device, self._chunk_overlap_sec
        )
        self._others_state = _TrackBufferState(
            TrackLabel.OTHERS, others_device, self._chunk_overlap_sec
        )

        self._current_chunk_index: int = 0
        self._chunk_started_monotonic: float = self._clock()
        self._session_started_monotonic: float = self._chunk_started_monotonic
        self._current_start_offset_sec: float = 0.0
        self._timeline_Cumulative_sec: float = 0.0
        self._chunk_discontinuities: list[dict[str, object]] = []
        self._is_open: bool = False
        self._is_paused: bool = False
        self._pause_started_mono: float | None = None
        self._total_paused_sec: float = 0.0

    @property
    def is_paused(self) -> bool:
        with self._lock:
            return self._is_paused

    @property
    def total_paused_sec(self) -> float:
        with self._lock:
            return self._total_paused_sec

    def pause(self, now_mono: float | None = None) -> bool:
        """Attiva la pausa della sessione dopo aver allineato le tracce."""
        with self._lock:
            if self._is_paused:
                return False
            ref_mono = time.monotonic() if now_mono is None else float(now_mono)
            elapsed = max(0.0, ref_mono - self._chunk_started_monotonic)
            self.compensate_idle_tracks(elapsed)
            self._is_paused = True
            self._pause_started_mono = ref_mono
            return True

    def resume(self, now_mono: float | None = None) -> float:
        """Riprende la sessione e compensa il clock dei chunk con la durata della pausa."""
        with self._lock:
            if not self._is_paused:
                return 0.0
            current = time.monotonic() if now_mono is None else float(now_mono)
            pause_start = self._pause_started_mono if self._pause_started_mono is not None else current
            paused_delta = max(0.0, current - pause_start)
            self._total_paused_sec += paused_delta
            self._chunk_started_monotonic += paused_delta
            self._session_started_monotonic += paused_delta
            for state in (self._io_state, self._others_state):
                if state.last_frame_monotonic is not None:
                    state.last_frame_monotonic += paused_delta
            self._is_paused = False
            self._pause_started_mono = None
            return paused_delta

    @property
    def current_chunk_index(self) -> int:
        with self._lock:
            return self._current_chunk_index

    @property
    def is_open(self) -> bool:
        with self._lock:
            return self._is_open

    def open_initial_chunk(self, now_monotonic: float | None = None) -> None:
        """Apre la prima coppia di file WAV grezzi (chunk 000)."""
        with self._lock:
            ref_mono = float(now_monotonic) if now_monotonic is not None else float(self._clock())
            self._current_chunk_index = 0
            self._chunk_started_monotonic = ref_mono
            self._current_start_offset_sec = 0.0
            self._timeline_Cumulative_sec = 0.0
            self._chunk_discontinuities = []
            self._open_chunk_files(chunk_index=0, write_overlap=False)
            self._is_open = True

    def _open_chunk_files(self, chunk_index: int, write_overlap: bool) -> None:
        self.audio_dir.mkdir(parents=True, exist_ok=True)
        io_path = self.audio_dir / f"chunk_{chunk_index:03d}_io_raw.wav"
        others_path = self.audio_dir / f"chunk_{chunk_index:03d}_altri_raw.wav"
        self._io_state.open_wav(io_path, write_overlap=write_overlap)
        self._others_state.open_wav(others_path, write_overlap=write_overlap)

    def _get_track_state(self, track: TrackLabel) -> _TrackBufferState:
        return self._io_state if track == TrackLabel.IO else self._others_state

    def record_discontinuity(self, entry: dict[str, object]) -> None:
        with self._lock:
            self._chunk_discontinuities.append(dict(entry))

    def _inject_silence_into_track(
        self,
        state: _TrackBufferState,
        frames_to_inject: int,
        reason: str,
    ) -> int:
        if frames_to_inject <= 0:
            return 0
        silence_bytes = generate_silence_bytes(
            frames_to_inject,
            channels=state.device.channels,
            sample_width=state.device.sample_width,
        )
        state.append_frames(silence_bytes, frames_to_inject)
        self._chunk_discontinuities.append(
            {
                "track": state.track.value,
                "reason": reason,
                "injected_frames": frames_to_inject,
                "sample_rate": state.device.sample_rate,
                "occurred_at": now_tz().isoformat(),
            }
        )
        return frames_to_inject

    def write_block(
        self,
        block: CapturedBlock,
        elapsed_chunk_sec: float | None = None,
    ) -> int:
        """Scrive un CapturedBlock nella traccia corrispondente, iniettando silenzio se necessario."""
        with self._lock:
            if not self._is_open or self._is_paused:
                return 0
            state = self._get_track_state(block.track)
            elapsed = (
                float(elapsed_chunk_sec)
                if elapsed_chunk_sec is not None
                else max(0.0, float(self._clock()) - self._chunk_started_monotonic)
            )
            missing = calculate_missing_frames(
                elapsed_seconds=elapsed,
                sample_rate=state.device.sample_rate,
                written_frames=state.timeline_frames,
            )
            injected = 0
            if missing > 0:
                silence_frames = compute_silence_injection_frames(
                    missing_frames=missing,
                    sample_rate=state.device.sample_rate,
                    gap_tolerance_ms=self._gap_tolerance_ms,
                    max_injection_ms=self._max_silence_injection_ms,
                )
                if silence_frames > 0:
                    injected = self._inject_silence_into_track(
                        state,
                        silence_frames,
                        reason="gap_compensation",
                    )

            state.append_frames(block.payload, block.frame_count)
            return injected

    def append_block(
        self,
        block: CapturedBlock,
        elapsed_chunk_sec: float | None = None,
    ) -> int:
        return self.write_block(block, elapsed_chunk_sec)

    def compensate_idle_tracks(
        self,
        elapsed_chunk_sec: float | None = None,
    ) -> dict[TrackLabel, int]:
        """Compensa i periodi di silenzio digitale su entrambe le tracce e corregge il drift (Sezione 20.5)."""
        with self._lock:
            if not self._is_open or self._is_paused:
                return {TrackLabel.IO: 0, TrackLabel.OTHERS: 0}

            elapsed = (
                float(elapsed_chunk_sec)
                if elapsed_chunk_sec is not None
                else max(0.0, float(self._clock()) - self._chunk_started_monotonic)
            )
            injected_by_track: dict[TrackLabel, int] = {
                TrackLabel.IO: 0,
                TrackLabel.OTHERS: 0,
            }

            for state in (self._io_state, self._others_state):
                missing = calculate_missing_frames(
                    elapsed_seconds=elapsed,
                    sample_rate=state.device.sample_rate,
                    written_frames=state.timeline_frames,
                )
                silence_frames = compute_silence_injection_frames(
                    missing_frames=missing,
                    sample_rate=state.device.sample_rate,
                    gap_tolerance_ms=self._gap_tolerance_ms,
                    max_injection_ms=self._max_silence_injection_ms,
                )
                if silence_frames > 0:
                    injected_by_track[state.track] += self._inject_silence_into_track(
                        state,
                        silence_frames,
                        reason="idle_silence_compensation",
                    )

            drift_ms = compute_track_drift_ms(
                io_frames=self._io_state.timeline_frames,
                io_rate=self._io_state.device.sample_rate,
                others_frames=self._others_state.timeline_frames,
                others_rate=self._others_state.device.sample_rate,
            )
            if drift_ms > float(self._max_track_drift_ms):
                io_sec = self._io_state.timeline_frames / float(
                    self._io_state.device.sample_rate
                )
                others_sec = self._others_state.timeline_frames / float(
                    self._others_state.device.sample_rate
                )
                if io_sec < others_sec:
                    lagging = self._io_state
                    diff_sec = others_sec - io_sec
                else:
                    lagging = self._others_state
                    diff_sec = io_sec - others_sec

                catchup_frames = compute_silence_injection_frames(
                    missing_frames=round(diff_sec * lagging.device.sample_rate),
                    sample_rate=lagging.device.sample_rate,
                    gap_tolerance_ms=0,
                    max_injection_ms=self._max_silence_injection_ms,
                )
                if catchup_frames > 0:
                    injected_by_track[lagging.track] += self._inject_silence_into_track(
                        lagging,
                        catchup_frames,
                        reason="drift_alignment",
                    )

            return injected_by_track

    def _align_tracks_before_close(self, target_elapsed_sec: float) -> None:
        """Allinea la durata finale delle due tracce e garantisce almeno 1 frame per WAV valido."""
        for state in (self._io_state, self._others_state):
            missing = calculate_missing_frames(
                elapsed_seconds=target_elapsed_sec,
                sample_rate=state.device.sample_rate,
                written_frames=state.timeline_frames,
            )
            if missing > 0:
                max_pad = max(
                    1,
                    round(
                        (self._max_silence_injection_ms / 1000.0)
                        * state.device.sample_rate
                    ),
                )
                pad_frames = min(missing, max_pad)
                self._inject_silence_into_track(
                    state,
                    pad_frames,
                    reason="boundary_padding",
                )
            if state.written_frames == 0:
                min_frames = max(1, state.device.sample_rate // 50)
                self._inject_silence_into_track(
                    state,
                    min_frames,
                    reason="empty_chunk_guard",
                )

        io_sec = self._io_state.written_frames / float(self._io_state.device.sample_rate)
        others_sec = self._others_state.written_frames / float(
            self._others_state.device.sample_rate
        )
        diff_sec = abs(io_sec - others_sec)
        if diff_sec * 1000.0 > float(self._max_track_drift_ms):
            lagging = self._io_state if io_sec < others_sec else self._others_state
            pad_frames = round(diff_sec * lagging.device.sample_rate)
            max_pad = max(
                1,
                round(
                    (self._max_silence_injection_ms / 1000.0)
                    * lagging.device.sample_rate
                ),
            )
            self._inject_silence_into_track(
                lagging,
                min(pad_frames, max_pad),
                reason="close_drift_alignment",
            )

    def should_rotate(self, now_monotonic: float | None = None) -> bool:
        """Restituisce True se il chunk corrente ha raggiunto o superato chunk_duration_sec."""
        with self._lock:
            if not self._is_open or self._is_paused:
                return False
            ref_mono = float(now_monotonic) if now_monotonic is not None else float(self._clock())
            return (ref_mono - self._chunk_started_monotonic) >= float(
                self._chunk_duration_sec
            )

    def rotate_chunk(
        self,
        *,
        is_final: bool = False,
        now_monotonic: float | None = None,
    ) -> AudioChunk:
        """Chiude il chunk corrente e, se non è finale, apre il successivo con overlap (Sezione 20.6)."""
        with self._lock:
            if not self._is_open:
                raise AudioProcessingError("Nessun chunk attivo da ruotare o chiudere.")

            ref_mono = float(now_monotonic) if now_monotonic is not None else float(self._clock())
            elapsed_chunk = max(0.02, ref_mono - self._chunk_started_monotonic)
            self._align_tracks_before_close(elapsed_chunk)

            io_frames = self._io_state.written_frames
            others_frames = self._others_state.written_frames
            io_dur = io_frames / float(self._io_state.device.sample_rate)
            others_dur = others_frames / float(self._others_state.device.sample_rate)
            actual_chunk_duration = max(elapsed_chunk, io_dur, others_dur)

            io_path = self._io_state.wav_path
            others_path = self._others_state.wav_path
            assert io_path is not None and others_path is not None

            self._io_state.close_wav()
            self._others_state.close_wav()

            start_offset = round(self._current_start_offset_sec, 3)
            end_offset = round(start_offset + actual_chunk_duration, 3)
            closed_index = self._current_chunk_index
            discontinuities_copy = list(self._chunk_discontinuities)

            closed_chunk = AudioChunk(
                index=closed_index,
                session_id=self.session_id,
                start_offset_sec=start_offset,
                end_offset_sec=end_offset,
                io_wav_path=io_path,
                others_wav_path=others_path,
                is_final=is_final,
                created_at=now_tz(),
                status=ChunkStatus.RAW_CLOSED,
                discontinuities=discontinuities_copy,
            )

            self._timeline_Cumulative_sec = end_offset
            self._chunk_discontinuities = []

            if is_final:
                self._is_open = False
                self._io_state.clear_ring_buffer()
                self._others_state.clear_ring_buffer()
            else:
                self._current_chunk_index += 1
                self._chunk_started_monotonic = ref_mono
                overlap_applied = min(
                    float(self._chunk_overlap_sec),
                    actual_chunk_duration,
                )
                self._current_start_offset_sec = max(
                    0.0,
                    end_offset - overlap_applied,
                )
                self._open_chunk_files(
                    chunk_index=self._current_chunk_index,
                    write_overlap=True,
                )

            return closed_chunk

    def close_for_reconnect(
        self,
        now_monotonic: float | None = None,
    ) -> AudioChunk | None:
        """Chiude in sicurezza il chunk corrente prima di un hot-swap dei dispositivi (Sezione 20.8)."""
        with self._lock:
            if not self._is_open:
                return None

            ref_mono = float(now_monotonic) if now_monotonic is not None else float(self._clock())
            elapsed_chunk = max(0.02, ref_mono - self._chunk_started_monotonic)
            self._align_tracks_before_close(elapsed_chunk)

            io_dur = self._io_state.written_frames / float(
                self._io_state.device.sample_rate
            )
            others_dur = self._others_state.written_frames / float(
                self._others_state.device.sample_rate
            )
            actual_chunk_duration = max(elapsed_chunk, io_dur, others_dur)

            io_path = self._io_state.wav_path
            others_path = self._others_state.wav_path
            assert io_path is not None and others_path is not None

            self._io_state.close_wav()
            self._others_state.close_wav()
            self._io_state.clear_ring_buffer()
            self._others_state.clear_ring_buffer()

            start_offset = round(self._current_start_offset_sec, 3)
            end_offset = round(start_offset + actual_chunk_duration, 3)
            closed_chunk = AudioChunk(
                index=self._current_chunk_index,
                session_id=self.session_id,
                start_offset_sec=start_offset,
                end_offset_sec=end_offset,
                io_wav_path=io_path,
                others_wav_path=others_path,
                is_final=False,
                created_at=now_tz(),
                status=ChunkStatus.RAW_CLOSED,
                discontinuities=list(self._chunk_discontinuities),
            )

            self._timeline_Cumulative_sec = end_offset
            self._current_chunk_index += 1
            self._chunk_discontinuities = []
            self._is_open = False
            return closed_chunk

    def reopen_after_reconnect(
        self,
        io_device: DeviceDescriptor,
        others_device: DeviceDescriptor,
        now_monotonic: float | None = None,
    ) -> None:
        """Riapre un nuovo chunk con i nuovi descrittori dispositivo dopo hot-swap (Sezione 20.8)."""
        with self._lock:
            ref_mono = float(now_monotonic) if now_monotonic is not None else float(self._clock())
            self._io_state = _TrackBufferState(
                TrackLabel.IO, io_device, self._chunk_overlap_sec
            )
            self._others_state = _TrackBufferState(
                TrackLabel.OTHERS, others_device, self._chunk_overlap_sec
            )
            self._chunk_started_monotonic = ref_mono
            self._current_start_offset_sec = self._timeline_Cumulative_sec
            self._open_chunk_files(
                chunk_index=self._current_chunk_index,
                write_overlap=False,
            )
            self._is_open = True


class AudioFinalizationWorker:
    """Worker asincrono per la normalizzazione FFmpeg dei chunk chiusi (Sezioni 20.1, 20.6, 20.7)."""

    def __init__(
        self,
        ffmpeg_path: Path,
        on_chunk_ready: Callable[[AudioChunk], None],
        *,
        state_manager: StateManager | None = None,
        normalizer: Callable[..., Path] | None = None,
    ) -> None:
        self._ffmpeg_path = ffmpeg_path
        self._on_chunk_ready = on_chunk_ready
        self._state_manager = state_manager
        self._normalizer = (
            normalizer if normalizer is not None else normalize_wav_with_ffmpeg
        )

        self._queue: queue.Queue[AudioChunk | None] = queue.Queue()
        self._thread: threading.Thread | None = None
        self._lock = threading.RLock()
        self._processed_chunks: list[AudioChunk] = []
        self.log_session_id: str | None = None

    @property
    def processed_chunks(self) -> list[AudioChunk]:
        with self._lock:
            return list(self._processed_chunks)

    def start(self) -> None:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._thread = threading.Thread(
                target=self._run_loop,
                name="AudioFinalizationWorker",
                daemon=True,
            )
            self._thread.start()

    def submit(self, chunk: AudioChunk) -> None:
        self._queue.put(chunk)

    def finish(self) -> None:
        """Inserisce la sentinella di terminazione nella coda del worker."""
        self._queue.put(None)

    def join(self, timeout: float | None = None) -> None:
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=timeout)

    def is_alive(self) -> bool:
        thread = self._thread
        return thread is not None and thread.is_alive()

    def process_chunk(self, chunk: AudioChunk) -> AudioChunk:
        """Normalizza un singolo AudioChunk con FFmpeg, aggiorna lo stato e invoca on_chunk_ready."""
        chunk.status = ChunkStatus.NORMALIZING
        io_norm_path = chunk.io_wav_path.with_name(f"chunk_{chunk.index:03d}_io_16k.wav")
        others_norm_path = chunk.others_wav_path.with_name(
            f"chunk_{chunk.index:03d}_altri_16k.wav"
        )

        try:
            self._normalizer(
                self._ffmpeg_path,
                chunk.io_wav_path,
                io_norm_path,
                delete_raw_on_success=True,
            )
            self._normalizer(
                self._ffmpeg_path,
                chunk.others_wav_path,
                others_norm_path,
                delete_raw_on_success=True,
            )
            chunk.io_wav_path = io_norm_path
            chunk.others_wav_path = others_norm_path
            chunk.status = ChunkStatus.NORMALIZED

            self._update_state_normalized(chunk)
            with self._lock:
                self._processed_chunks.append(chunk)
            self._on_chunk_ready(chunk)
            return chunk
        except Exception as exc:
            chunk.status = ChunkStatus.FAILED
            logger.error(
                "Normalizzazione FFmpeg fallita per chunk %03d: %s",
                chunk.index,
                exc,
            )
            self._record_state_error(exc)
            with self._lock:
                self._processed_chunks.append(chunk)
            return chunk

    def _update_state_normalized(self, chunk: AudioChunk) -> None:
        if self._state_manager is None:
            return
        try:
            if hasattr(self._state_manager, "mutate"):
                def _mutate(ctx: object) -> None:
                    norm_list = getattr(ctx, "normalized_chunks", None)
                    if isinstance(norm_list, list) and chunk.index not in norm_list:
                        norm_list.append(chunk.index)
                        norm_list.sort()
                    rec_list = getattr(ctx, "recorded_chunks", None)
                    if isinstance(rec_list, list) and chunk.index not in rec_list:
                        rec_list.append(chunk.index)
                        rec_list.sort()

                getattr(self._state_manager, "mutate")(_mutate)
        except Exception as exc:
            logger.warning("Impossibile aggiornare state_manager per chunk %d: %s", chunk.index, exc)

    def _record_state_error(self, exc: Exception) -> None:
        if self._state_manager is None:
            return
        try:
            if hasattr(self._state_manager, "record_error"):
                getattr(self._state_manager, "record_error")(
                    Stage.FINALIZING_AUDIO,
                    exc,
                    affects_stage=False,
                )
        except Exception as state_exc:
            logger.debug("Impossibile registrare errore su state_manager: %s", state_exc)

    def _run_loop(self) -> None:
        if self.log_session_id is not None:
            set_thread_log_session_id(self.log_session_id)
        while True:
            item = self._queue.get()
            try:
                if item is None:
                    return
                self.process_chunk(item)
            finally:
                self._queue.task_done()


def _resolve_default_ffmpeg_path() -> Path:
    if FFMPEG_LOCAL_PATH.exists():
        return FFMPEG_LOCAL_PATH
    system_ffmpeg = shutil.which("ffmpeg")
    if system_ffmpeg:
        return Path(system_ffmpeg)
    return FFMPEG_LOCAL_PATH


def _default_pyaudio_factory() -> object:
    if pyaudiowpatch is None or not hasattr(pyaudiowpatch, "PyAudio"):
        raise AudioDeviceError("PyAudioWPatch non è disponibile.")
    return pyaudiowpatch.PyAudio()


class DualTrackRecorder:
    """ Coordinatore della registrazione dual-track (IO + ALTRI) con hot-swap e chunking (Sezione 20)."""

    def __init__(
        self,
        session_id: str,
        session_dir: Path,
        on_chunk_ready: Callable[[AudioChunk], None],
        stage_reporter: StageReporter | None = None,
        state_manager: StateManager | None = None,
        *,
        ffmpeg_path: Path | None = None,
        chunk_duration_sec: int = CHUNK_DURATION_SEC,
        chunk_overlap_sec: int = CHUNK_OVERLAP_SEC,
        max_session_duration_sec: int = MAX_SESSION_DURATION_SEC,
        on_max_duration_reached: Callable[[], None] | None = None,
        pyaudio_factory: Callable[[], object] | None = None,
        device_discoverer: Callable[
            [object | None], tuple[DeviceDescriptor, DeviceDescriptor]
        ]
        | None = None,
        clock: Callable[[], float] | None = None,
        reconnect_delay_sec: float = float(DEVICE_RECONNECT_DELAY_SEC),
        reconnect_max_attempts: int = DEVICE_RECONNECT_MAX_ATTEMPTS,
    ) -> None:
        self.session_id = session_id
        self.session_dir = session_dir
        self.audio_dir = session_dir / "audio"
        self._on_chunk_ready = on_chunk_ready
        self._stage_reporter = stage_reporter
        self._state_manager = state_manager
        self._ffmpeg_path = (
            ffmpeg_path if ffmpeg_path is not None else _resolve_default_ffmpeg_path()
        )
        self._chunk_duration_sec = max(1, int(chunk_duration_sec))
        self._chunk_overlap_sec = max(0, int(chunk_overlap_sec))
        self._max_session_duration_sec = max(1, int(max_session_duration_sec))
        self._on_max_duration_reached = on_max_duration_reached
        self._pyaudio_factory: Callable[[], object] = (
            pyaudio_factory
            if pyaudio_factory is not None
            else _default_pyaudio_factory
        )
        self._device_discoverer = (
            device_discoverer
            if device_discoverer is not None
            else discover_audio_devices
        )
        self._clock: Callable[[], float] = clock if clock is not None else time.monotonic
        self._reconnect_delay_sec = max(0.0, float(reconnect_delay_sec))
        self._reconnect_max_attempts = max(1, int(reconnect_max_attempts))

        self._lock = threading.RLock()
        self._pause_lock = threading.Lock()
        self._stop_event = threading.Event()
        self._finalize_event = threading.Event()
        self._is_recording: bool = False
        self._is_paused: bool = False
        self._sleep_prevented: bool = False
        self._session_started_monotonic: float = 0.0

        self._block_queue: queue.Queue[CapturedBlock] = queue.Queue(
            maxsize=compute_audio_queue_size()
        )
        self._discontinuities: list[dict[str, object]] = []

        self._pa_instance: object | None = None
        self._mic_thread: MicCaptureThread | None = None
        self._loopback_thread: LoopbackCaptureThread | None = None
        self._assembler_thread: threading.Thread | None = None
        self._assembler: ChunkAssembler | None = None
        self._finalization_worker = AudioFinalizationWorker(
            ffmpeg_path=self._ffmpeg_path,
            on_chunk_ready=self._on_chunk_ready,
            state_manager=self._state_manager,
        )
        self._finalization_worker.log_session_id = session_id
        self._reconnecting = threading.Lock()
        self._pa_status_event_count: int = 0

    @property
    def is_recording(self) -> bool:
        """Indica se la registrazione dual-track è attualmente attiva."""
        with self._lock:
            return self._is_recording

    @property
    def finalize_event(self) -> threading.Event:
        """Evento impostato al raggiungimento della durata massima o richiesta di chiusura."""
        return self._finalize_event

    @property
    def is_paused(self) -> bool:
        with self._pause_lock:
            return self._is_paused

    def pause(self) -> bool:
        """Mette in pausa la cattura audio e l'assemblaggio dei chunk."""
        with self._pause_lock:
            if not self._is_recording or self._is_paused:
                return False
            self._is_paused = True
            if self._mic_thread is not None:
                self._mic_thread.pause()
            if self._loopback_thread is not None:
                self._loopback_thread.pause()
            if self._assembler is not None:
                self._assembler.pause()
            logger.info("Registrazione sessione %s messa in pausa.", self.session_id)
            return True

    def resume(self) -> bool:
        """Riprende la cattura audio e compensa i clock dei chunk."""
        with self._pause_lock:
            if not self._is_recording or not self._is_paused:
                return False
            if self._assembler is not None:
                self._assembler.resume()
            if self._mic_thread is not None:
                self._mic_thread.resume()
            if self._loopback_thread is not None:
                self._loopback_thread.resume()
            self._is_paused = False
            logger.info("Registrazione sessione %s ripresa.", self.session_id)
            return True

    def start(self) -> None:
        """Avvia l'acquisizione audio dual-track, il ChunkAssembler e il worker FFmpeg."""
        with self._lock:
            if self._is_recording:
                return

            self._stop_event.clear()
            self._finalize_event.clear()
            self.audio_dir.mkdir(parents=True, exist_ok=True)

            prevent_system_sleep()
            self._sleep_prevented = True

            try:
                self._pa_instance = self._pyaudio_factory()
                io_device, others_device = self._device_discoverer(self._pa_instance)

                now_mono = float(self._clock())
                self._session_started_monotonic = now_mono

                self._assembler = ChunkAssembler(
                    session_id=self.session_id,
                    audio_dir=self.audio_dir,
                    io_device=io_device,
                    others_device=others_device,
                    chunk_duration_sec=self._chunk_duration_sec,
                    chunk_overlap_sec=self._chunk_overlap_sec,
                    clock=self._clock,
                )
                self._assembler.open_initial_chunk(now_monotonic=now_mono)

                self._finalization_worker.start()
                self._start_capture_threads(io_device, others_device)

                self._assembler_thread = threading.Thread(
                    target=self._run_assembler_loop,
                    name="ChunkAssemblerLoop",
                    daemon=True,
                )
                self._assembler_thread.start()
                self._is_recording = True
            except Exception:
                self._terminate_pyaudio()
                if self._sleep_prevented:
                    restore_system_sleep()
                    self._sleep_prevented = False
                raise

    def _start_capture_threads(
        self,
        io_device: DeviceDescriptor,
        others_device: DeviceDescriptor,
    ) -> None:
        self._mic_thread = MicCaptureThread(
            device=io_device,
            block_queue=self._block_queue,
            stop_event=self._stop_event,
            discontinuities=self._discontinuities,
            on_device_error=self._on_capture_thread_error,
            pyaudio_instance=self._pa_instance,
            lock=self._lock,
        )
        self._loopback_thread = LoopbackCaptureThread(
            device=others_device,
            block_queue=self._block_queue,
            stop_event=self._stop_event,
            discontinuities=self._discontinuities,
            on_device_error=self._on_capture_thread_error,
            pyaudio_instance=self._pa_instance,
            lock=self._lock,
        )
        self._mic_thread.log_session_id = self.session_id
        self._loopback_thread.log_session_id = self.session_id
        self._mic_thread.start()
        self._loopback_thread.start()

    def _stop_capture_threads(self) -> None:
        mic = self._mic_thread
        loop = self._loopback_thread
        self._mic_thread = None
        self._loopback_thread = None

        if mic is not None:
            mic.suspend()
        if loop is not None:
            loop.suspend()

        current_thread = threading.current_thread()
        if mic is not None and mic.is_alive() and mic is not current_thread:
            mic.join(timeout=2.0)
        if loop is not None and loop.is_alive() and loop is not current_thread:
            loop.join(timeout=2.0)

    def _terminate_pyaudio(self) -> None:
        pa = self._pa_instance
        self._pa_instance = None
        if pa is not None and hasattr(pa, "terminate"):
            try:
                getattr(pa, "terminate")()
            except Exception as exc:
                logger.debug("Errore durante terminate() di PyAudio: %s", exc)

    def _drain_block_queue(self) -> None:
        if self._assembler is None:
            return
        while True:
            try:
                block = self._block_queue.get_nowait()
            except queue.Empty:
                break
            try:
                self._assembler.write_block(block)
            finally:
                self._block_queue.task_done()

        with self._lock:
            if self._discontinuities:
                for disc in self._discontinuities:
                    self._assembler.record_discontinuity(disc)
                self._discontinuities.clear()

    def _record_chunk_in_state(self, chunk: AudioChunk) -> None:
        if self._state_manager is None:
            return
        try:
            if hasattr(self._state_manager, "mutate"):
                def _mutate(ctx: object) -> None:
                    rec_list = getattr(ctx, "recorded_chunks", None)
                    if isinstance(rec_list, list) and chunk.index not in rec_list:
                        rec_list.append(chunk.index)
                        rec_list.sort()
                    disc_list = getattr(ctx, "discontinuities", None)
                    if isinstance(disc_list, list) and chunk.discontinuities:
                        disc_list.extend(chunk.discontinuities)

                getattr(self._state_manager, "mutate")(_mutate)
        except Exception as exc:
            logger.warning("Impossibile salvare chunk %d nello state: %s", chunk.index, exc)

    def _on_capture_thread_error(self, track: TrackLabel, error: Exception) -> None:
        if self._stop_event.is_set():
            return
        threading.Thread(
            target=self.handle_device_error,
            args=(track, error),
            name=f"HotSwap-{track.value}",
            daemon=True,
        ).start()

    def handle_device_error(self, track: TrackLabel, error: Exception) -> bool:
        """Gestisce il cambio o la disconnessione a caldo di un dispositivo audio (Sezione 20.8).

        Sequenza rigorosa:
        1. Sospende gli stream di cattura;
        2. Chiude i file WAV aperti;
        3. Finalizza il chunk parziale corrente inviandolo a FFmpeg;
        4. Salva lo stato su StateManager;
        5. Rilascia l'istanza PyAudio;
        6. Attende e ritenta fino a DEVICE_RECONNECT_MAX_ATTEMPTS;
        7. Enumera nuovamente i dispositivi;
        8. Apre un nuovo chunk;
        9. Notifica la Tray.
        """
        if not self._reconnecting.acquire(blocking=False):
            return False

        try:
            logger.warning(
                "Avvio procedura hot-swap per errore su traccia %s: %s",
                track.value,
                error,
            )
            # 1. Sospendere stream
            self._stop_capture_threads()
            self._drain_block_queue()

            # 2 & 3. Chiudere WAV e finalizzare chunk
            closed_chunk: AudioChunk | None = None
            if self._assembler is not None and self._assembler.is_open:
                self._assembler.record_discontinuity(
                    {
                        "track": track.value,
                        "reason": "device_disconnected",
                        "error": str(error),
                        "occurred_at": now_tz().isoformat(),
                    }
                )
                closed_chunk = self._assembler.close_for_reconnect()

            if closed_chunk is not None:
                # 4. Salvare state
                self._record_chunk_in_state(closed_chunk)
                self._finalization_worker.submit(closed_chunk)

            # 5. Rilasciare PyAudio
            self._terminate_pyaudio()

            if self._stage_reporter is not None:
                self._stage_reporter.set_stage(
                    Stage.RECORDING,
                    f"Riconnessione dispositivo audio ({track.value}) in corso...",
                )

            # 6 & 7. Attendere ed enumerare dispositivi
            for attempt in range(1, self._reconnect_max_attempts + 1):
                if self._stop_event.is_set():
                    return False
                if self._reconnect_delay_sec > 0.0:
                    if self._stop_event.wait(timeout=self._reconnect_delay_sec):
                        return False

                try:
                    self._pa_instance = self._pyaudio_factory()
                    io_dev, others_dev = self._device_discoverer(self._pa_instance)

                    # 8. Aprire nuovo chunk e riavviare stream
                    if self._assembler is not None:
                        self._assembler.reopen_after_reconnect(io_dev, others_dev)
                    self._start_capture_threads(io_dev, others_dev)

                    # 9. Notificare Tray
                    if self._stage_reporter is not None:
                        self._stage_reporter.set_stage(
                            Stage.RECORDING,
                            f"Dispositivi audio riconnessi ({io_dev.name} / {others_dev.name})",
                        )
                    logger.info(
                        "Hot-swap completato al tentativo %d/%d.",
                        attempt,
                        self._reconnect_max_attempts,
                    )
                    return True
                except Exception as retry_exc:
                    logger.warning(
                        "Tentativo hot-swap %d/%d fallito: %s",
                        attempt,
                        self._reconnect_max_attempts,
                        retry_exc,
                    )
                    self._terminate_pyaudio()

            fatal_err = AudioDeviceError(
                f"Riconnessione dispositivo fallita dopo {self._reconnect_max_attempts} tentativi: {error}"
            )
            if self._state_manager is not None and hasattr(
                self._state_manager, "record_error"
            ):
                getattr(self._state_manager, "record_error")(
                    Stage.ERROR,
                    fatal_err,
                )
            if self._stage_reporter is not None:
                self._stage_reporter.set_stage(
                    Stage.ERROR,
                    "Dispositivo audio perso definitivamente",
                )
            self._finalize_event.set()
            return False
        finally:
            self._reconnecting.release()

    @property
    def pa_status_event_count(self) -> int:
        """Numero di callback PortAudio con status_flags non nulli (overflow/underflow)."""
        return self._pa_status_event_count

    def _collect_status_flag_events(self) -> None:
        """Converte gli status_flags PortAudio accumulati in discontinuità del chunk corrente."""
        assembler = self._assembler
        for capture in (self._mic_thread, self._loopback_thread):
            if capture is None:
                continue
            drained = 0
            first_flags = 0
            frames = 0
            while True:
                try:
                    _captured_ns, flags, frame_count = capture.status_events.popleft()
                except IndexError:
                    break
                if drained == 0:
                    first_flags = flags
                drained += 1
                frames += max(0, frame_count)
            if drained == 0:
                continue
            self._pa_status_event_count += drained
            entry: dict[str, object] = {
                "track": capture.track.value,
                "reason": "pa_status_flags",
                "status": describe_pa_status_flags(first_flags),
                "callbacks": drained,
                "frames": frames,
                "occurred_at": now_tz().isoformat(),
            }
            logger.warning(
                "PortAudio ha segnalato %s su %d callback della traccia %s.",
                entry["status"],
                drained,
                capture.track.value,
            )
            if assembler is not None:
                assembler.record_discontinuity(entry)

    def _run_assembler_loop(self) -> None:
        set_thread_log_session_id(self.session_id)
        raise_current_thread_priority()
        last_status_check = float(self._clock())
        while not self._stop_event.is_set() and not self._finalize_event.is_set():
            try:
                try:
                    block = self._block_queue.get(timeout=0.1)
                    try:
                        if self._assembler is not None and self._assembler.is_open:
                            self._assembler.write_block(block)
                    finally:
                        self._block_queue.task_done()
                except queue.Empty:
                    if self._assembler is not None and self._assembler.is_open:
                        self._assembler.compensate_idle_tracks()

                now_mono = float(self._clock())
                if now_mono - last_status_check >= 1.0:
                    last_status_check = now_mono
                    self._collect_status_flag_events()
                if self._assembler is not None and self._assembler.is_open:
                    if self._assembler.should_rotate(now_monotonic=now_mono):
                        rotated_chunk = self._assembler.rotate_chunk(
                            is_final=False,
                            now_monotonic=now_mono,
                        )
                        self._record_chunk_in_state(rotated_chunk)
                        self._finalization_worker.submit(rotated_chunk)

                    session_elapsed = now_mono - self._session_started_monotonic
                    if session_elapsed >= float(self._max_session_duration_sec):
                        logger.info(
                            "Raggiunta durata massima sessione (%ds), avvio finalizzazione.",
                            self._max_session_duration_sec,
                        )
                        self._finalize_event.set()
                        if self._stage_reporter is not None:
                            self._stage_reporter.set_stage(
                                Stage.FINALIZING_AUDIO,
                                "Durata massima sessione raggiunta (4 ore)",
                            )
                        if self._on_max_duration_reached is not None:
                            self._on_max_duration_reached()
                        break
            except Exception as exc:
                logger.error("Errore nel loop ChunkAssembler: %s", exc, exc_info=True)

    def stop_capture(self) -> None:
        """Ferma la cattura e chiude l'ultimo chunk senza troncare i WAV (sincrono, veloce).

        Va invocato sullo stesso thread che ha chiamato start(): SetThreadExecutionState vale
        per singolo thread, quindi solo lì il blocco dello standby viene davvero rilasciato.
        La normalizzazione FFmpeg dei chunk ancora in coda prosegue in background:
        attenderla con wait_normalized().
        """
        try:
            if self.is_paused:
                self.resume()
            self._stop_event.set()
            self._finalize_event.set()

            # Gli status_flags vanno raccolti prima che i thread di cattura vengano rilasciati
            self._collect_status_flag_events()
            self._stop_capture_threads()

            assembler_thread = self._assembler_thread
            self._assembler_thread = None
            if (
                assembler_thread is not None
                and assembler_thread.is_alive()
                and assembler_thread is not threading.current_thread()
            ):
                assembler_thread.join(timeout=5.0)

            self._drain_block_queue()

            if self._assembler is not None and self._assembler.is_open:
                final_chunk = self._assembler.rotate_chunk(
                    is_final=True,
                    now_monotonic=float(self._clock()),
                )
                self._record_chunk_in_state(final_chunk)
                self._finalization_worker.submit(final_chunk)

            self._finalization_worker.finish()
            self._terminate_pyaudio()
            with self._lock:
                self._is_recording = False
        finally:
            if self._sleep_prevented:
                restore_system_sleep()
                self._sleep_prevented = False

    def wait_normalized(
        self,
        shutdown_event: threading.Event | None = None,
        *,
        poll_interval_sec: float = 0.5,
        timeout_sec: float | None = None,
    ) -> list[AudioChunk]:
        """Attende la fine della normalizzazione dei chunk chiusi; interrompibile dallo shutdown."""
        deadline = None if timeout_sec is None else time.monotonic() + max(0.0, timeout_sec)
        while self._finalization_worker.is_alive():
            if shutdown_event is not None and shutdown_event.is_set():
                raise ShutdownRequested(
                    "Normalizzazione audio interrotta: arresto richiesto."
                )
            if deadline is not None and time.monotonic() >= deadline:
                raise AudioProcessingError(
                    "Timeout in attesa della normalizzazione FFmpeg dei chunk audio."
                )
            self._finalization_worker.join(timeout=poll_interval_sec)
        return self._finalization_worker.processed_chunks

    def stop_and_finalize(self) -> list[AudioChunk]:
        """Ferma la cattura, chiude l'ultimo chunk, attende la normalizzazione e ripristina lo standby."""
        self.stop_capture()
        return self.wait_normalized(timeout_sec=300.0)
