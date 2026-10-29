from __future__ import annotations

import json
import threading
from datetime import timedelta
from pathlib import Path

import pytest

from modules.contracts import (
    InvalidStageTransitionError,
    Stage,
    StateValidationError,
    atomic_write_text,
    now_tz,
    validate_stage_transition,
)
from modules.job_registry import JobRegistry, parse_sessions_from_log
from modules.logging_setup import bind_log_session, get_logger, set_log_session_id
from modules.settings import (
    default_settings,
    load_settings,
    restart_required_fields,
    save_settings,
    settings_from_dict,
    settings_to_dict,
    to_summarizer_options,
)
from modules.state_manager import (
    StateManager,
    get_state_manager,
    is_session_locked,
)


# ---------------------------------------------------------------- settings
def test_settings_roundtrip_validation_and_restart_fields(tmp_path: Path) -> None:
    path = tmp_path / "settings.json"
    defaults = load_settings(path)
    assert defaults == default_settings()
    assert defaults.ollama.map_block_duration_sec == 600
    assert defaults.output.glossary == ""

    updated, problems = settings_from_dict(
        {
            "ollama": {"detail_level": "massimo", "num_ctx": "24576", "temperature": "0,3"},
            "whisper": {"model": "large-v3-turbo", "cpu_threads": 99},
            "output": {"user_display_name": " Andrea ", "glossary": "ACR, RS256\nSnam", "generate_recap_email": False},
            "detection": {"use_mic_signal": "false"},
        },
        defaults,
    )
    assert updated.ollama.detail_level == "massimo"
    assert updated.ollama.num_ctx == 24576
    assert updated.ollama.temperature == pytest.approx(0.3)
    assert updated.whisper.model == "large-v3-turbo"
    assert updated.whisper.cpu_threads == defaults.whisper.cpu_threads
    assert updated.output.user_display_name == "Andrea"
    assert updated.output.glossary == "ACR, RS256\nSnam"
    assert updated.output.generate_recap_email is False
    assert updated.detection.use_mic_signal is False
    assert len(problems) == 1 and "Thread CPU Whisper" in problems[0]

    save_settings(updated, path)
    reloaded = load_settings(path)
    assert reloaded == updated
    assert json.loads(path.read_text(encoding="utf-8"))["schema_version"] == 1
    assert settings_to_dict(reloaded)["ollama"]["detail_level"] == "massimo"  # type: ignore[index]

    changed = restart_required_fields(defaults, updated)
    assert "Modello Whisper (CPU)" in changed
    assert all("Livello" not in label for label in changed)

    options = to_summarizer_options(updated)
    assert options.user_name == "Andrea"
    assert options.detail_level == "massimo"
    assert options.glossary.startswith("ACR")


def test_corrupted_settings_fall_back_to_defaults(tmp_path: Path) -> None:
    path = tmp_path / "settings.json"
    path.write_text("{ non json", encoding="utf-8")
    assert load_settings(path) == default_settings()
    # Un file salvato con BOM (Blocco note, PowerShell) resta leggibile
    path.write_text('{"output": {"user_display_name": "Andrea"}}', encoding="utf-8-sig")
    assert load_settings(path).output.user_display_name == "Andrea"


