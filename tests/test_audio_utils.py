from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from modules.audio_utils import (
    build_ffmpeg_normalize_command,
    calculate_missing_frames,
    compute_silence_injection_frames,
    compute_track_drift_ms,
    create_silent_wav,
    discover_audio_devices,
    generate_silence_bytes,
    get_wav_duration_sec,
    inspect_wav_file,
    is_valid_normalized_wav,
    normalize_wav_with_ffmpeg,
    prepare_imported_audio_session,
    split_wav_file,
    validate_normalized_wav,
)
from modules.contracts import AudioDeviceError, AudioProcessingError


def test_missing_frames_silence_injection_and_drift() -> None:
    """Verifica il calcolo dei frame mancanti, l'iniezione silenzio limitata e il drift tra tracce."""
    assert calculate_missing_frames(1.0, 16000, 16000) == 0
    assert calculate_missing_frames(1.0, 16000, 12000) == 4000

    # Sotto tolleranza (60ms a 16kHz = 960 frame) -> 0 iniettati
    assert compute_silence_injection_frames(500, 16000) == 0

    # Sopra il massimo (2000ms a 16kHz = 32000 frame) -> limitato a 32000
    assert compute_silence_injection_frames(80000, 16000) == 32000

    silence = generate_silence_bytes(100, channels=2, sample_width=2)
    assert len(silence) == 400
    assert silence == b"\x00" * 400

    drift = compute_track_drift_ms(16000, 16000, 16800, 16000)
    assert drift == 50.0


def test_create_validate_and_split_wav(tmp_path: Path) -> None:
    """Verifica creazione WAV silenzioso, validazione 16kHz mono e split con overlap."""
    wav_path = create_silent_wav(tmp_path / "test.wav", duration_sec=2.5)
    assert validate_normalized_wav(wav_path) is True
    assert is_valid_normalized_wav(wav_path) is True
    assert abs(get_wav_duration_sec(wav_path) - 2.5) < 0.05

    # Split di un file di 2.5s in chunk da 1s con overlap di 0s -> 3 chunk
    chunks = split_wav_file(
        wav_path,
        tmp_path / "split_out",
        chunk_duration_sec=1,
        overlap_sec=0,
    )
    assert len(chunks) == 3
    assert wav_path.exists()  # Il file originale non viene mai eliminato


def test_validate_normalized_wav_rejects_invalid_files(tmp_path: Path) -> None:
    """Verifica che WAV stereo, sample rate errato o file corrotti vengano rifiutati."""
    stereo_wav = create_silent_wav(
        tmp_path / "stereo.wav",
        duration_sec=0.5,
        sample_rate=16000,
        channels=2,
    )
    with pytest.raises(AudioProcessingError, match="Canali WAV non validi"):
        validate_normalized_wav(stereo_wav)

    corrupt_wav = tmp_path / "corrupt.wav"
    corrupt_wav.write_bytes(b"NOT_A_WAV_HEADER" * 10)
    with pytest.raises(AudioProcessingError):
        validate_normalized_wav(corrupt_wav)


