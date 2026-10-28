from __future__ import annotations

import queue
import threading
from pathlib import Path

import pytest

from modules.contracts import Stage, UiCommandType
from modules.tray_ui import StageMonitorTray, create_stage_icon_image


def test_create_stage_icon_image_for_all_stages() -> None:
    """Verifica che ogni Stage produca un'immagine RGBA 64x64 valida."""
    for stage in Stage:
        img = create_stage_icon_image(stage)
        assert img.mode == "RGBA"
        assert img.size == (64, 64)


def test_tray_concurrent_updates_and_menu_labels(tmp_path: Path) -> None:
    """Verifica thread-safety di StageMonitorTray, menu dinamico e command queue."""
    cmd_queue: queue.Queue[UiCommandType] = queue.Queue()
    tray = StageMonitorTray(
        command_queue=cmd_queue,
        output_dir=tmp_path / "output",
        log_path=tmp_path / "logs" / "app.log",
        headless=True,
    )
    tray.start()
    assert tray.ready_event.is_set()

    def _updater(stage: Stage, idx: int) -> None:
        tray.set_stage(stage, detail_text=f"Step {idx}", current=idx, total=10)

    stages = list(Stage)
    threads = [
        threading.Thread(target=_updater, args=(stages[i % len(stages)], i))
        for i in range(20)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    tray.set_stage(Stage.SUMMARIZING_MAP, detail_text="Blocco 2", current=2, total=4)
    snapshot = tray.get_snapshot()
    assert snapshot.stage == Stage.SUMMARIZING_MAP
    assert snapshot.current == 2
    assert snapshot.total == 4
    assert "(2/4)" in tray.format_status_label()

    menu_labels = tray.get_menu_labels()
    assert len(menu_labels) == 10
    assert menu_labels[1] == "Avvia Registrazione Manuale Ora"
    assert menu_labels[2] == "Termina Call e Genera Recap Ora"
    assert menu_labels[3] == "Pausa / Riprendi Registrazione (Ctrl+Alt+P)"
    assert menu_labels[4] == "Apri Cruscotto"
    assert menu_labels[5] == "Impostazioni"
    assert menu_labels[-1] == "Esci"

    tray._on_start_manual()
    tray._on_finalize_now()
    tray._on_toggle_pause()
    tray._on_exit()
    assert cmd_queue.get_nowait() == UiCommandType.START_MANUAL
    assert cmd_queue.get_nowait() == UiCommandType.FINALIZE_NOW
    assert cmd_queue.get_nowait() == UiCommandType.TOGGLE_PAUSE
    assert cmd_queue.get_nowait() == UiCommandType.EXIT

    # Test apertura ultimo file DOCX mancante vs esistente
    assert tray._on_open_last_docx() is False
    dummy_docx = tmp_path / "output" / "test.docx"
    dummy_docx.parent.mkdir(parents=True, exist_ok=True)
    dummy_docx.write_bytes(b"docx")
    tray.set_last_output_file(dummy_docx)
    assert tray.last_output_file == dummy_docx.resolve()

    tray.stop()
    assert tray.stop_event.is_set()


def test_tray_open_helpers_and_notifications(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verifica _open_path_safely, menu callbacks e gestione notifiche."""
    cmd_queue: queue.Queue[UiCommandType] = queue.Queue(maxsize=1)
    output_dir = tmp_path / "output"
    output_dir.mkdir(parents=True, exist_ok=True)
    log_file = tmp_path / "logs" / "app.log"
    log_file.parent.mkdir(parents=True, exist_ok=True)
    log_file.write_text("log content", encoding="utf-8")

    tray = StageMonitorTray(
        command_queue=cmd_queue,
        output_dir=output_dir,
        log_path=log_file,
        headless=True,
    )

    # 1. Verifica _can_start_manual e _can_finalize_now in base allo Stage
    tray.set_stage(Stage.IDLE)
    assert tray._can_start_manual() is True
    assert tray._can_finalize_now() is False
    assert tray._can_toggle_pause() is False

    tray.set_stage(Stage.RECORDING)
    assert tray._can_start_manual() is False
    assert tray._can_finalize_now() is True
    assert tray._can_toggle_pause() is True

    tray.set_stage(Stage.PAUSED)
    assert tray._can_toggle_pause() is True

    # Durante l'elaborazione di un job l'avvio manuale resta disponibile (registrazione concorrente)
    tray.set_recording_status(Stage.IDLE)
    tray.set_queue_status(Stage.SUMMARIZING_MAP, "MAP 1/3", 1, 3, queued=2)
    assert tray._can_start_manual() is True
    assert tray.get_snapshot().stage == Stage.SUMMARIZING_MAP
    assert "Job attivi: 2" in tray.format_status_label()
    # La registrazione ha la precedenza sullo stato dell'elaborazione
    tray.set_recording_status(Stage.RECORDING, "Call in corso")
    assert tray.get_snapshot().stage == Stage.RECORDING
    assert "Elaborazione" in tray.format_status_label()
    tray.set_recording_status(Stage.IDLE)
    tray.set_queue_status(None, queued=0)

    # 2. Simuliamo os.startfile con esito positivo e fallimento OSError
    opened_paths: list[str] = []

    def _mock_startfile(path_str: str) -> None:
        opened_paths.append(path_str)

    monkeypatch.setattr("modules.tray_ui.os.startfile", _mock_startfile, raising=False)

    assert tray._on_open_output() is True
    assert str(output_dir) in opened_paths[-1]

    assert tray._on_open_log() is True
    assert str(log_file) in opened_paths[-1]

    # Coda piena: non deve sollevare eccezioni
    tray._enqueue_command(UiCommandType.EXIT)
    tray._enqueue_command(UiCommandType.START_MANUAL)  # Ignorato su coda piena

    # os.startfile solleva OSError
    def _fail_startfile(_p: str) -> None:
        raise OSError("Access denied")

    monkeypatch.setattr("modules.tray_ui.os.startfile", _fail_startfile, raising=False)
    assert tray._open_path_safely(output_dir, "Test cartella") is False

    # Percorso non esistente
    assert tray._open_path_safely(tmp_path / "non_esiste", "Test inesistente") is False

    # Notifiche vuote o senza messaggio
    tray.notify("Titolo", "")
    tray.notify("", "Messaggio valido")