def test_all_settings_fields_have_help_text_and_dashboard_renders_them(tmp_path: Path) -> None:
    from dataclasses import fields
    from modules.settings import AppSettings
    from modules.dashboard_ui import DashboardApp

    settings = AppSettings()
    total_fields = 0
    for group_field in fields(settings):
        group = getattr(settings, group_field.name)
        for item in fields(group):
            total_fields += 1
            help_text = dict(item.metadata).get("help", "")
            assert help_text, f"Campo {group_field.name}.{item.name} privo di help_text"
            assert len(help_text) >= 20, f"help_text troppo breve per {group_field.name}.{item.name}"

    assert total_fields == 33

    try:
        import tkinter as tk
        root = tk.Tk()
    except Exception:
        return
    root.withdraw()
    try:
        registry = JobRegistry(tmp_path / "test.db")
        app = DashboardApp(root, registry, initial_tab="impostazioni")
        root.update()
        rendered_help: list[str] = []
        for child in app.tab_settings.winfo_children():
            if isinstance(child, tk.Canvas):
                for f in child.winfo_children():
                    for box in f.winfo_children():
                        if isinstance(box, tk.ttk.LabelFrame):
                            for w in box.winfo_children():
                                if w.grid_info().get("column") == 2 and isinstance(w, tk.ttk.Label):
                                    txt = w.cget("text")
                                    if txt:
                                        rendered_help.append(txt)
        assert len(rendered_help) == 33
        app._setting_vars.clear()
    finally:
        root.destroy()


# ------------------------------------------------------------ job registry
def test_job_registry_jobs_events_commands_and_history(tmp_path: Path) -> None:
    registry = JobRegistry(tmp_path / "history.db")
    started = now_tz() - timedelta(hours=1)
    registry.upsert_job("S1", title="Call A", status="processing", call_started_at=started, llm_model="m")
    registry.update_stage("S1", Stage.QUEUED.value, "In coda")
    registry.update_stage("S1", Stage.SUMMARIZING_MAP.value, "MAP 1/2", 1, 2)
    registry.upsert_job("S1", tokens_generated=350)
    registry.update_stage("S1", Stage.SUMMARIZING_MAP.value, "MAP 2/2", 2, 2)
    job_mid = registry.get_job("S1")
    assert job_mid is not None and job_mid["tokens_generated"] is None
    registry.upsert_job("S1", tokens_generated=420)
    job = registry.get_job("S1")
    assert job is not None
    assert job["progress_current"] == 2 and job["tokens_generated"] == 420
    events = [e for e in registry.get_events("S1") if e["kind"] == "stage"]
    assert [e["stage"] for e in events] == [Stage.QUEUED.value, Stage.SUMMARIZING_MAP.value]
    assert [stage for stage, _ in registry.stage_durations("S1")] == [
        Stage.QUEUED.value,
        Stage.SUMMARIZING_MAP.value,
    ]

    registry.upsert_job(
        "S2",
        title="Daily",
        status="completed",
        call_started_at=started - timedelta(days=2),
        processing_ended_at=started - timedelta(days=2),
    )
    assert {row["session_id"] for row in registry.list_active_jobs()} == {"S1"}
    registry.upsert_job("S3", title="Appena finita", status="completed", processing_ended_at=now_tz())
    assert {row["session_id"] for row in registry.list_active_jobs()} == {"S1", "S3"}
    registry.delete_job("S3")
    assert [row["session_id"] for row in registry.list_history(day=started.strftime("%Y-%m-%d"))] == ["S1"]
    assert [row["session_id"] for row in registry.list_history(status="completed")] == ["S2"]
    assert [row["session_id"] for row in registry.list_history(text="dail")] == ["S2"]

    registry.enqueue_command("start_manual")
    registry.enqueue_command("retry_job", {"session_id": "S1"})
    assert registry.fetch_pending_commands() == [("start_manual", {}), ("retry_job", {"session_id": "S1"})]
    assert registry.fetch_pending_commands() == []

    registry.set_status({"watcher_stage": "recording", "queue_length": 2})
    status = registry.get_status()
    assert status["watcher_stage"] == "recording" and status["queue_length"] == 2

    csv_path = JobRegistry.export_csv(registry.list_history(), tmp_path / "out.csv")
    assert "Call A" in csv_path.read_text(encoding="utf-8-sig")

    registry.upsert_job("OLD", status="completed", call_started_at=now_tz() - timedelta(days=500))
    assert registry.purge_older_than(365) == 1
    assert registry.get_job("OLD") is None