def test_normalize_wav_with_ffmpeg_preserves_raw_on_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verifica che il file raw venga preservato se FFmpeg fallisce e cancellato solo se valido."""
    raw_wav = create_silent_wav(
        tmp_path / "raw.wav",
        duration_sec=0.5,
        sample_rate=48000,
        channels=2,
    )
    norm_wav = tmp_path / "norm_16k.wav"
    ffmpeg_bin = tmp_path / "ffmpeg.exe"
    ffmpeg_bin.write_bytes(b"fake")

    cmd = build_ffmpeg_normalize_command(ffmpeg_bin, raw_wav, norm_wav)
    assert "-ar" in cmd and "16000" in cmd

    # Caso 1: FFmpeg fallisce -> raw_wav deve rimanere intatto (AC05 / DoD)
    def _fail_run(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(args=cmd, returncode=1, stdout="", stderr="ffmpeg error")

    monkeypatch.setattr("modules.audio_utils.subprocess.run", _fail_run)
    with pytest.raises(AudioProcessingError, match="codice 1"):
        normalize_wav_with_ffmpeg(ffmpeg_bin, raw_wav, norm_wav)
    assert raw_wav.exists()
    assert not norm_wav.exists()

    # Caso 2: FFmpeg ha successo e produce un WAV 16kHz mono valido -> raw_wav eliminato
    def _ok_run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        out_target = Path(command[-1])
        create_silent_wav(out_target, duration_sec=0.5, sample_rate=16000, channels=1)
        return subprocess.CompletedProcess(args=command, returncode=0, stdout="", stderr="")

    monkeypatch.setattr("modules.audio_utils.subprocess.run", _ok_run)
    result = normalize_wav_with_ffmpeg(ffmpeg_bin, raw_wav, norm_wav, delete_raw_on_success=True)
    assert result == norm_wav
    assert norm_wav.exists()
    assert not raw_wav.exists()


def test_discover_audio_devices_with_fake_pyaudio() -> None:
    """Verifica l'enumerazione di microfono e WASAPI loopback con un'istanza PyAudio simulata."""
    class _FakePyAudio:
        def get_default_input_device_info(self) -> dict[str, object]:
            return {
                "index": 0,
                "name": "Fake Mic",
                "hostApi": 0,
                "defaultSampleRate": 48000.0,
                "maxInputChannels": 1,
                "isLoopbackDevice": False,
            }

        def get_default_wasapi_loopback(self) -> dict[str, object]:
            return {
                "index": 3,
                "name": "Fake Speakers [Loopback]",
                "hostApi": 1,
                "defaultSampleRate": 48000.0,
                "maxInputChannels": 2,
                "isLoopbackDevice": True,
            }

        def get_host_api_info_by_index(self, idx: int) -> dict[str, object]:
            return {"name": "Windows WASAPI" if idx == 1 else "MME"}

    mic, loop = discover_audio_devices(_FakePyAudio())
    assert mic.name == "Fake Mic"
    assert mic.is_loopback is False
    assert loop.name == "Fake Speakers [Loopback]"
    assert loop.is_loopback is True

    class _EmptyPyAudio:
        def get_device_count(self) -> int:
            return 0

    with pytest.raises(AudioDeviceError):
        discover_audio_devices(_EmptyPyAudio())


def test_discover_audio_devices_fallback_enumeration() -> None:
    """Verifica il fallback di enumerazione manuale dei dispositivi quando i metodi default falliscono."""
    class _FallbackPyAudio:
        def get_default_input_device_info(self) -> dict[str, object]:
            raise OSError("No default input")

        def get_default_wasapi_loopback(self) -> dict[str, object]:
            raise OSError("No default loopback")

        def get_device_count(self) -> int:
            return 3

        def get_device_info_by_index(self, idx: int) -> dict[str, object]:
            devices: list[dict[str, object]] = [
                {
                    "index": 0,
                    "name": "USB Microphone",
                    "hostApi": 0,
                    "defaultSampleRate": 44100.0,
                    "maxInputChannels": 1,
                    "isLoopbackDevice": False,
                },
                {
                    "index": 1,
                    "name": "Default Speakers",
                    "hostApi": 1,
                    "defaultSampleRate": 48000.0,
                    "maxInputChannels": 0,
                    "maxOutputChannels": 2,
                    "isLoopbackDevice": False,
                },
                {
                    "index": 2,
                    "name": "Default Speakers [Loopback]",
                    "hostApi": 1,
                    "defaultSampleRate": 48000.0,
                    "maxInputChannels": 2,
                    "isLoopbackDevice": True,
                },
            ]
            return devices[idx]

        def get_default_output_device_info(self) -> dict[str, object]:
            return {"name": "Default Speakers"}

        def get_host_api_info_by_index(self, idx: int) -> dict[str, object]:
            return {"name": "Windows WASAPI" if idx == 1 else "MME"}

    mic, loop = discover_audio_devices(_FallbackPyAudio())
    assert mic.name == "USB Microphone"
    assert loop.name == "Default Speakers [Loopback]"
    assert loop.is_loopback is True


def test_inspect_wav_file(tmp_path: Path) -> None:
    """Verifica che inspect_wav_file legga correttamente metadati e durata."""
    wav_path = create_silent_wav(tmp_path / "sample.wav", duration_sec=3.0, sample_rate=16000, channels=1)
    info = inspect_wav_file(wav_path)
    assert info.sample_rate == 16000
    assert info.channels == 1
    assert info.sample_width == 2
    assert abs(info.duration_sec - 3.0) < 0.05
    assert info.frames == 48000


def test_prepare_imported_audio_session(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Verifica che prepare_imported_audio_session prepari la cartella, i chunk e salvi lo state."""
    raw_wav = create_silent_wav(tmp_path / "input.wav", duration_sec=5.0)

    def _mock_normalize(_ffmpeg: object, _src: Path, dst: Path, **_kwargs: object) -> Path:
        return create_silent_wav(dst, duration_sec=5.0)

    monkeypatch.setattr("modules.audio_utils.normalize_wav_with_ffmpeg", _mock_normalize)

    temp_root = tmp_path / "temp_sessions"
    context = prepare_imported_audio_session(
        input_audio_path=raw_wav,
        temp_dir=temp_root,
        chunk_duration_sec=2,
        overlap_sec=0,
    )

    assert context.session_dir.exists()
    assert (context.session_dir / "state.json").is_file()
    assert (context.session_dir / "audio").is_dir()
    assert len(context.recorded_chunks) == 3
    for idx in context.recorded_chunks:
        io_wav = context.session_dir / "audio" / f"chunk_{idx:03d}_io_16k.wav"
        others_wav = context.session_dir / "audio" / f"chunk_{idx:03d}_altri_16k.wav"
        assert io_wav.exists()
        assert others_wav.exists()


