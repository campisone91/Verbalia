from __future__ import annotations

import queue
import threading
from pathlib import Path

from modules.audio_utils import create_silent_wav, get_wav_duration_sec
from modules.contracts import (
    AudioChunk,
    CapturedBlock,
    ChunkStatus,
    DeviceDescriptor,
    SessionContext,
    TrackLabel,
)
from modules.recorder import (
    AudioFinalizationWorker,
    ChunkAssembler,
    DualTrackRecorder,
    MicCaptureThread,
    compute_audio_queue_size,
    enqueue_captured_block,
)
from modules.state_manager import StateManager


def test_bounded_queue_never_blocks_and_records_discontinuity() -> None:
    """Verifica che la queue bounded non blocchi il callback e registri una discontinuity se piena."""
    q_size = compute_audio_queue_size(block_ms=20, queue_seconds=1)
    assert q_size == 50

    bounded_q: queue.Queue[CapturedBlock] = queue.Queue(maxsize=1)
    discontinuities: list[dict[str, object]] = []
    block = CapturedBlock(
        track=TrackLabel.IO,
        captured_at_ns=1000,
        frame_count=320,
        sample_rate=16000,
        channels=1,
        sample_width=2,
        payload=b"\x00" * 640,
    )

    assert enqueue_captured_block(bounded_q, block, discontinuities) is True
    assert enqueue_captured_block(bounded_q, block, discontinuities) is False
    assert len(discontinuities) == 1
    assert discontinuities[0]["reason"] == "queue_full"


def test_stream_callback_enqueues_captured_block(
    fake_devices: tuple[DeviceDescriptor, DeviceDescriptor],
) -> None:
    """Verifica che il callback PyAudio incapsuli i frame in CapturedBlock senza bloccare."""
    mic_dev, _ = fake_devices
    q: queue.Queue[CapturedBlock] = queue.Queue(maxsize=10)
    disc: list[dict[str, object]] = []
    thread = MicCaptureThread(
        device=mic_dev,
        block_queue=q,
        stop_event=threading.Event(),
        discontinuities=disc,
    )
    cb = thread.create_stream_callback()
    payload = b"\x01\x00" * 320
    res_data, status = cb(payload, 320, None, 0)
    assert res_data is None
    assert status == 0
    queued = q.get_nowait()
    assert queued.track == TrackLabel.IO
    assert queued.frame_count == 320
    assert queued.payload == payload


def test_chunk_assembler_31_minute_simulation_produces_three_chunks_with_overlap(
    tmp_path: Path,
    fake_devices: tuple[DeviceDescriptor, DeviceDescriptor],
) -> None:
    """Verifica AC03: una sessione simulata di 31 minuti genera esattamente tre chunk con overlap."""
    mic_dev, loop_dev = fake_devices
    now_mono = [0.0]
    audio_dir = tmp_path / "audio"

    assembler = ChunkAssembler(
        session_id="20261023_140000_test0001",
        audio_dir=audio_dir,
        io_device=mic_dev,
        others_device=loop_dev,
        chunk_duration_sec=900,
        chunk_overlap_sec=3,
        clock=lambda: now_mono[0],
    )
    assembler.open_initial_chunk(now_monotonic=0.0)

    one_sec_payload = b"\x00\x00" * 16000
    generated_chunks: list[AudioChunk] = []

    for minute in range(1, 32):
        now_mono[0] = float(minute * 60)
        for track in (TrackLabel.IO, TrackLabel.OTHERS):
            assembler.write_block(
                CapturedBlock(
                    track=track,
                    captured_at_ns=int(now_mono[0] * 1e9),
                    frame_count=16000,
                    sample_rate=16000,
                    channels=1,
                    sample_width=2,
                    payload=one_sec_payload,
                ),
                elapsed_chunk_sec=1.0,
            )
        if assembler.should_rotate(now_monotonic=now_mono[0]):
            rotated = assembler.rotate_chunk(is_final=False, now_monotonic=now_mono[0])
            generated_chunks.append(rotated)

    final_chunk = assembler.rotate_chunk(is_final=True, now_monotonic=now_mono[0])
    generated_chunks.append(final_chunk)

    assert len(generated_chunks) == 3
    assert [c.index for c in generated_chunks] == [0, 1, 2]
    assert generated_chunks[0].is_final is False
    assert generated_chunks[1].is_final is False
    assert generated_chunks[2].is_final is True
    # Verifica che il secondo chunk includa i 3 secondi di overlap del primo
    assert generated_chunks[1].start_offset_sec < generated_chunks[0].end_offset_sec
    for c in generated_chunks:
        assert c.io_wav_path.exists()
        assert c.others_wav_path.exists()
        assert get_wav_duration_sec(c.io_wav_path) > 0.0