def test_job_registry_concurrent_writers(tmp_path: Path) -> None:
    registry = JobRegistry(tmp_path / "history.db")
    second_handle = JobRegistry(tmp_path / "history.db")

    def _writer(reg: JobRegistry, prefix: str) -> None:
        for i in range(30):
            reg.update_stage(f"{prefix}{i % 3}", Stage.SUMMARIZING_MAP.value, f"step {i}", i, 30)

    threads = [
        threading.Thread(target=_writer, args=(registry, "A")),
        threading.Thread(target=_writer, args=(second_handle, "B")),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(registry.list_history()) == 6


def test_history_import_from_log(tmp_path: Path) -> None:
    log_path = tmp_path / "app.log"
    log_path.write_text(
        "2026-10-26 09:18:26,000 | INFO | MainThread | teams_meeting_scribe.state_manager | session=20261026_091826_1293e287 | "
        "Sessione inizializzata: session_id=20261026_091826_1293e287 mode=auto stage=recording\n"
        "2026-10-26 09:45:00,000 | INFO | MainThread | teams_meeting_scribe | session=20261026_091826_1293e287 | "
        "Documento Word generato e verificato: 2026-10-26_09-18_Riunione Teams_Recap.docx\n"
        "2026-10-26 09:31:03,000 | INFO | MainThread | teams_meeting_scribe.state_manager | session=20261026_093103_e8d72d3f | "
        "Sessione inizializzata: session_id=20261026_093103_e8d72d3f mode=auto stage=recording\n"
        "2026-10-26 09:58:00,000 | ERROR | MainThread | teams_meeting_scribe.state_manager | session=20261026_093103_e8d72d3f | "
        "Errore registrato nello stage summarizing_map (OllamaError): timeout\n"
        "2026-10-24 00:45:48,418 | INFO | MainThread | teams_meeting_scribe.state_manager | session=x | "
        "Sessione inizializzata: session_id=20261024_004548_03477107 mode=dry_run stage=idle\n",
        encoding="utf-8",
    )
    sessions = parse_sessions_from_log(log_path)
    assert set(sessions) == {"20261026_091826_1293e287", "20261026_093103_e8d72d3f"}
    assert sessions["20261026_091826_1293e287"]["status"] == "completed"
    assert sessions["20261026_093103_e8d72d3f"]["status"] == "failed"

    output_dir = tmp_path / "output"
    day_docx = output_dir / "2026-10-26" / "2026-10-26_09-18_Riunione Teams_Recap.docx"
    day_docx.parent.mkdir(parents=True)
    day_docx.write_bytes(b"x")
    registry = JobRegistry(tmp_path / "history.db")
    assert registry.import_history_from_log(log_path, output_dir=output_dir) == 2
    assert registry.import_history_from_log(log_path, output_dir=output_dir) == 0
    job = registry.get_job("20261026_091826_1293e287")
    assert job is not None and job["docx_path"] == str(day_docx.resolve())


# ----------------------------------------------------------- state manager
def test_state_manager_registry_lock_and_closed_session(
    tmp_path: Path,
    sample_session_context: object,
    valid_map_markdown: str,
) -> None:
    from modules.contracts import SessionContext

    assert isinstance(sample_session_context, SessionContext)
    temp_root = tmp_path / "temp"
    sm = get_state_manager(sample_session_context.session_dir, temp_root=temp_root, output_root=tmp_path / "output")
    assert get_state_manager(sample_session_context.session_dir) is sm
    sm.initialize(sample_session_context)

    sm.acquire_owner_lock()
    assert is_session_locked(sample_session_context.session_dir) is True
    other = StateManager(sample_session_context.session_dir, temp_root=temp_root)
    with pytest.raises(Exception, match="già in uso"):
        other.acquire_owner_lock()
    sm.release_owner_lock()
    assert is_session_locked(sample_session_context.session_dir) is False

    # Dopo il cleanup una scrittura tardiva non deve ricreare la cartella di sessione
    docx = tmp_path / "output" / "2026-10-23" / "verbale.docx"
    docx.parent.mkdir(parents=True)
    docx.write_bytes(b"0" * 5000)
    sm.acquire_owner_lock()
    sm.cleanup_session_files(docx)
    assert not sample_session_context.session_dir.exists()
    with pytest.raises(StateValidationError):
        sm.save(sample_session_context)
    assert not sample_session_context.session_dir.exists()
    assert get_state_manager(sample_session_context.session_dir, temp_root=temp_root) is not sm


def test_validate_resume_detects_undeclared_open_chunk(tmp_path: Path, sample_session_context: object) -> None:
    """B14: i raw del chunk aperto al momento del crash vengono recuperati anche se non dichiarati."""
    from modules.audio_utils import create_silent_wav
    from modules.contracts import SessionContext

    assert isinstance(sample_session_context, SessionContext)
    sm = StateManager(sample_session_context.session_dir, temp_root=tmp_path / "temp")
    sm.initialize(sample_session_context)
    audio = sample_session_context.session_dir / "audio"
    transcripts = sample_session_context.session_dir / "transcripts"
    atomic_write_text(
        transcripts / "chunk_000.txt",
        "# Chunk 000\n# Intervallo: 00:00:00 - 00:15:00\n# Sessione: x\n\n[00:00:01] [IO]: ciao\n",
    )
    create_silent_wav(audio / "chunk_001_io_16k.wav", duration_sec=0.2)
    create_silent_wav(audio / "chunk_001_altri_16k.wav", duration_sec=0.2)
    create_silent_wav(audio / "chunk_002_io_raw.wav", duration_sec=0.2)
    create_silent_wav(audio / "chunk_002_altri_raw.wav", duration_sec=0.2)

    def _declare(c: SessionContext) -> None:
        c.recorded_chunks = [0]
        c.normalized_chunks = [0]
        c.transcribed_chunks = [0]

    sm.mutate(_declare)
    context = sm.validate_resume_artifacts()
    assert context.recorded_chunks == [0, 1, 2]
    assert context.normalized_chunks == [0, 1]
    assert context.stage == Stage.FINALIZING_AUDIO


def test_record_error_without_affecting_stage(tmp_path: Path, sample_session_context: object) -> None:
    from modules.contracts import SessionContext

    assert isinstance(sample_session_context, SessionContext)
    sample_session_context.stage = Stage.RECORDING
    sm = StateManager(sample_session_context.session_dir, temp_root=tmp_path / "temp")
    sm.initialize(sample_session_context)
    sm.record_error(Stage.TRANSCRIBING_FINAL, RuntimeError("chunk corrotto"), affects_stage=False)
    context = sm.load()
    assert context.stage == Stage.RECORDING
    assert context.errors[-1]["affects_stage"] is False


# ---------------------------------------------------------------- contratti
def test_queued_transitions() -> None:
    validate_stage_transition(Stage.FINALIZING_AUDIO, Stage.QUEUED)
    validate_stage_transition(Stage.QUEUED, Stage.TRANSCRIBING_FINAL)
    validate_stage_transition(Stage.QUEUED, Stage.SUMMARIZING_MAP)
    validate_stage_transition(Stage.TRANSCRIBING_FINAL, Stage.SUMMARIZING_MAP)
    validate_stage_transition(Stage.ERROR, Stage.QUEUED)
    with pytest.raises(InvalidStageTransitionError):
        validate_stage_transition(Stage.RECORDING, Stage.QUEUED)
    with pytest.raises(InvalidStageTransitionError):
        validate_stage_transition(Stage.SHUTTING_DOWN, Stage.QUEUED)


def test_atomic_write_uses_unique_temp_files_under_concurrency(tmp_path: Path) -> None:
    """B20: scrittori concorrenti sullo stesso file non collidono sul nome temporaneo."""
    target = tmp_path / "state.json"
    errors: list[BaseException] = []

    def _writer(n: int) -> None:
        try:
            for i in range(25):
                atomic_write_text(target, f"{n}-{i}")
        except BaseException as exc:  # pragma: no cover - riportato dall'assert
            errors.append(exc)

    threads = [threading.Thread(target=_writer, args=(n,)) for n in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert errors == []
    assert list(tmp_path.glob("*.tmp")) == []
    assert target.read_text(encoding="utf-8").count("-") == 1


# ------------------------------------------------------------------ logging
def test_log_session_id_is_per_thread(_isolated_logging: Path) -> None:
    """B11: con sessioni concorrenti ogni thread scrive il proprio session id."""
    logger = get_logger("test_threads")
    set_log_session_id("GLOBALE")
    barrier = threading.Barrier(2)

    def _worker(session_id: str) -> None:
        with bind_log_session(session_id):
            barrier.wait()
            logger.info("messaggio da %s", session_id)

    threads = [threading.Thread(target=_worker, args=(sid,)) for sid in ("SESS_A", "SESS_B")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    logger.info("messaggio principale")
    for handler in get_logger().handlers:
        handler.flush()
    text = (_isolated_logging / "app.log").read_text(encoding="utf-8")
    assert "session=SESS_A | messaggio da SESS_A" in text
    assert "session=SESS_B | messaggio da SESS_B" in text
    assert "session=GLOBALE | messaggio principale" in text


def test_incognito_mode_tray_and_notifications(tmp_path: Path) -> None:
    """Verifica impostazione incognito mode e cancellazione ultimo docx su StageMonitorTray."""
    from modules.tray_ui import StageMonitorTray

    tray = StageMonitorTray(output_dir=tmp_path / "output", headless=True)
    assert tray.is_incognito_mode() is False

    tray.set_incognito_mode(True)
    assert tray.is_incognito_mode() is True

    # Test clear_last_output_file_if_matches
    docx_file = tmp_path / "output" / "test.docx"
    docx_file.parent.mkdir(parents=True, exist_ok=True)
    docx_file.write_bytes(b"test")
    tray.set_last_output_file(docx_file)
    assert tray.last_output_file == docx_file.resolve()

    tray.clear_last_output_file_if_matches(tmp_path / "output" / "other.docx")
    assert tray.last_output_file == docx_file.resolve()

    tray.clear_last_output_file_if_matches(docx_file)
    assert tray.last_output_file is None


def test_purge_session_files_and_delete_job(tmp_path: Path) -> None:
    """Verifica purge_session_files e delete_job di orchestrator."""
    from modules.job_registry import JobRegistry
    from modules.orchestrator import MeetingScribeApplication, purge_session_files

    temp_root = tmp_path / "temp"
    out_root = tmp_path / "output"
    sess_dir = temp_root / "session_test123"
    sess_dir.mkdir(parents=True, exist_ok=True)
    (sess_dir / "state.json").write_text("{}", encoding="utf-8")
    docx_file = out_root / "2026-10-27" / "test123_Recap.docx"
    docx_file.parent.mkdir(parents=True, exist_ok=True)
    docx_file.write_bytes(b"word content")

    t_del, d_del = purge_session_files("test123", session_dir=sess_dir, docx_path=docx_file, temp_root=temp_root)
    assert t_del is True
    assert d_del is True
    assert not sess_dir.exists()
    assert not docx_file.exists()

    # Test delete_job orchestrator
    reg = JobRegistry(tmp_path / "db.sqlite")
    reg.upsert_job("sess_del", title="Meeting To Delete")
    app = MeetingScribeApplication(
        headless=True,
        output_dir=out_root,
        temp_dir=temp_root,
        consent_path=tmp_path / "consent.json",
        registry=reg,
    )
    app.delete_job("sess_del")
    assert reg.is_deleted("sess_del") is True
    assert reg.get_job("sess_del") is None
