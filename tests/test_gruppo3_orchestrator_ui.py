from __future__ import annotations

import argparse
import queue
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from modules.contracts import (
    AudioChunk,
    ChunkStatus,
    MeetingMetadata,
    SessionContext,
    SessionMode,
    Stage,
    TrackLabel,
    UiCommandType,
)
from modules.dashboard_ui import _STAGE_STEP_INDEX, _stage_label
from modules.job_registry import JobRegistry
from modules.orchestrator import MeetingScribeApplication, RecordingController
from main import build_arg_parser, main


def test_stage_paused_mapping_and_label():
    assert _STAGE_STEP_INDEX.get(Stage.PAUSED.value) == 0
    assert _STAGE_STEP_INDEX.get("paused") == 0
    assert _stage_label("paused") == "⏸ In Pausa"
    assert _stage_label(Stage.PAUSED.value) == "⏸ In Pausa"


def test_recording_controller_pause_resume_toggle(tmp_path: Path):
    app = MeetingScribeApplication(
        temp_dir=tmp_path / "temp",
        output_dir=tmp_path / "output",
        headless=True,
    )
    controller = RecordingController(app)

    # When no live session, pause/resume return False
    assert not controller.pause()
    assert not controller.resume()
    assert not controller.is_paused

    # Create dummy live session
    metadata = MeetingMetadata(title="Test Meeting", source="fallback")
    started = app.start_live_session(metadata, SessionMode.MANUAL)
    assert started
    assert app.live is not None

    # Pause recording
    paused_ok = controller.pause()
    assert paused_ok
    assert controller.is_paused
    assert app.live.reporter.stage == Stage.PAUSED

    # Pausing again returns False
    assert not controller.pause()

    # Toggle pause -> resumes
    toggled_resume = controller.toggle_pause()
    assert toggled_resume
    assert not controller.is_paused
    assert app.live.reporter.stage in (Stage.RECORDING, Stage.RECORDING_AND_TRANSCRIBING)

    # Toggle pause -> pauses
    toggled_pause = controller.toggle_pause()
    assert toggled_pause
    assert controller.is_paused
    assert app.live.reporter.stage == Stage.PAUSED

    # Resume recording
    resumed_ok = controller.resume()
    assert resumed_ok
    assert not controller.is_paused
    assert app.live.reporter.stage in (Stage.RECORDING, Stage.RECORDING_AND_TRANSCRIBING)

    app.finalize_live_session()
    app._perform_coordinated_shutdown()


def test_orchestrator_handle_pause_and_resume_commands(tmp_path: Path):
    app = MeetingScribeApplication(
        temp_dir=tmp_path / "temp",
        output_dir=tmp_path / "output",
        headless=True,
    )
    metadata = MeetingMetadata(title="Test Meeting", source="fallback")
    started = app.start_live_session(metadata, SessionMode.MANUAL)
    assert started

    # Send PAUSE_RECORDING
    app._handle_command(UiCommandType.PAUSE_RECORDING.value, {})
    assert app.controller.is_paused
    assert app.live.reporter.stage == Stage.PAUSED

    # Send RESUME_RECORDING
    app._handle_command(UiCommandType.RESUME_RECORDING.value, {})
    assert not app.controller.is_paused
    assert app.live.reporter.stage in (Stage.RECORDING, Stage.RECORDING_AND_TRANSCRIBING)

    # Send TOGGLE_PAUSE
    app._handle_command(UiCommandType.TOGGLE_PAUSE.value, {})
    assert app.controller.is_paused
    assert app.live.reporter.stage == Stage.PAUSED

    app.finalize_live_session()
    app._perform_coordinated_shutdown()


def test_orchestrator_enqueue_imported_audio(tmp_path: Path):
    app = MeetingScribeApplication(
        temp_dir=tmp_path / "temp",
        output_dir=tmp_path / "output",
        headless=True,
    )
    # Create a small valid WAV file
    from modules.audio_utils import create_silent_wav
    audio_path = tmp_path / "external_call.wav"
    create_silent_wav(audio_path, duration_sec=1.0)

    ctx = app.enqueue_imported_audio(audio_path, title="Imported Review")
    assert ctx is not None
    assert ctx.mode == SessionMode.IMPORTED
    assert ctx.metadata.title == "Imported Review"

    # Verify job is enqueued in processing_queue
    assert len(app.processing_queue) == 1
    job = app.processing_queue.get(timeout=0.0)
    assert job is not None
    assert job.session_id == ctx.session_id

    # Verify job is recorded in JobRegistry
    registry_job = app.registry.get_job(ctx.session_id)
    assert registry_job is not None
    assert registry_job["mode"] == "imported"
    assert registry_job["status"] == "queued"


