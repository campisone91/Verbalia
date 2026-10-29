from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import config
from modules.contracts import Stage
from modules.job_registry import JobRegistry
from modules.orchestrator import purge_session_files
from modules.settings import (
    AppSettings,
    DetectionSettings,
    apply_to_config,
    load_settings,
    save_settings,
)
from modules.tray_ui import StageMonitorTray


def test_incognito_mode_suppresses_tray_notifications(tmp_path: Path) -> None:
    tray = StageMonitorTray(output_dir=tmp_path, log_dir=tmp_path)
    fake_icon = MagicMock()
    tray._icon = fake_icon

    tray.set_incognito_mode(True)
    assert tray.is_incognito_mode() is True
    tray.notify("Titolo", "Messaggio che non deve apparire")
    fake_icon.notify.assert_not_called()

    tray.set_incognito_mode(False)
    config.INCOGNITO_MODE = False
    tray.notify("Titolo", "Messaggio visibile")
    fake_icon.notify.assert_called_once_with("Messaggio visibile", "Titolo")


def test_settings_persist_incognito_mode(tmp_path: Path) -> None:
    settings_file = tmp_path / "settings.json"
    s = AppSettings(detection=DetectionSettings(incognito_mode=True))
    save_settings(s, settings_file)

    loaded = load_settings(settings_file)
    assert loaded.detection.incognito_mode is True
    apply_to_config(loaded)
    assert config.INCOGNITO_MODE is True

    apply_to_config(AppSettings())


def test_delete_job_tombstone_prevents_log_reimport(tmp_path: Path) -> None:
    db_path = tmp_path / "history.db"
    registry = JobRegistry(db_path=db_path)

    sid = "20261027_100000_deadbeef"
    registry.upsert_job(sid, title="Riunione Test", status="completed", mode="auto")
    registry.add_event(sid, "stage", "completed", stage=Stage.COMPLETED.value)

    deleted_row = registry.delete_job(sid)
    assert deleted_row is not None
    assert deleted_row["session_id"] == sid
    assert registry.get_job(sid) is None
    assert registry.get_events(sid) == []
    assert registry.is_deleted(sid) is True

    log_file = tmp_path / "app.log"
    log_file.write_text(
        f"2026-10-27 10:00:00,000 | INFO | main | MainThread | session={sid} | "
        f"Sessione inizializzata: session_id={sid} mode=auto\n",
        encoding="utf-8",
    )
    imported = registry.import_history_from_log(log_path=log_file, output_dir=tmp_path)
    assert imported == 0
    assert registry.get_job(sid) is None


def test_purge_session_files_removes_temp_and_docx(tmp_path: Path) -> None:
    sid = "20261027_110000_cafebabe"
    session_dir = tmp_path / f"session_{sid}"
    session_dir.mkdir(parents=True)
    (session_dir / "state.json").write_text("{}", encoding="utf-8")
    (session_dir / ".owner.lock").write_text("123", encoding="utf-8")

    day_dir = tmp_path / "output" / "2026-10-27"
    day_dir.mkdir(parents=True)
    docx_file = day_dir / "2026-10-27_11-00_Test_Recap.docx"
    docx_file.write_bytes(b"PK\x03\x04dummy")

    temp_deleted, docx_deleted = purge_session_files(
        session_id=sid,
        session_dir=session_dir,
        docx_path=docx_file,
        temp_root=tmp_path,
    )
    assert temp_deleted is True
    assert docx_deleted is True
    assert not session_dir.exists()
    assert not docx_file.exists()
    assert not day_dir.exists()
