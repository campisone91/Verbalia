from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import threading
from pathlib import Path

import pytest

import bootstrap
from config import PROJECT_ROOT
from modules.contracts import (
    DeviceDescriptor,
    SessionContext,
    ShutdownRequested,
    Stage,
)
from modules.processing import AiResourceArbiter, ProcessingJob, ProcessingQueue
from modules.recorder import DualTrackRecorder, describe_pa_status_flags
from modules.session_runtime import EventBus, SessionStageReporter
from modules.state_manager import StateManager
from modules.tray_ui import create_stage_icon_image


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


def test_pa_status_flags_become_discontinuities_and_stop_is_split(
    tmp_path: Path,
    sample_session_context: SessionContext,
    fake_devices: tuple[DeviceDescriptor, DeviceDescriptor],
) -> None:
    """B24: gli overflow PortAudio diventano discontinuità; stop_capture e wait_normalized separati."""
    sm = StateManager(sample_session_context.session_dir, temp_root=tmp_path / "temp")
    sm.initialize(sample_session_context)
    released = threading.Event()

    def _slow_normalizer(_ffmpeg: Path, raw: Path, norm: Path, **_kw: object) -> Path:
        released.wait(timeout=5)
        raw.replace(norm)
        return norm

    recorder = DualTrackRecorder(
        session_id=sample_session_context.session_id,
        session_dir=sample_session_context.session_dir,
        on_chunk_ready=lambda _c: None,
        state_manager=sm,
        pyaudio_factory=lambda: _FakePA(),
        device_discoverer=lambda _pa: fake_devices,
    )
    recorder._finalization_worker._normalizer = _slow_normalizer
    recorder.start()
    assert recorder._mic_thread is not None
    callback = recorder._mic_thread.create_stream_callback()
    callback(b"\x00\x00" * 320, 320, None, 0x2)
    callback(b"\x00\x00" * 320, 320, None, 0x2)
    callback(b"\x00\x00" * 320, 320, None, 0)

    recorder.stop_capture()
    assert recorder.is_recording is False
    assert recorder.pa_status_event_count == 2
    context = sm.load()
    flags = [d for d in context.discontinuities if d.get("reason") == "pa_status_flags"]
    assert flags and flags[0]["status"] == "input_overflow" and flags[0]["callbacks"] == 2

    shutdown = threading.Event()
    shutdown.set()
    with pytest.raises(ShutdownRequested):
        recorder.wait_normalized(shutdown, poll_interval_sec=0.05)
    released.set()
    chunks = recorder.wait_normalized(None, poll_interval_sec=0.05)
    assert len(chunks) == 1
    assert describe_pa_status_flags(0x3) == "input_overflow+input_underflow"