def test_orchestrator_run_import_pipeline(tmp_path: Path):
    app = MeetingScribeApplication(
        temp_dir=tmp_path / "temp",
        output_dir=tmp_path / "output",
        headless=True,
    )
    from modules.audio_utils import create_silent_wav
    audio_path = tmp_path / "external_meeting.wav"
    create_silent_wav(audio_path, duration_sec=1.0)

    # Mock the worker execution to avoid real Ollama / Whisper calls in unit test
    fake_docx = tmp_path / "output" / "2026-10-28" / "Verbale_External.docx"
    fake_docx.parent.mkdir(parents=True, exist_ok=True)
    fake_docx.write_text("docx dummy", encoding="utf-8")

    with patch.object(app, "_make_worker") as mock_make_worker:
        mock_worker = MagicMock()
        def _fake_run(job, retry=False):
            app.tray.set_last_output_file(fake_docx)
            return 0
        mock_worker.run_job.side_effect = _fake_run
        mock_make_worker.return_value = mock_worker

        res_docx = app.run_import_pipeline(audio_path, title="External Meeting")
        assert res_docx == fake_docx


def test_cli_import_file_parser_and_dispatch(tmp_path: Path):
    parser = build_arg_parser()
    args = parser.parse_args(["--import-file", "test.mp3", "--title", "CLI Meeting"])
    assert str(args.import_file).endswith("test.mp3")
    assert args.title == "CLI Meeting"

    # Non-existent file error
    res_code = main(["--import-file", str(tmp_path / "non_existent.mp3")])
    assert res_code != 0


def test_dashboard_ui_pause_button_state_and_command(tmp_path: Path):
    try:
        import tkinter as tk
        root = tk.Tk()
    except Exception:
        return
    root.withdraw()
    try:
        from modules.dashboard_ui import DashboardApp

        registry = JobRegistry(tmp_path / "dash_test.db")
        app = DashboardApp(root, registry, initial_tab="cruscotto")
        root.update()

        assert hasattr(app, "btn_pause")
        # Initially no active jobs -> disabled
        assert "disabled" in app.btn_pause.state()

        # Simulate active recording job
        app._active_jobs = {
            "sess1": {
                "session_id": "sess1",
                "title": "Call 1",
                "status": "recording",
                "stage": Stage.RECORDING.value,
            }
        }
        app._update_action_buttons()
        assert "disabled" not in app.btn_pause.state()
        assert app.btn_pause.cget("text") == "⏸ Pausa"

        # Toggle pause sends pause command
        app._cmd_toggle_pause()
        commands = registry.fetch_pending_commands()
        assert any(cmd == UiCommandType.PAUSE_RECORDING.value for cmd, _ in commands)

        # Simulate job paused
        app._active_jobs["sess1"]["status"] = "paused"
        app._active_jobs["sess1"]["stage"] = Stage.PAUSED.value
        app._update_action_buttons()
        assert app.btn_pause.cget("text") == "▶ Riprendi"

        # Toggle pause sends resume command
        app._cmd_toggle_pause()
        commands2 = registry.fetch_pending_commands()
        assert any(cmd == UiCommandType.RESUME_RECORDING.value for cmd, _ in commands2)
        app._setting_vars.clear()
        del app
    finally:
        import gc

        gc.collect()
        root.destroy()
        gc.collect()


def test_dashboard_auto_refreshes_history_and_keeps_today_completed_in_cruscotto(tmp_path: Path):
    import gc
    from datetime import timedelta
    from modules.contracts import now_tz

    try:
        import tkinter as tk
        root = tk.Tk()
    except Exception:
        return
    root.withdraw()
    try:
        from modules.dashboard_ui import DashboardApp

        registry = JobRegistry(tmp_path / "dash_sync.db")
        now = now_tz()
        call_start = now.replace(hour=min(now.hour, 12), minute=0, second=0, microsecond=0)
        if call_start > now:
            call_start = now - timedelta(minutes=5)

        # 1. Job is recording when DashboardApp opens
        sid = "20261029_123106_c3d9fdd9"
        registry.upsert_job(
            sid,
            title="Call Test 12:31",
            mode="auto",
            status="recording",
            stage=Stage.RECORDING.value,
            call_started_at=call_start,
        )

        app = DashboardApp(root, registry, initial_tab="cruscotto")
        root.update()

        # Verify Storico initially shows "In registrazione"
        assert sid in app._history_rows
        assert app._history_rows[sid]["status"] == "recording"
        hist_vals_before = app.history_tree.item(sid, "values")
        assert "In registrazione" in hist_vals_before

        # 2. Job completes (>30 min ago in the same day to also verify daily retention in Cruscotto)
        ended_at = call_start + timedelta(minutes=1)
        registry.upsert_job(
            sid,
            status="completed",
            stage=Stage.COMPLETED.value,
            call_ended_at=ended_at,
            processing_started_at=ended_at,
            processing_ended_at=ended_at,
            docx_path=str(tmp_path / "2026-10-29_12-31_Call_Recap.docx"),
        )

        # 3. Trigger periodic _refresh() without touching Storico filters
        app._refresh()
        root.update()

        # Cruscotto still lists today's completed call even if >30 min old
        assert sid in app._active_jobs
        assert app.active_tree.exists(sid)

        # Storico automatically updated from "In registrazione" to "Completato"
        assert app._history_rows[sid]["status"] == "completed"
        hist_vals_after = app.history_tree.item(sid, "values")
        assert "Completato" in hist_vals_after
        assert "2026-10-29_12-31_Call_Recap.docx" in hist_vals_after
        app._setting_vars.clear()
        del app
    finally:
        gc.collect()
        root.destroy()
        gc.collect()


