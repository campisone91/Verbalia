from __future__ import annotations

import wave
from pathlib import Path
from queue import Queue
from unittest.mock import MagicMock

import numpy as np
import pytest

from modules.contracts import (
    ALLOWED_STAGE_TRANSITIONS,
    SessionMode,
    Stage,
    TrackLabel,
    TranscriptSegment,
    UiCommandType,
    validate_stage_transition,
)
from modules.diarizer import (
    AudioFeatureExtractor,
    OnnxSpeakerEmbedder,
    SessionSpeakerTracker,
)
from modules.hotkey import (
    MOD_ALT,
    MOD_CONTROL,
    MOD_SHIFT,
    GlobalHotkeyListener,
    parse_hotkey_string,
)
from modules.transcript_utils import (
    format_transcript_segment,
    parse_transcript_line,
)


def _generate_test_wav(path: Path, freq: float = 220.0, duration_sec: float = 2.0) -> None:
    """Genera un file WAV mono a 16 kHz con tono puro."""
    sample_rate = 16000
    t = np.linspace(0, duration_sec, int(sample_rate * duration_sec), endpoint=False)
    # Tono con armoniche per un timbro acustico realistico
    signal = 0.6 * np.sin(2 * np.pi * freq * t) + 0.3 * np.sin(2 * np.pi * 2 * freq * t)
    pcm_data = (signal * 32767).astype(np.int16).tobytes()

    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(pcm_data)


def test_contracts_paused_stage_and_transitions() -> None:
    assert Stage.PAUSED in ALLOWED_STAGE_TRANSITIONS[Stage.RECORDING]
    assert Stage.PAUSED in ALLOWED_STAGE_TRANSITIONS[Stage.RECORDING_AND_TRANSCRIBING]
    assert Stage.PAUSED in ALLOWED_STAGE_TRANSITIONS[Stage.GRACE_PERIOD]
    assert Stage.RECORDING in ALLOWED_STAGE_TRANSITIONS[Stage.PAUSED]
    assert Stage.FINALIZING_AUDIO in ALLOWED_STAGE_TRANSITIONS[Stage.PAUSED]
    validate_stage_transition(Stage.RECORDING, Stage.PAUSED)
    validate_stage_transition(Stage.PAUSED, Stage.RECORDING)
    assert SessionMode.IMPORTED == "imported"
    assert UiCommandType.PAUSE_RECORDING == "pause_recording"
    assert UiCommandType.RESUME_RECORDING == "resume_recording"
    assert UiCommandType.TOGGLE_PAUSE == "toggle_pause"
    assert UiCommandType.IMPORT_AUDIO == "import_audio"


def test_transcript_segment_speaker_and_utils() -> None:
    seg = TranscriptSegment(
        track=TrackLabel.OTHERS,
        start_sec=10.0,
        end_sec=12.0,
        text="Salve a tutti",
        speaker="  INTERLOCUTORE   2  ",
    )
    assert seg.speaker == "INTERLOCUTORE 2"
    assert seg.display_label == "INTERLOCUTORE 2"

    line = format_transcript_segment(seg)
    assert line == "[00:00:10] [INTERLOCUTORE 2]: Salve a tutti"

    parsed = parse_transcript_line(line)
    assert parsed is not None
    assert parsed.track == TrackLabel.OTHERS
    assert parsed.speaker == "INTERLOCUTORE 2"
    assert parsed.text == "Salve a tutti"

    # Segmento IO non usa speaker come display_label
    seg_io = TranscriptSegment(
        track=TrackLabel.IO,
        start_sec=5.0,
        end_sec=8.0,
        text="Buongiorno",
        speaker="Qualcuno",
    )
    assert seg_io.display_label == "IO"


def test_parse_hotkey_string() -> None:
    mod, vk = parse_hotkey_string("Ctrl+Alt+P")
    assert mod == (MOD_CONTROL | MOD_ALT)
    assert vk == ord("P")

    mod2, vk2 = parse_hotkey_string("ctrl+shift+f5")
    assert mod2 == (MOD_CONTROL | MOD_SHIFT)
    assert vk2 == 0x74  # VK_F5

    mod3, vk3 = parse_hotkey_string("alt+space")
    assert mod3 == MOD_ALT
    assert vk3 == 0x20

    with pytest.raises(ValueError):
        parse_hotkey_string("")
    with pytest.raises(ValueError):
        parse_hotkey_string("Ctrl+Alt")
    with pytest.raises(ValueError):
        parse_hotkey_string("Ctrl+Alt+P+X")