def test_probe_ollama_gpu_parses_api_ps(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    payload = {
        "models": [
            {"name": "llama3.1:8b", "size": 6_000_000_000, "size_vram": 6_000_000_000},
            {"name": "qwen3:14b", "size": 10_000_000_000, "size_vram": 0},
        ]
    }

    class _Response(io.BytesIO):
        def __enter__(self) -> _Response:
            return self

        def __exit__(self, *_a: object) -> None:
            return None

    monkeypatch.setattr(
        "bootstrap.urllib.request.urlopen",
        lambda *_a, **_kw: _Response(json.dumps(payload).encode("utf-8")),
    )
    result = bootstrap.probe_ollama_gpu("http://localhost:11434", "llama3.1:8b")
    assert result["reachable"] is True and result["gpu_percent"] == 100.0

    server_log = tmp_path / "server.log"
    server_log.write_text(
        'time=1 level=INFO msg="server config"\n'
        "msg=\"dropping integrated GPU; to enable, set OLLAMA_IGPU_ENABLE=1\"\n",
        encoding="utf-8",
    )
    monkeypatch.setattr("bootstrap._default_ollama_server_log", lambda: server_log)
    cpu_only = bootstrap.probe_ollama_gpu("http://localhost:11434", "qwen3:14b")
    assert cpu_only["gpu_percent"] == 0.0
    assert "OLLAMA_IGPU_ENABLE" in str(cpu_only["hint"])

    server_log.write_text(
        server_log.read_text(encoding="utf-8") + 'msg="server config"\nmsg="inference compute" library=Vulkan\n',
        encoding="utf-8",
    )
    assert bootstrap.detect_ollama_igpu_dropped(server_log) is False


def test_ollama_serve_env_enables_igpu() -> None:
    assert bootstrap.OLLAMA_SERVE_ENV_OVERRIDES["OLLAMA_IGPU_ENABLE"] == "1"


def test_session_reporter_validates_without_forcing(tmp_path: Path, sample_session_context: SessionContext) -> None:
    """B2: una transizione non valida viene ignorata invece di essere forzata nello state.json."""
    sample_session_context.stage = Stage.RECORDING
    sm = StateManager(sample_session_context.session_dir, temp_root=tmp_path / "temp")
    sm.initialize(sample_session_context)
    bus = EventBus()
    reporter = SessionStageReporter(sample_session_context.session_id, sm, bus, initial_stage=Stage.RECORDING)
    reporter.set_stage(Stage.FINALIZING_AUDIO, "chiusura")
    reporter.set_stage(Stage.RECORDING_AND_TRANSCRIBING, "chunk tardivo")
    assert reporter.stage == Stage.FINALIZING_AUDIO
    assert sm.load().stage == Stage.FINALIZING_AUDIO
    reporter.set_stage(Stage.QUEUED, "coda")
    assert sm.load().stage == Stage.QUEUED
    events = []
    while (event := bus.get(timeout=0.01)) is not None:
        events.append(event.stage)
    assert events == [Stage.FINALIZING_AUDIO, Stage.QUEUED]


def test_arbiter_policy_and_processing_queue_order() -> None:
    arbiter = AiResourceArbiter("auto")
    assert arbiter.parallel is False
    arbiter.refresh(100.0)
    assert arbiter.parallel is True

    in_llm = threading.Event()
    release = threading.Event()

    def _llm() -> None:
        with arbiter.llm_lock:
            in_llm.set()
            release.wait(timeout=5)

    thread = threading.Thread(target=_llm)
    thread.start()
    assert in_llm.wait(timeout=5)
    # In parallelo Whisper non attende l'LLM
    assert arbiter.whisper_lock.acquire(timeout=0.5) is True
    arbiter.whisper_lock.release()
    arbiter.refresh(0.0)
    assert arbiter.parallel is False
    release.set()
    thread.join()
    assert AiResourceArbiter("sequential").parallel is False
    fixed = AiResourceArbiter("parallel")
    fixed.refresh(0.0)
    assert fixed.parallel is True

    queue = ProcessingQueue()
    queue.put(ProcessingJob("A", Path("a"), not_before=10**12))
    queue.put(ProcessingJob("B", Path("b")))
    queue.put(ProcessingJob("B", Path("b")))
    assert len(queue) == 2
    job = queue.get(timeout=0.1)
    assert job is not None and job.session_id == "B"
    assert queue.get(timeout=0.1) is None


def test_queue_badge_icon_and_dashboard_helpers(tmp_path: Path) -> None:
    plain = create_stage_icon_image(Stage.RECORDING)
    badged = create_stage_icon_image(Stage.RECORDING, badge_count=3)
    assert plain.tobytes() != badged.tobytes()

    from modules.dashboard_ui import describe_progress, format_duration, pipeline_marks

    assert format_duration(3725) == "1h 02m"
    assert format_duration(75) == "1m 15s"
    assert describe_progress(
        {"stage": "summarizing_map", "progress_current": 3, "progress_total": 6, "tokens_generated": 812}
    ) == "MAP 3/6 · 812 token"
    assert pipeline_marks("summarizing_map", "processing") == ["✓", "✓", "✓", "●", "○", "○", "○"]
    assert pipeline_marks("summarizing_map", "failed")[3] == "✗"
    assert pipeline_marks("cleaning_up", "completed") == ["✓"] * 7


_DASHBOARD_PROBE = r'''
import json, sys
import bootstrap
bootstrap._fetch_ollama_models = lambda *_a, **_kw: ["llama3.1:8b"]
import tkinter as tk
from modules.dashboard_ui import DashboardApp
from modules.job_registry import JobRegistry

registry = JobRegistry()
registry.upsert_job("S1", title="Call A", status="processing", stage="summarizing_map")
registry.update_stage("S1", "summarizing_map", "MAP 1/2", 1, 2)
registry.upsert_job("S2", title="Vecchia", status="completed", processing_ended_at="2020-01-01T00:00:00+00:00")
try:
    root = tk.Tk()
except tk.TclError:
    print(json.dumps({"skip": True}))
    sys.exit(0)
root.withdraw()
app = DashboardApp(root, registry, initial_tab="impostazioni")
app._info = lambda *args, **kwargs: None
app._confirm = lambda *args, **kwargs: True
root.update()
result = {"active": list(app.active_tree.get_children())}
app._selected_active = "S1"
app._refresh_detail()
result["step_map"] = app.step_vars[3].get()
app._confirm = lambda *args, **kwargs: True
app._cmd_stop_selected()
result["commands_after_stop"] = registry.fetch_pending_commands()
app._cmd_start_manual()
result["commands"] = registry.fetch_pending_commands()
app.var_all_days.set(True)
app._load_history()
result["history"] = sorted(app._history_rows)
result["has_detail_level"] = ("ollama", "detail_level") in app._setting_vars
result["has_recap_email_setting"] = ("output", "generate_recap_email") in app._setting_vars
result["map_block"] = str(app._collect_settings_payload()["ollama"]["map_block_duration_sec"])
for _ in range(10):
    root.update()
root.destroy()
print(json.dumps(result))
'''


def test_dashboard_window_builds_and_sends_commands(tmp_path: Path) -> None:
    """Il cruscotto gira in un processo dedicato (come in produzione): Tk resta su un solo thread."""
    pytest.importorskip("tkinter")
    env = dict(os.environ)
    env["TEAMS_SCRIBE_DATA_DIR"] = str(tmp_path / "data")
    env["TEAMS_SCRIBE_SETTINGS_PATH"] = str(tmp_path / "settings.json")
    env["TEAMS_SCRIBE_LOG_DIR"] = str(tmp_path / "logs")
    completed = subprocess.run(
        [sys.executable, "-c", _DASHBOARD_PROBE],
        cwd=str(PROJECT_ROOT),
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    result = json.loads(completed.stdout.strip().splitlines()[-1])
    if result.get("skip"):
        pytest.skip("Display Tk non disponibile")
    assert result["active"] == ["S1"]
    assert result["step_map"].startswith("●")
    assert result["commands_after_stop"] == [["cancel_job", {"session_id": "S1"}]]
    assert result["commands"] == [["start_manual", {}]]
    assert result["history"] == ["S1", "S2"]
    assert result["has_detail_level"] is True
    assert result["has_recap_email_setting"] is True
    assert result["map_block"] == "600"


def test_worker_and_processor_cancellation(tmp_path: Path) -> None:
    """Verifica la cancellazione del job in coda e l'interruzione del processore."""
    from modules.contracts import JobCancelledError
    from modules.processing import ProcessingJob, ProcessingQueue, ProcessingWorker, SessionProcessor

    queue = ProcessingQueue()
    job = ProcessingJob(session_id="S_TEST_CANCEL", session_dir=tmp_path / "s_test")
    queue.put(job)
    assert len(queue) == 1

    bus = EventBus()
    shutdown = threading.Event()

    class _FakeEngine:
        pass

    class _FakeArbiter:
        parallel = False

    class _FakeDocx:
        pass

    processor = SessionProcessor(
        engine=_FakeEngine(),  # type: ignore[arg-type]
        arbiter=_FakeArbiter(),  # type: ignore[arg-type]
        bus=bus,
        shutdown_event=shutdown,
        docx_builder=_FakeDocx(),  # type: ignore[arg-type]
        summarizer_provider=lambda _r: None,  # type: ignore[return-value]
        output_dir_provider=lambda _r: tmp_path,
        ffmpeg_provider=lambda: tmp_path,
    )

    worker = ProcessingWorker(
        queue=queue,
        processor=processor,
        bus=bus,
        shutdown_event=shutdown,
        runtime_factory=lambda _p: None,  # type: ignore[return-value]
    )

    # 1. Cancellazione di un job in coda
    cancelled = worker.cancel_session("S_TEST_CANCEL")
    assert cancelled is True
    assert len(queue) == 0

    # 2. Cancellazione del job attivo sul processore
    processor.cancel_active_job()
    with pytest.raises(JobCancelledError):
        processor._check_shutdown()

