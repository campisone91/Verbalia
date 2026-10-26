from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from modules.audio_utils import create_silent_wav
from modules.contracts import (
    AudioChunk,
    ChunkStatus,
    SessionContext,
    TranscriptionError,
)
from modules.state_manager import StateManager
from modules.transcriber import RollingTranscriber


class _FakeWhisperSegment:
    def __init__(self, start: float, end: float, text: str, avg_logprob: float = -0.2) -> None:
        self.start = start
        self.end = end
        self.text = text
        self.avg_logprob = avg_logprob


@pytest.fixture
def _reset_cuda_probe(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("modules.transcriber._CUDA_DEVICE_COUNT", None)
    monkeypatch.setattr("modules.transcriber._CUDA_LOAD_FAILED", False)


def test_transcriber_gpu_fallback_lazy_segments_wav_deletion_and_release(
    tmp_path: Path,
    sample_session_context: SessionContext,
    monkeypatch: pytest.MonkeyPatch,
    _reset_cuda_probe: None,
) -> None:
    """Verifica fallback GPU->CPU, consumo iteratore lazy, cancellazione WAV post-verifica e release."""
    # Simula una GPU CUDA presente ma non inizializzabile: deve tentare CUDA e ripiegare su CPU
    monkeypatch.setattr("modules.transcriber.cuda_device_count", lambda: 1)
    temp_root = tmp_path / "temp"
    sm = StateManager(sample_session_context.session_dir, temp_root=temp_root)
    sm.initialize(sample_session_context)

    factory_calls: list[tuple[str, str, str]] = []

    class _FakeModel:
        def transcribe(
            self,
            audio_path: str,
            **_kwargs: object,
        ) -> tuple[Iterator[_FakeWhisperSegment], dict[str, object]]:
            def _lazy_gen() -> Iterator[_FakeWhisperSegment]:
                if "io_16k" in audio_path:
                    yield _FakeWhisperSegment(1.0, 3.0, "Intervento locale sul progetto.")
                else:
                    yield _FakeWhisperSegment(4.0, 7.0, "Conferma da parte del team remoto.")

            return _lazy_gen(), {"language": "it"}

    def _model_factory(model_name: str, device: str, compute_type: str) -> object:
        factory_calls.append((model_name, device, compute_type))
        if device == "cuda":
            raise RuntimeError("CUDA driver non disponibile")
        return _FakeModel()

    transcriber = RollingTranscriber(
        session_dir=sample_session_context.session_dir,
        state_manager=sm,
        model_factory=_model_factory,
    )

    audio_dir = sample_session_context.session_dir / "audio"
    io_wav = create_silent_wav(audio_dir / "chunk_000_io_16k.wav", duration_sec=0.5)
    others_wav = create_silent_wav(audio_dir / "chunk_000_altri_16k.wav", duration_sec=0.5)
    chunk = AudioChunk(
        index=0,
        session_id=sample_session_context.session_id,
        start_offset_sec=10.0,
        end_offset_sec=20.0,
        io_wav_path=io_wav,
        others_wav_path=others_wav,
        is_final=True,
        created_at=sample_session_context.started_at,
        status=ChunkStatus.NORMALIZED,
    )

    transcriber.start()
    transcriber.submit(chunk)
    transcriber.finish()
    transcriber.join(timeout=10.0)

    # Verifica che abbia tentato prima CUDA e poi usato CPU int8
    assert len(factory_calls) == 2
    assert factory_calls[0][1] == "cuda"
    assert factory_calls[1][1] == "cpu"
    assert transcriber.active_device == "cpu"

    # Verifica AC05: WAV cancellati dopo la verifica del transcript
    assert not io_wav.exists()
    assert not others_wav.exists()
    assert chunk.status == ChunkStatus.AUDIO_DELETED

    ctx = sm.load()
    assert ctx.transcribed_chunks == [0]
    assert ctx.audio_deleted_chunks == [0]

    transcript_path = sample_session_context.session_dir / "transcripts" / "chunk_000.txt"
    content = transcript_path.read_text(encoding="utf-8")
    assert "[00:00:11] [IO]: Intervento locale sul progetto." in content
    assert "[00:00:14] [ALTRI]: Conferma da parte del team remoto." in content

    transcriber.release_model()
    assert transcriber.active_device is None


def test_transcriber_preserves_wav_when_transcription_fails(
    tmp_path: Path,
    sample_session_context: SessionContext,
) -> None:
    """Verifica che i WAV 16k non vengano mai eliminati se Whisper fallisce."""
    temp_root = tmp_path / "temp"
    sm = StateManager(sample_session_context.session_dir, temp_root=temp_root)
    sm.initialize(sample_session_context)

    def _failing_factory(_m: str, _d: str, _c: str) -> object:
        raise RuntimeError("Modello Whisper corrotto")

    transcriber = RollingTranscriber(
        session_dir=sample_session_context.session_dir,
        state_manager=sm,
        model_factory=_failing_factory,
    )

    audio_dir = sample_session_context.session_dir / "audio"
    io_wav = create_silent_wav(audio_dir / "chunk_000_io_16k.wav", duration_sec=0.5)
    others_wav = create_silent_wav(audio_dir / "chunk_000_altri_16k.wav", duration_sec=0.5)
    chunk = AudioChunk(
        index=0,
        session_id=sample_session_context.session_id,
        start_offset_sec=0.0,
        end_offset_sec=0.5,
        io_wav_path=io_wav,
        others_wav_path=others_wav,
        is_final=True,
        created_at=sample_session_context.started_at,
        status=ChunkStatus.NORMALIZED,
    )

    with pytest.raises(TranscriptionError):
        transcriber.transcribe_chunk_sync(chunk)

    assert io_wav.exists()
    assert others_wav.exists()
    assert chunk.status == ChunkStatus.FAILED


def test_transcriber_skips_cuda_without_nvidia_and_remembers_failure(
    tmp_path: Path,
    sample_session_context: SessionContext,
    monkeypatch: pytest.MonkeyPatch,
    _reset_cuda_probe: None,
) -> None:
    """B13: senza GPU CUDA non tenta mai il caricamento cuda; dopo un fallimento non lo ritenta."""
    temp_root = tmp_path / "temp"
    sm = StateManager(sample_session_context.session_dir, temp_root=temp_root)
    sm.initialize(sample_session_context)

    calls: list[str] = []

    class _Model:
        def transcribe(self, *_a: object, **_kw: object) -> tuple[list[object], dict[str, object]]:
            return [], {}

    def _factory(_m: str, device: str, _c: str) -> object:
        calls.append(device)
        if device == "cuda":
            raise RuntimeError("cuda non disponibile")
        return _Model()

    monkeypatch.setattr("modules.transcriber.cuda_device_count", lambda: 0)
    t1 = RollingTranscriber(sample_session_context.session_dir, sm, model_factory=_factory)
    t1._ensure_model_loaded()
    assert calls == ["cpu"]

    monkeypatch.setattr("modules.transcriber.cuda_device_count", lambda: 1)
    t2 = RollingTranscriber(sample_session_context.session_dir, sm, model_factory=_factory)
    t2._ensure_model_loaded()
    t3 = RollingTranscriber(sample_session_context.session_dir, sm, model_factory=_factory)
    t3._ensure_model_loaded()
    # Un solo tentativo CUDA fallito, poi solo CPU
    assert calls == ["cpu", "cuda", "cpu", "cpu"]