def test_global_hotkey_listener_debounce_and_trigger() -> None:
    q: Queue[UiCommandType] = Queue()
    callback = MagicMock()
    listener = GlobalHotkeyListener(
        hotkey_str="Ctrl+Alt+P",
        command_queue=q,
        on_trigger=callback,
        debounce_sec=0.10,
    )

    # Primo trigger: accettato
    assert listener.trigger_now() is True
    assert q.get_nowait() == UiCommandType.TOGGLE_PAUSE
    assert callback.call_count == 1

    # Secondo trigger immediato: bloccato da debounce
    assert listener.trigger_now() is False
    assert q.empty()
    assert callback.call_count == 1


def test_global_hotkey_listener_start_stop_mock() -> None:
    reg_mock = MagicMock(return_value=True)
    unreg_mock = MagicMock(return_value=True)

    listener = GlobalHotkeyListener(
        hotkey_str="Ctrl+Alt+P",
        register_fn=reg_mock,
        unregister_fn=unreg_mock,
    )
    listener.start()
    assert listener.is_running() is True
    assert reg_mock.called

    listener.stop()
    assert listener.is_running() is False
    assert unreg_mock.called


def test_audio_feature_extractor_and_embedding(tmp_path: Path) -> None:
    wav1 = tmp_path / "voice1.wav"
    _generate_test_wav(wav1, freq=180.0, duration_sec=2.0)

    samples = AudioFeatureExtractor.load_wav_mono_16k(wav1)
    assert len(samples) == 32000
    assert np.max(np.abs(samples)) > 0.1

    log_mel = AudioFeatureExtractor.extract_log_mel_filterbank(samples, n_mels=80)
    assert log_mel.ndim == 2
    assert log_mel.shape[1] == 80

    emb1 = AudioFeatureExtractor.extract_acoustic_embedding(samples)
    assert emb1 is not None
    assert emb1.ndim == 1
    assert np.isclose(np.linalg.norm(emb1), 1.0, atol=1e-4)

    # Audio silenzioso -> None
    silent_samples = np.zeros(16000, dtype=np.float32)
    assert AudioFeatureExtractor.extract_acoustic_embedding(silent_samples) is None


def test_session_speaker_tracker_multi_and_single_speaker(tmp_path: Path) -> None:
    wav_path = tmp_path / "multi.wav"
    sample_rate = 16000
    # Genera 2 toni distinti: speaker A (150Hz) nei primi 2 sec, speaker B (600Hz) nei secondi 2 sec
    t1 = np.linspace(0, 2.0, 32000, endpoint=False)
    sig1 = 0.7 * np.sin(2 * np.pi * 150 * t1)
    t2 = np.linspace(0, 2.0, 32000, endpoint=False)
    sig2 = 0.7 * np.sin(2 * np.pi * 600 * t2)
    combined = np.concatenate([sig1, sig2])

    with wave.open(str(wav_path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes((combined * 32767).astype(np.int16).tobytes())

    session_dir = tmp_path / "session_test"
    tracker = SessionSpeakerTracker(
        session_dir=session_dir,
        similarity_threshold=0.85,
        min_segment_sec=0.5,
    )

    segs = [
        TranscriptSegment(track=TrackLabel.OTHERS, start_sec=0.2, end_sec=1.8, text="Parlante 1 parla"),
        TranscriptSegment(track=TrackLabel.OTHERS, start_sec=2.2, end_sec=3.8, text="Parlante 2 risponde"),
    ]

    assigned = tracker.assign_speakers_to_segments(wav_path, segs)
    assert len(assigned) == 2
    # Due parlanti distinti identificati
    assert assigned[0].speaker == "INTERLOCUTORE 1"
    assert assigned[1].speaker == "INTERLOCUTORE 2"

    # Centroidi persistiti su disco
    centroids_file = session_dir / "transcripts" / "speaker_centroids.json"
    assert centroids_file.is_file()

    # Ricaricando lo stesso tracker da disco
    tracker2 = SessionSpeakerTracker(session_dir=session_dir)
    assert len(tracker2._centroids) == 2