def test_audio_finalization_worker_and_hotswap_reconnect(
    tmp_path: Path,
    sample_session_context: SessionContext,
    fake_devices: tuple[DeviceDescriptor, DeviceDescriptor],
) -> None:
    """Verifica AC07: l'hot-swap chiude il chunk parziale, riconnette i device e non distrugge la sessione."""
    temp_root = tmp_path / "temp"
    sm = StateManager(sample_session_context.session_dir, temp_root=temp_root)
    sm.initialize(sample_session_context)

    ready_chunks: list[AudioChunk] = []

    def _fake_normalizer(
        _ffmpeg: Path,
        raw_wav: Path,
        norm_wav: Path,
        *,
        delete_raw_on_success: bool = True,
    ) -> Path:
        create_silent_wav(norm_wav, duration_sec=0.5)
        if delete_raw_on_success and raw_wav.exists():
            raw_wav.unlink()
        return norm_wav

    worker = AudioFinalizationWorker(
        ffmpeg_path=tmp_path / "ffmpeg.exe",
        on_chunk_ready=lambda c: ready_chunks.append(c),
        state_manager=sm,
        normalizer=_fake_normalizer,
    )

    audio_dir = sample_session_context.session_dir / "audio"
    raw_io = create_silent_wav(audio_dir / "chunk_000_io_raw.wav", duration_sec=0.5)
    raw_others = create_silent_wav(audio_dir / "chunk_000_altri_raw.wav", duration_sec=0.5)
    chunk0 = AudioChunk(
        index=0,
        session_id=sample_session_context.session_id,
        start_offset_sec=0.0,
        end_offset_sec=0.5,
        io_wav_path=raw_io,
        others_wav_path=raw_others,
        is_final=False,
        created_at=sample_session_context.started_at,
        status=ChunkStatus.RAW_CLOSED,
    )
    processed = worker.process_chunk(chunk0)
    assert processed.status == ChunkStatus.NORMALIZED
    assert len(ready_chunks) == 1
    assert sm.load().normalized_chunks == [0]

    # Test hot-swap su DualTrackRecorder con fake PyAudio
    class _FakeStream:
        def start_stream(self) -> None:
            pass

        def is_active(self) -> bool:
            return True

        def stop_stream(self) -> None:
            pass

        def close(self) -> None:
            pass

    class _FakePA:
        def open(self, **_kwargs: object) -> _FakeStream:
            return _FakeStream()

        def terminate(self) -> None:
            pass

    recorder = DualTrackRecorder(
        session_id=sample_session_context.session_id,
        session_dir=sample_session_context.session_dir,
        on_chunk_ready=lambda c: ready_chunks.append(c),
        state_manager=sm,
        pyaudio_factory=lambda: _FakePA(),
        device_discoverer=lambda _pa: fake_devices,
        reconnect_delay_sec=0.01,
        reconnect_max_attempts=3,
    )
    recorder._finalization_worker = worker
    recorder.start()
    assert recorder.is_recording is True

    reconnected = recorder.handle_device_error(
        TrackLabel.IO,
        RuntimeError("USB headset unplugged"),
    )
    assert reconnected is True
    assert recorder.is_recording is True

    finalized = recorder.stop_and_finalize()
    assert len(finalized) >= 1
    assert recorder.is_recording is False
