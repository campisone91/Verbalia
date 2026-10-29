from __future__ import annotations

import json
import os
import sys
import threading
from collections.abc import Callable, Mapping
from dataclasses import fields
from dataclasses import replace as _dc_replace
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from PySide6 import QtCore, QtGui, QtWidgets
from PySide6.QtCore import Qt, QTimer, Signal

from config import (
    APP_ICON_PATH,
    APP_NAME,
    APP_VERSION,
    EXIT_MISSING_DEPENDENCY,
    EXIT_SUCCESS,
    PROJECT_ROOT,
)
from modules.contracts import STAGE_LABELS_IT, Stage, UiCommandType, now_tz
from modules.job_registry import JOB_STATUS_LABELS_IT, JobRegistry, parse_iso
from modules.logging_setup import get_logger
from modules.settings import (
    SETTINGS_GROUP_LABELS,
    AppSettings,
    apply_to_config,
    default_settings,
    load_settings,
    resolve_output_dir,
    restart_required_fields,
    save_settings,
    settings_from_dict,
    settings_to_dict,
)
from modules.single_instance import DASHBOARD_MUTEX_NAME, SingleInstance, focus_window_by_title
from modules.updater import (
    UpdateInfo,
    UpdateResult,
    check_for_updates,
    download_and_apply_update,
    extract_changelog_section,
    is_newer_version,
    spawn_restarted_processes,
    stop_background_instance,
)

logger = get_logger("dashboard")

DASHBOARD_TITLE: str = f"{APP_NAME} - Cruscotto"
REFRESH_INTERVAL_MS: int = 1000
HEARTBEAT_STALE_SEC: float = 10.0

PIPELINE_STEPS: tuple[str, ...] = (
    "Registrazione",
    "Normalizzazione",
    "Trascrizione",
    "MAP",
    "REDUCE",
    "DOCX",
    "Pulizia",
)
_STAGE_STEP_INDEX: dict[str, int] = {
    Stage.RECORDING.value: 0,
    Stage.RECORDING_AND_TRANSCRIBING.value: 0,
    Stage.GRACE_PERIOD.value: 0,
    Stage.PAUSED.value: 0,
    Stage.FINALIZING_AUDIO.value: 1,
    Stage.QUEUED.value: 1,
    Stage.TRANSCRIBING_FINAL.value: 2,
    Stage.RELEASING_WHISPER.value: 2,
    Stage.SUMMARIZING_MAP.value: 3,
    Stage.SUMMARIZING_REDUCE.value: 4,
    Stage.BUILDING_DOCX.value: 5,
    Stage.VERIFYING_DOCX.value: 5,
    Stage.CLEANING_UP.value: 6,
    Stage.COMPLETED.value: 7,
}
HISTORY_STATUS_FILTERS: tuple[tuple[str, str | None], ...] = (
    ("Tutti", None),
    ("Completato", "completed"),
    ("Errore", "failed"),
    ("Fermo (serve Riprova)", "parked"),
    ("Interrotto", "interrupted"),
    ("In coda", "queued"),
    ("In elaborazione", "processing"),
    ("In registrazione", "recording"),
    ("In pausa", "paused"),
)


# ==============================================================================
# Funzioni pure di formattazione e calcolo avanzamento
# ==============================================================================
def _stage_label(stage: object) -> str:
    if isinstance(stage, str):
        if stage.lower() == "paused":
            return "⏸ In Pausa"
        try:
            return STAGE_LABELS_IT.get(Stage(stage), stage)
        except ValueError:
            return stage
    return ""


def format_duration(seconds: float | None) -> str:
    """Durata leggibile: 1h 05m, 12m 30s, 45s."""
    if seconds is None or seconds < 0:
        return "-"
    total = int(seconds)
    hours, rem = divmod(total, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours}h {minutes:02d}m"
    if minutes:
        return f"{minutes}m {secs:02d}s"
    return f"{secs}s"


def _elapsed(start: object, end: object = None) -> float | None:
    start_dt = parse_iso(start)
    if start_dt is None:
        return None
    end_dt = parse_iso(end) or now_tz()
    return max(0.0, (end_dt - start_dt).total_seconds())


def compute_job_progress_percent(job: Mapping[str, object]) -> int | None:
    """Calcola la percentuale di avanzamento (0..100) dell'elaborazione della transazione."""
    status = str(job.get("status") or "").strip().lower()
    stage = str(job.get("stage") or "").strip().lower()
    if status == "completed" or stage == Stage.COMPLETED.value:
        return 100
    if status in ("recording", "paused") or stage in (
        Stage.RECORDING.value,
        Stage.RECORDING_AND_TRANSCRIBING.value,
        Stage.PAUSED.value,
        Stage.GRACE_PERIOD.value,
    ):
        return None
    if stage == Stage.QUEUED.value or (not stage and status == "queued"):
        return 0
    if stage == Stage.FINALIZING_AUDIO.value:
        return 2

    current = job.get("progress_current")
    total = job.get("progress_total")
    tokens = job.get("tokens_generated")
    has_steps = (
        isinstance(current, int)
        and not isinstance(current, bool)
        and isinstance(total, int)
        and not isinstance(total, bool)
        and total > 0
    )
    tokens_val = tokens if isinstance(tokens, int) and not isinstance(tokens, bool) and tokens > 0 else 0

    if stage == Stage.TRANSCRIBING_FINAL.value:
        if has_steps:
            assert isinstance(current, int) and isinstance(total, int)
            frac = min(1.0, max(0.0, current / total))
            return int(round(5 + 40 * frac))
        return 10

    if stage == Stage.RELEASING_WHISPER.value:
        return 45

    if stage == Stage.SUMMARIZING_MAP.value:
        if has_steps:
            assert isinstance(current, int) and isinstance(total, int)
            completed_blocks = max(0, current - 1)
            in_block = min(0.9, float(tokens_val) / 600.0) if tokens_val > 0 else 0.1
            frac = min(1.0, max(0.0, (completed_blocks + in_block) / total))
        else:
            frac = min(0.9, float(tokens_val) / 600.0) if tokens_val > 0 else 0.1
        return int(round(45 + 30 * frac))

    if stage == Stage.SUMMARIZING_REDUCE.value:
        if has_steps:
            assert isinstance(current, int) and isinstance(total, int)
            completed_steps = max(0, current - 1)
            in_step = min(0.9, float(tokens_val) / 800.0) if tokens_val > 0 else 0.1
            frac = min(1.0, max(0.0, (completed_steps + in_step) / total))
        else:
            frac = min(0.9, float(tokens_val) / 1000.0) if tokens_val > 0 else 0.2
        return int(round(75 + 17 * frac))

    if stage == Stage.BUILDING_DOCX.value:
        return 94
    if stage == Stage.VERIFYING_DOCX.value:
        return 97
    if stage == Stage.CLEANING_UP.value:
        return 99

    return None


def describe_progress(job: Mapping[str, object]) -> str:
    """Testo di avanzamento del job: percentuale, MAP/REDUCE/Chunk corrente e token generati."""
    parts: list[str] = []
    pct = compute_job_progress_percent(job)
    if pct is not None:
        parts.append(f"{pct}%")
    current, total = job.get("progress_current"), job.get("progress_total")
    stage = str(job.get("stage") or "")
    if (
        isinstance(current, int)
        and not isinstance(current, bool)
        and isinstance(total, int)
        and not isinstance(total, bool)
        and total > 0
        and stage != Stage.COMPLETED.value
        and str(job.get("status") or "") != "completed"
    ):
        if stage == Stage.SUMMARIZING_MAP.value:
            prefix = "MAP "
        elif stage == Stage.SUMMARIZING_REDUCE.value:
            prefix = "REDUCE "
        elif stage in (
            Stage.TRANSCRIBING_FINAL.value,
            Stage.RECORDING.value,
            Stage.RECORDING_AND_TRANSCRIBING.value,
        ):
            prefix = "Chunk "
        else:
            prefix = ""
        parts.append(f"{prefix}{current}/{total}")
    tokens = job.get("tokens_generated")
    if isinstance(tokens, int) and not isinstance(tokens, bool) and tokens > 0:
        parts.append(f"{tokens} token")
    return " · ".join(parts)


def pipeline_marks(stage: str | None, status: str | None) -> list[str]:
    """Simboli ✓/●/○ (✗ in errore) per i passi della pipeline dato lo stage corrente."""
    index = _STAGE_STEP_INDEX.get(stage or "", -1)
    if status == "completed":
        index = len(PIPELINE_STEPS)
    marks: list[str] = []
    for step in range(len(PIPELINE_STEPS)):
        if step < index:
            marks.append("✓")
        elif step == index:
            marks.append("✗" if status in ("failed", "parked") else "●")
        else:
            marks.append("○")
    return marks


def _open_path(path: Path) -> bool:
    startfile = getattr(os, "startfile", None)
    if not callable(startfile) or not path.exists():
        return False
    try:
        startfile(str(path))
        return True
    except OSError as exc:
        logger.warning("Impossibile aprire %s: %s", path, exc)
        return False


# ==============================================================================
# Fogli di stile QSS Windows 11 Fluent (Light & Dark)
# ==============================================================================
FLUENT_LIGHT_QSS = """
QMainWindow, QDialog {
    background-color: #F3F4F6;
    color: #0F172A;
    font-family: "Segoe UI Variable Text", "Segoe UI", sans-serif;
    font-size: 11px;
}
QWidget {
    color: #0F172A;
    font-family: "Segoe UI Variable Text", "Segoe UI", sans-serif;
    font-size: 11px;
}
QGroupBox {
    background-color: #FFFFFF;
    border: 1px solid #E2E8F0;
    border-radius: 6px;
    margin-top: 17px;
    padding: 8px 8px 6px 8px;
    font-weight: 600;
    color: #1E293B;
}
QGroupBox::title {
    subcontrol-origin: margin;
    subcontrol-position: top left;
    left: 6px;
    top: 1px;
    padding: 1px 6px;
    border-radius: 4px;
    color: #2563EB;
    background-color: transparent;
}
QTabWidget::pane {
    background-color: #F8FAFC;
    border: 1px solid #E2E8F0;
    border-radius: 6px;
    top: -1px;
}
QTabBar::tab {
    background-color: transparent;
    color: #475569;
    padding: 5px 14px;
    margin-right: 3px;
    border-top-left-radius: 5px;
    border-top-right-radius: 5px;
    font-weight: 600;
    border-bottom: 2px solid transparent;
}
QTabBar::tab:hover {
    background-color: #E2E8F0;
    color: #0F172A;
}
QTabBar::tab:selected {
    background-color: #FFFFFF;
    color: #2563EB;
    border: 1px solid #E2E8F0;
    border-bottom: 2px solid #2563EB;
}
QPushButton {
    background-color: #FFFFFF;
    color: #1E293B;
    border: 1px solid #CBD5E1;
    border-radius: 5px;
    padding: 3px 9px;
    font-weight: 500;
}
QPushButton:hover {
    background-color: #F1F5F9;
    border-color: #94A3B8;
}
QPushButton:pressed {
    background-color: #E2E8F0;
}
QPushButton:disabled {
    background-color: #F8FAFC;
    color: #94A3B8;
    border-color: #E2E8F0;
}
QPushButton[btnRole="primary"] {
    background-color: #2563EB;
    color: #FFFFFF;
    border: 1px solid #1D4ED8;
    font-weight: 600;
}
QPushButton[btnRole="primary"]:hover {
    background-color: #1D4ED8;
}
QPushButton[btnRole="primary"]:disabled {
    background-color: #93C5FD;
    color: #EFF6FF;
    border-color: #93C5FD;
}
QPushButton[btnRole="danger"] {
    background-color: #FFFFFF;
    color: #DC2626;
    border: 1px solid #FCA5A5;
}
QPushButton[btnRole="danger"]:hover {
    background-color: #FEF2F2;
    border-color: #EF4444;
}
QPushButton[btnRole="danger"]:disabled {
    background-color: #F8FAFC;
    color: #CBD5E1;
    border-color: #E2E8F0;
}
QTreeWidget, QListWidget {
    background-color: #FFFFFF;
    alternate-background-color: #F8FAFC;
    border: 1px solid #CBD5E1;
    border-radius: 5px;
    outline: none;
    selection-background-color: #DBEAFE;
    selection-color: #1E3A8A;
}
QTreeWidget::item, QListWidget::item {
    padding: 2px 4px;
    border-bottom: 1px solid #F1F5F9;
}
QListWidget#eventsList {
    font-family: "Cascadia Mono", "Consolas", monospace;
    font-size: 10px;
}
QListWidget#eventsList::item {
    padding: 1px 4px;
    border-bottom: none;
}
QTreeWidget::item:selected, QListWidget::item:selected {
    background-color: #DBEAFE;
    color: #1E3A8A;
}
QHeaderView::section {
    background-color: #F1F5F9;
    color: #334155;
    font-weight: 600;
    padding: 4px 6px;
    border: none;
    border-right: 1px solid #E2E8F0;
    border-bottom: 1px solid #CBD5E1;
}
QLineEdit, QComboBox, QPlainTextEdit {
    background-color: #FFFFFF;
    color: #0F172A;
    border: 1px solid #CBD5E1;
    border-radius: 5px;
    padding: 3px 6px;
    selection-background-color: #2563EB;
    selection-color: #FFFFFF;
}
QLineEdit:focus, QComboBox:focus, QPlainTextEdit:focus {
    border: 1px solid #2563EB;
}
QProgressBar {
    background-color: #E2E8F0;
    border: none;
    border-radius: 4px;
    height: 8px;
    text-align: center;
    color: transparent;
}
QProgressBar::chunk {
    background-color: #2563EB;
    border-radius: 4px;
}
QScrollArea, QWidget#settingsViewport, QWidget#settingsScrollContent {
    border: none;
    background-color: #F8FAFC;
}
QSplitter::handle:vertical {
    background-color: #E2E8F0;
    height: 5px;
    margin: 2px 40px;
    border-radius: 2px;
}
QSplitter::handle:vertical:hover {
    background-color: #2563EB;
}
QLabel[labelRole="warn"] {
    color: #B45309;
    font-weight: 600;
}
QLabel[labelRole="help"] {
    color: #64748B;
    font-size: 10.5px;
}
QLabel[labelRole="step"] {
    background-color: #F1F5F9;
    border: 1px solid #E2E8F0;
    border-radius: 4px;
    padding: 2px 6px;
    font-size: 10.5px;
    font-weight: 500;
}
QLabel[labelRole="version"] {
    background-color: #EFF6FF;
    color: #1D4ED8;
    border: 1px solid #BFDBFE;
    border-radius: 4px;
    padding: 1px 7px;
    font-weight: 600;
}
QFrame#updateBanner {
    background-color: #EFF6FF;
    border: 1px solid #93C5FD;
    border-radius: 6px;
    padding: 4px 8px;
}
QLabel[labelRole="updateTitle"] {
    color: #1E3A8A;
    font-weight: 600;
}
QMenu {
    background-color: #FFFFFF;
    color: #0F172A;
    border: 1px solid #CBD5E1;
    border-radius: 6px;
    padding: 3px;
}
QMenu::item {
    padding: 4px 16px;
    border-radius: 4px;
}
QMenu::item:selected {
    background-color: #DBEAFE;
    color: #1E3A8A;
}
"""

FLUENT_DARK_QSS = """
QMainWindow, QDialog {
    background-color: #0F172A;
    color: #F8FAFC;
    font-family: "Segoe UI Variable Text", "Segoe UI", sans-serif;
    font-size: 11px;
}
QWidget {
    color: #F8FAFC;
    font-family: "Segoe UI Variable Text", "Segoe UI", sans-serif;
    font-size: 11px;
}
QGroupBox {
    background-color: #1E293B;
    border: 1px solid #334155;
    border-radius: 6px;
    margin-top: 17px;
    padding: 8px 8px 6px 8px;
    font-weight: 600;
    color: #F1F5F9;
}
QGroupBox::title {
    subcontrol-origin: margin;
    subcontrol-position: top left;
    left: 6px;
    top: 1px;
    padding: 1px 6px;
    border-radius: 4px;
    color: #60A5FA;
    background-color: transparent;
}
QTabWidget::pane {
    background-color: #0F172A;
    border: 1px solid #334155;
    border-radius: 6px;
    top: -1px;
}
QTabBar::tab {
    background-color: transparent;
    color: #94A3B8;
    padding: 5px 14px;
    margin-right: 3px;
    border-top-left-radius: 5px;
    border-top-right-radius: 5px;
    font-weight: 600;
    border-bottom: 2px solid transparent;
}
QTabBar::tab:hover {
    background-color: #1E293B;
    color: #F8FAFC;
}
QTabBar::tab:selected {
    background-color: #1E293B;
    color: #60A5FA;
    border: 1px solid #334155;
    border-bottom: 2px solid #3B82F6;
}
QPushButton {
    background-color: #1E293B;
    color: #F1F5F9;
    border: 1px solid #475569;
    border-radius: 5px;
    padding: 3px 9px;
    font-weight: 500;
}
QPushButton:hover {
    background-color: #334155;
    border-color: #64748B;
}
QPushButton:pressed {
    background-color: #0F172A;
}
QPushButton:disabled {
    background-color: #0F172A;
    color: #475569;
    border-color: #1E293B;
}
QPushButton[btnRole="primary"] {
    background-color: #2563EB;
    color: #FFFFFF;
    border: 1px solid #3B82F6;
    font-weight: 600;
}
QPushButton[btnRole="primary"]:hover {
    background-color: #3B82F6;
}
QPushButton[btnRole="primary"]:disabled {
    background-color: #1E3A8A;
    color: #64748B;
    border-color: #1E3A8A;
}
QPushButton[btnRole="danger"] {
    background-color: #1E293B;
    color: #F87171;
    border: 1px solid #7F1D1D;
}
QPushButton[btnRole="danger"]:hover {
    background-color: #450A0A;
    border-color: #EF4444;
}
QPushButton[btnRole="danger"]:disabled {
    background-color: #0F172A;
    color: #475569;
    border-color: #1E293B;
}
QTreeWidget, QListWidget {
    background-color: #1E293B;
    alternate-background-color: #172033;
    border: 1px solid #334155;
    border-radius: 5px;
    outline: none;
    selection-background-color: #1E3A8A;
    selection-color: #EFF6FF;
}
QTreeWidget::item, QListWidget::item {
    padding: 2px 4px;
    border-bottom: 1px solid #1E293B;
}
QListWidget#eventsList {
    font-family: "Cascadia Mono", "Consolas", monospace;
    font-size: 10px;
}
QListWidget#eventsList::item {
    padding: 1px 4px;
    border-bottom: none;
}
QTreeWidget::item:selected, QListWidget::item:selected {
    background-color: #1E3A8A;
    color: #EFF6FF;
}
QHeaderView::section {
    background-color: #0F172A;
    color: #CBD5E1;
    font-weight: 600;
    padding: 4px 6px;
    border: none;
    border-right: 1px solid #1E293B;
    border-bottom: 1px solid #334155;
}
QLineEdit, QComboBox, QPlainTextEdit {
    background-color: #0F172A;
    color: #F8FAFC;
    border: 1px solid #475569;
    border-radius: 5px;
    padding: 3px 6px;
    selection-background-color: #3B82F6;
    selection-color: #FFFFFF;
}
QLineEdit:focus, QComboBox:focus, QPlainTextEdit:focus {
    border: 1px solid #60A5FA;
}
QProgressBar {
    background-color: #334155;
    border: none;
    border-radius: 4px;
    height: 8px;
    text-align: center;
    color: transparent;
}
QProgressBar::chunk {
    background-color: #3B82F6;
    border-radius: 4px;
}
QScrollArea, QWidget#settingsViewport, QWidget#settingsScrollContent {
    border: none;
    background-color: #0F172A;
}
QSplitter::handle:vertical {
    background-color: #334155;
    height: 5px;
    margin: 2px 40px;
    border-radius: 2px;
}
QSplitter::handle:vertical:hover {
    background-color: #60A5FA;
}
QLabel[labelRole="warn"] {
    color: #FBBF24;
    font-weight: 600;
}
QLabel[labelRole="help"] {
    color: #94A3B8;
    font-size: 10.5px;
}
QLabel[labelRole="step"] {
    background-color: #0F172A;
    border: 1px solid #334155;
    border-radius: 4px;
    padding: 2px 6px;
    font-size: 10.5px;
    font-weight: 500;
}
QLabel[labelRole="version"] {
    background-color: #1E3A8A;
    color: #93C5FD;
    border: 1px solid #3B82F6;
    border-radius: 4px;
    padding: 1px 7px;
    font-weight: 600;
}
QFrame#updateBanner {
    background-color: #1E293B;
    border: 1px solid #3B82F6;
    border-radius: 6px;
    padding: 4px 8px;
}
QLabel[labelRole="updateTitle"] {
    color: #93C5FD;
    font-weight: 600;
}
QMenu {
    background-color: #1E293B;
    color: #F8FAFC;
    border: 1px solid #475569;
    border-radius: 6px;
    padding: 3px;
}
QMenu::item {
    padding: 4px 16px;
    border-radius: 4px;
}
QMenu::item:selected {
    background-color: #1E3A8A;
    color: #EFF6FF;
}
"""


# ==============================================================================
# Wrapper e Componenti Qt con API compatibile per Binding Reattivi e Test
# ==============================================================================
class _BackgroundBridge(QtCore.QObject):
    """Consegna in modo thread-safe i risultati dei thread di background sul main thread Qt."""

    completed = Signal(object, object)
    progress = Signal(object, int, str)

    def __init__(self, parent: QtCore.QObject | None = None) -> None:
        super().__init__(parent)
        self.completed.connect(self._on_completed)
        self.progress.connect(self._on_progress)

    @staticmethod
    def _on_completed(done_cb: object, result: object) -> None:
        if callable(done_cb):
            try:
                done_cb(result)
            except RuntimeError:
                pass

    @staticmethod
    def _on_progress(prog_cb: object, pct: int, msg: str) -> None:
        if callable(prog_cb):
            try:
                prog_cb(pct, msg)
            except RuntimeError:
                pass


def _run_in_background(owner: Any, work: Callable[[], object], done: Callable[[object], None]) -> None:
    """Esegue work fuori dal thread UI e consegna il risultato nel thread principale."""
    bridge = getattr(owner, "_bg_bridge", None)

    def _target() -> None:
        try:
            result: object = work()
        except Exception as exc:  # pragma: no cover
            result = exc
        if isinstance(bridge, _BackgroundBridge):
            try:
                bridge.completed.emit(done, result)
            except RuntimeError:
                pass
        elif hasattr(owner, "after"):
            try:
                owner.after(0, lambda: done(result))
            except Exception:
                pass

    threading.Thread(target=_target, daemon=True).start()


class _ReactiveVar:
    """Variabile reattiva leggera sincronizzata con uno o più widget Qt (QLabel, QLineEdit, QComboBox, QCheckBox)."""

    def __init__(self, value: Any = "") -> None:
        self._value: Any = value
        self._listeners: list[Callable[[Any], None]] = []
        self._getter: Callable[[], Any] | None = None

    def bind_Label(self, label: QtWidgets.QLabel) -> QtWidgets.QLabel:
        label.setText(str(self._value))
        self._listeners.append(lambda v: label.setText(str(v)))
        return label

    def bind_LineEdit(self, edit: QtWidgets.QLineEdit) -> QtWidgets.QLineEdit:
        edit.setText(str(self._value))
        self._listeners.append(lambda v: edit.setText(str(v)) if edit.text() != str(v) else None)
        edit.textChanged.connect(self._on_widget_changed)
        self._getter = edit.text
        return edit

    def bind_ComboBox(self, combo: QtWidgets.QComboBox) -> QtWidgets.QComboBox:
        text = str(self._value)
        idx = combo.findText(text)
        if idx >= 0:
            combo.setCurrentIndex(idx)
        else:
            combo.setEditText(text)

        def _apply(v: Any) -> None:
            s = str(v)
            i = combo.findText(s)
            if i >= 0:
                combo.setCurrentIndex(i)
            elif combo.isEditable():
                combo.setEditText(s)

        self._listeners.append(_apply)
        combo.currentTextChanged.connect(self._on_widget_changed)
        self._getter = combo.currentText
        return combo

    def bind_CheckBox(self, chk: QtWidgets.QCheckBox) -> QtWidgets.QCheckBox:
        chk.setChecked(bool(self._value))
        self._listeners.append(lambda v: chk.setChecked(bool(v)) if chk.isChecked() != bool(v) else None)
        chk.toggled.connect(self._on_widget_changed)
        self._getter = chk.isChecked
        return chk

    def _on_widget_changed(self, new_val: Any) -> None:
        self._value = new_val

    def get(self) -> Any:
        if self._getter is not None:
            try:
                return self._getter()
            except RuntimeError:
                return self._value
        return self._value

    def set(self, value: Any) -> None:
        self._value = value
        for listener in list(self._listeners):
            try:
                listener(value)
            except RuntimeError:
                pass


class _FluentButton(QtWidgets.QPushButton):
    """QPushButton con metodi di comodità compatibili con i test esistenti (state, config, cget)
    e protezione anti-taglio del testo su qualsiasi larghezza finestra."""

    def __init__(self, text: str = "", parent: QtWidgets.QWidget | None = None) -> None:
        super().__init__(text, parent)
        self.setSizePolicy(
            QtWidgets.QSizePolicy.Policy.Minimum,
            QtWidgets.QSizePolicy.Policy.Fixed,
        )

    def _computed_size(self) -> QtCore.QSize:
        base = super().sizeHint()
        fm = self.fontMetrics()
        text_width = fm.horizontalAdvance(self.text() or "")
        # Margine compatto per emoji Unicode DirectWrite + padding QSS
        width = max(base.width(), text_width + 32, 30)
        height = max(base.height(), 26)
        return QtCore.QSize(width, height)

    def sizeHint(self) -> QtCore.QSize:  # noqa: N802
        return self._computed_size()

    def minimumSizeHint(self) -> QtCore.QSize:  # noqa: N802
        return self._computed_size()

    def setText(self, text: str) -> None:  # noqa: N802
        super().setText(text)
        self.updateGeometry()

    def state(self, states: list[str] | tuple[str, ...] | None = None) -> tuple[str, ...]:
        if states is not None:
            for st in states:
                if st == "disabled":
                    self.setEnabled(False)
                elif st == "!disabled":
                    self.setEnabled(True)
        return () if self.isEnabled() else ("disabled",)

    def config(self, **kwargs: Any) -> None:
        if "text" in kwargs:
            self.setText(str(kwargs["text"]))

    configure = config

    def cget(self, key: str) -> Any:
        if key == "text":
            return self.text()
        return getattr(self, key, None)


class _FluentProgressBar(QtWidgets.QProgressBar):
    """QProgressBar con accesso indicizzato `bar['value']` oltre a `.value()` / `.setValue()`."""

    def __getitem__(self, key: str) -> Any:
        if key == "value":
            return self.value()
        raise KeyError(key)

    def __setitem__(self, key: str, val: Any) -> None:
        if key == "value":
            self.setValue(int(val or 0))
        else:
            raise KeyError(key)


class _FluentEventsList(QtWidgets.QListWidget):
    """QListWidget con metodi rapidi `delete(0, 'end')` e `insert('end', text)`."""

    def delete(self, _start: int | str, _end: str | None = None) -> None:
        self.clear()

    def insert(self, _pos: str | int, text: str) -> None:
        self.addItem(str(text))


class _FluentTreeWidget(QtWidgets.QTreeWidget):
    """QTreeWidget ad alte prestazioni con indicizzazione O(1) per iid (usato in Cruscotto e Storico)."""

    def __init__(self, *, show_tree_column: bool = False, parent: QtWidgets.QWidget | None = None) -> None:
        super().__init__(parent)
        self._show_tree_column = show_tree_column
        self._items_by_id: dict[str, QtWidgets.QTreeWidgetItem] = {}
        self.setAlternatingRowColors(True)
        self.setRootIsDecorated(show_tree_column)
        self.setUniformRowHeights(True)
        self.setSelectionMode(QtWidgets.QAbstractItemView.SelectionMode.SingleSelection)
        self.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectionBehavior.SelectRows)
        self.setEditTriggers(QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers)
        self.header().setStretchLastSection(True)

    def configure_columns(
        self,
        columns: tuple[str, ...],
        headings: tuple[str, ...],
        widths: tuple[int, ...],
        *,
        tree_heading: str = "",
        tree_width: int = 150,
    ) -> None:
        if self._show_tree_column:
            self.setColumnCount(len(columns) + 1)
            self.setHeaderLabels([tree_heading, *headings])
            self.setColumnWidth(0, tree_width)
            for idx, w in enumerate(widths, start=1):
                self.setColumnWidth(idx, w)
        else:
            self.setColumnCount(len(columns))
            self.setHeaderLabels(list(headings))
            for idx, w in enumerate(widths):
                self.setColumnWidth(idx, w)

    def get_children(self, parent_iid: str = "") -> tuple[str, ...]:
        if not parent_iid:
            res: list[str] = []
            for i in range(self.topLevelItemCount()):
                it = self.topLevelItem(i)
                if it is not None:
                    iid = it.data(0, Qt.ItemDataRole.UserRole)
                    if iid:
                        res.append(str(iid))
            return tuple(res)
        parent_item = self._items_by_id.get(parent_iid)
        if parent_item is None:
            return ()
        children_ids: list[str] = []
        for i in range(parent_item.childCount()):
            ch = parent_item.child(i)
            if ch is not None:
                cid = ch.data(0, Qt.ItemDataRole.UserRole)
                if cid:
                    children_ids.append(str(cid))
        return tuple(children_ids)

    def exists(self, iid: str) -> bool:
        return iid in self._items_by_id

    def insert(
        self,
        parent_iid: str,
        _index: str | int,
        *,
        iid: str,
        text: str = "",
        values: tuple[object, ...] = (),
        open: bool = False,
    ) -> str:
        str_vals = [str(v) for v in values]
        if self._show_tree_column:
            row_texts = [str(text), *str_vals]
        else:
            row_texts = str_vals if str_vals else [str(text)]

        item = QtWidgets.QTreeWidgetItem(row_texts)
        item.setData(0, Qt.ItemDataRole.UserRole, iid)
        for col_idx, col_text in enumerate(row_texts):
            if col_text:
                item.setToolTip(col_idx, col_text)
        if parent_iid and parent_iid in self._items_by_id:
            self._items_by_id[parent_iid].addChild(item)
        else:
            self.addTopLevelItem(item)
        if open:
            item.setExpanded(True)
        self._items_by_id[iid] = item
        return iid

    def item(self, iid: str, option: str | None = None, **kwargs: Any) -> Any:
        it = self._items_by_id.get(iid)
        if it is None:
            return () if option == "values" else {}
        if "values" in kwargs:
            vals = [str(v) for v in kwargs["values"]]
            offset = 1 if self._show_tree_column else 0
            for idx, val in enumerate(vals):
                it.setText(idx + offset, val)
                if val:
                    it.setToolTip(idx + offset, val)
        if option == "values":
            offset = 1 if self._show_tree_column else 0
            return tuple(it.text(c) for c in range(offset, self.columnCount()))
        return {
            "text": it.text(0) if self._show_tree_column else "",
            "values": tuple(
                it.text(c) for c in range(1 if self._show_tree_column else 0, self.columnCount())
            ),
        }

    def delete(self, *iids: str) -> None:
        if not iids:
            return
        top_count = self.topLevelItemCount()
        if len(iids) == top_count and top_count > 0:
            all_match = True
            for i in range(top_count):
                top_it = self.topLevelItem(i)
                if top_it is None or top_it.data(0, Qt.ItemDataRole.UserRole) not in iids:
                    all_match = False
                    break
            if all_match:
                self.clear()
                self._items_by_id.clear()
                return
        for iid in iids:
            it = self._items_by_id.pop(iid, None)
            if it is None:
                continue
            for child_idx in range(it.childCount()):
                child = it.child(child_idx)
                if child is not None:
                    cid = child.data(0, Qt.ItemDataRole.UserRole)
                    if cid:
                        self._items_by_id.pop(str(cid), None)
            parent = it.parent()
            if parent is not None:
                parent.removeChild(it)
            else:
                idx = self.indexOfTopLevelItem(it)
                if idx >= 0:
                    self.takeTopLevelItem(idx)

    def selection(self) -> tuple[str, ...]:
        selected = self.selectedItems()
        if not selected:
            return ()
        iid = selected[0].data(0, Qt.ItemDataRole.UserRole)
        return (str(iid),) if iid else ()

    def selection_set(self, iid: str) -> None:
        it = self._items_by_id.get(iid)
        if it is not None:
            self.setCurrentItem(it)
            it.setSelected(True)

    def see(self, iid: str) -> None:
        it = self._items_by_id.get(iid)
        if it is not None:
            self.scrollToItem(it)


class _GlossaryEdit(QtWidgets.QPlainTextEdit):
    """QPlainTextEdit con metodi `.get('1.0', 'end')`, `.delete('1.0', 'end')`, `.insert('1.0', text)`."""

    def get(self, _start: str = "1.0", _end: str = "end") -> str:
        return self.toPlainText()

    def delete(self, _start: str = "1.0", _end: str = "end") -> None:
        self.clear()

    def insert(self, _pos: str, text: str) -> None:
        self.setPlainText(str(text))


# ==============================================================================
# Finestra Principale del Cruscotto in PySide6
# ==============================================================================
def _ensure_qt_app() -> QtWidgets.QApplication:
    app = QtWidgets.QApplication.instance()
    if app is None:
        if "QT_QPA_PLATFORM" not in os.environ and (
            "PYTEST_CURRENT_TEST" in os.environ or os.environ.get("TEAMS_SCRIBE_HEADLESS_TRAY") == "1"
        ):
            os.environ["QT_QPA_PLATFORM"] = "offscreen"
        app = QtWidgets.QApplication(sys.argv[:1])
    assert isinstance(app, QtWidgets.QApplication)
    return app


class DashboardApp(QtWidgets.QMainWindow):
    """Finestra del cruscotto in PySide6 (Windows 11 Fluent Light/Dark): legge SQLite ogni secondo,
    gestisce lo storico riunioni e configura settings.json."""

    def __init__(
        self,
        root: Any,
        registry: JobRegistry,
        *,
        initial_tab: str | None = None,
    ) -> None:
        self._qt_app = _ensure_qt_app()
        super().__init__()
        self.root = root if root is not None else self
        self.registry = registry
        self.settings: AppSettings = load_settings()
        self.output_dir: Path = resolve_output_dir(self.settings)
        self._active_jobs: dict[str, dict[str, object]] = {}
        self._history_rows: dict[str, dict[str, object]] = {}
        self._selected_active: str | None = None
        self._last_history_signature: tuple[int, str, str] | None = None
        self._last_auto_day: str = date.today().isoformat()
        self._setting_vars: dict[tuple[str, str], _ReactiveVar] = {}
        self._help_labels: list[QtWidgets.QLabel] = []
        self._glossary_text: _GlossaryEdit | None = None
        self._model_combo: QtWidgets.QComboBox | None = None
        self._bg_bridge = _BackgroundBridge(self)
        self._update_info: UpdateInfo | None = None
        self._update_installed_version: str | None = None
        self._checking_updates: bool = False
        self._installing_update: bool = False
        self._update_banner_dismissed: bool = False

        # Tema persistito nel database (default: Windows 11 Fluent Light)
        saved_theme = "light"
        try:
            saved_theme = (self.registry.get_meta("ui_theme") or "light").strip().lower()
        except Exception:
            saved_theme = "light"
        self._theme: str = "dark" if saved_theme == "dark" else "light"

        self.setWindowTitle(DASHBOARD_TITLE)
        self.setMinimumSize(860, 500)
        screen = self._qt_app.primaryScreen()
        if screen is not None:
            avail = screen.availableGeometry()
            target_w = min(1100, max(880, avail.width() - 40))
            target_h = min(660, max(520, avail.height() - 40))
            self.resize(target_w, target_h)
        else:
            self.resize(1080, 640)
        if APP_ICON_PATH.exists():
            self.setWindowIcon(QtGui.QIcon(str(APP_ICON_PATH)))

        central = QtWidgets.QWidget(self)
        self.setCentralWidget(central)
        main_layout = QtWidgets.QVBoxLayout(central)
        main_layout.setContentsMargins(8, 6, 8, 6)
        main_layout.setSpacing(4)

        self._build_update_banner(main_layout)

        self.notebook = QtWidgets.QTabWidget(central)
        main_layout.addWidget(self.notebook)

        self.tab_dashboard = QtWidgets.QWidget()
        self.tab_history = QtWidgets.QWidget()
        self.tab_settings = QtWidgets.QWidget()
        self.notebook.addTab(self.tab_dashboard, "📊 Cruscotto")
        self.notebook.addTab(self.tab_history, "🗂️ Storico")
        self.notebook.addTab(self.tab_settings, "⚙️ Impostazioni")

        # Applica il foglio di stile prima di costruire i widget per avere fontMetrics accurate da subito
        self.apply_theme(self._theme, persist=False)

        self._build_dashboard_tab()
        self._build_history_tab()
        self._build_settings_tab()
        self.notebook.currentChanged.connect(self._on_tab_changed)

        tabs = {"cruscotto": 0, "storico": 1, "impostazioni": 2}
        if initial_tab in tabs:
            self.notebook.setCurrentIndex(tabs[initial_tab])

        self.apply_theme(self._theme, persist=False)

        self._load_history()
        self._refresh()

        self._timer = QTimer(self)
        self._timer.setInterval(REFRESH_INTERVAL_MS)
        self._timer.timeout.connect(self._refresh)
        self._timer.start()

        # Caricamento elenco modelli Ollama posticipato in background per non rallentare l'apertura
        QTimer.singleShot(50, lambda: self._refresh_models(silent=True))

        # Controllo automatico aggiornamenti su GitHub all'avvio del pannello (se abilitato)
        if (
            getattr(self.settings.execution, "auto_check_updates", True)
            and "PYTEST_CURRENT_TEST" not in os.environ
            and os.environ.get("QT_QPA_PLATFORM") != "offscreen"
        ):
            QTimer.singleShot(250, lambda: self._check_updates(silent=True))

    def _build_update_banner(self, parent_layout: QtWidgets.QVBoxLayout) -> None:
        """Costruisce il banner superiore per la notifica, il download e il riavvio post-aggiornamento."""
        self.update_banner = QtWidgets.QFrame(self)
        self.update_banner.setObjectName("updateBanner")
        self.update_banner.setVisible(False)

        banner_layout = QtWidgets.QHBoxLayout(self.update_banner)
        banner_layout.setContentsMargins(8, 4, 8, 4)
        banner_layout.setSpacing(8)

        self.var_update_banner = _ReactiveVar("")
        self.lbl_update_banner = self.var_update_banner.bind_Label(QtWidgets.QLabel())
        self.lbl_update_banner.setProperty("labelRole", "updateTitle")
        self.lbl_update_banner.setWordWrap(True)
        banner_layout.addWidget(self.lbl_update_banner, 1)

        self.update_progress_bar = _FluentProgressBar()
        self.update_progress_bar.setRange(0, 100)
        self.update_progress_bar.setValue(0)
        self.update_progress_bar.setFixedWidth(140)
        self.update_progress_bar.setVisible(False)
        banner_layout.addWidget(self.update_progress_bar)

        self.btn_release_notes = _FluentButton("📋 Note di rilascio")
        self.btn_release_notes.clicked.connect(self._show_release_notes)
        banner_layout.addWidget(self.btn_release_notes)

        self.btn_install_update = _FluentButton("⬇️ Scarica e installa")
        self.btn_install_update.setProperty("btnRole", "primary")
        self.btn_install_update.clicked.connect(self._cmd_install_update)
        banner_layout.addWidget(self.btn_install_update)

        self.btn_restart_after_update = _FluentButton("🚀 Riavvia ora")
        self.btn_restart_after_update.setProperty("btnRole", "primary")
        self.btn_restart_after_update.setVisible(False)
        self.btn_restart_after_update.clicked.connect(lambda: self._cmd_restart_app(skip_confirm=True))
        banner_layout.addWidget(self.btn_restart_after_update)

        self.btn_dismiss_update = _FluentButton("✕")
        self.btn_dismiss_update.setFixedWidth(30)
        self.btn_dismiss_update.setToolTip("Nascondi avviso aggiornamento")
        self.btn_dismiss_update.clicked.connect(self._dismiss_update_banner)
        banner_layout.addWidget(self.btn_dismiss_update)

        parent_layout.addWidget(self.update_banner)

    # ============================================================ gestione tema
    @property
    def current_theme(self) -> str:
        return self._theme

    def apply_theme(self, theme: str, *, persist: bool = True) -> None:
        """Applica il tema Windows 11 Fluent ('light' o 'dark') e aggiorna il pulsante selettore."""
        normalized = "dark" if str(theme).strip().lower() == "dark" else "light"
        self._theme = normalized
        self.setStyleSheet(FLUENT_DARK_QSS if normalized == "dark" else FLUENT_LIGHT_QSS)
        if hasattr(self, "btn_theme"):
            self.btn_theme.setText("☀️ Tema Chiaro" if normalized == "dark" else "🌙 Tema Scuro")
            self.btn_theme.setToolTip(
                "Passa al tema Windows 11 Fluent Chiaro"
                if normalized == "dark"
                else "Passa al tema Windows 11 Fluent Scuro"
            )
        if persist:
            try:
                self.registry.set_meta("ui_theme", normalized)
            except Exception as exc:
                logger.debug("Salvataggio preferenza tema non riuscito: %s", exc)

    def toggle_theme(self) -> str:
        next_theme = "dark" if self._theme == "light" else "light"
        self.apply_theme(next_theme, persist=True)
        return self._theme

    # ============================================================ cruscotto
    def _build_dashboard_tab(self) -> None:
        layout = QtWidgets.QVBoxLayout(self.tab_dashboard)
        layout.setContentsMargins(8, 4, 8, 6)
        layout.setSpacing(6)

        # Card Stato Superiore compatta su 2 righe
        status_box = QtWidgets.QGroupBox("Stato Sistema")
        status_vbox = QtWidgets.QVBoxLayout(status_box)
        status_vbox.setContentsMargins(8, 6, 8, 4)
        status_vbox.setSpacing(3)

        self.var_version = _ReactiveVar(f"Versione: v{APP_VERSION}")
        self.var_app = _ReactiveVar("Applicazione: -")
        self.var_watcher = _ReactiveVar("Watcher: -")
        self.var_gpu = _ReactiveVar("LLM su GPU: -")
        self.var_models = _ReactiveVar("Modelli: -")
        self.var_queue = _ReactiveVar("Coda: -")

        status_row1 = QtWidgets.QHBoxLayout()
        status_row1.setSpacing(14)
        self.lbl_version = self.var_version.bind_Label(QtWidgets.QLabel())
        self.lbl_version.setProperty("labelRole", "version")
        self.lbl_version.setToolTip(f"Versione corrente di {APP_NAME}: v{APP_VERSION}")
        status_row1.addWidget(self.lbl_version)

        for var in (self.var_app, self.var_watcher, self.var_queue):
            lbl = var.bind_Label(QtWidgets.QLabel())
            status_row1.addWidget(lbl)
        status_row1.addStretch(1)

        self.incognito_var = _ReactiveVar(bool(self.settings.detection.incognito_mode))
        self.incognito_btn = self.incognito_var.bind_CheckBox(
            QtWidgets.QCheckBox("Modalità in incognito (Zero Popup)")
        )
        self.incognito_btn.clicked.connect(self._on_toggle_incognito_quick)
        status_row1.addWidget(self.incognito_btn)

        self.btn_theme = _FluentButton("🌙 Tema Scuro")
        self.btn_theme.clicked.connect(self.toggle_theme)
        status_row1.addWidget(self.btn_theme)
        status_vbox.addLayout(status_row1)

        status_row2 = QtWidgets.QHBoxLayout()
        status_row2.setSpacing(18)
        for var in (self.var_gpu, self.var_models):
            lbl = var.bind_Label(QtWidgets.QLabel())
            status_row2.addWidget(lbl)
        status_row2.addStretch(1)

        self.btn_check_updates = _FluentButton("🔄 Verifica aggiornamenti")
        self.btn_check_updates.setToolTip("Controlla su GitHub se è disponibile una nuova versione")
        self.btn_check_updates.clicked.connect(self._cmd_check_updates)
        status_row2.addWidget(self.btn_check_updates)

        self.btn_restart_app = _FluentButton("🔁 Riavvia app")
        self.btn_restart_app.setToolTip("Riavvia il servizio in background e il Cruscotto")
        self.btn_restart_app.clicked.connect(lambda: self._cmd_restart_app(skip_confirm=False))
        status_row2.addWidget(self.btn_restart_app)
        status_vbox.addLayout(status_row2)

        layout.addWidget(status_box)

        # Barra Pulsanti Azione compatta
        btn_vbox = QtWidgets.QVBoxLayout()
        btn_vbox.setSpacing(4)

        btn_row1 = QtWidgets.QHBoxLayout()
        btn_row1.setSpacing(6)

        btn_start = _FluentButton("▶ Avvia registrazione manuale")
        btn_start.setProperty("btnRole", "primary")
        btn_start.clicked.connect(self._cmd_start_manual)
        btn_row1.addWidget(btn_start)

        btn_fin = _FluentButton("⏹ Termina call ora")
        btn_fin.clicked.connect(self._cmd_finalize)
        btn_row1.addWidget(btn_fin)

        self.btn_pause = _FluentButton("⏸ Pausa")
        self.btn_pause.setEnabled(False)
        self.btn_pause.clicked.connect(self._cmd_toggle_pause)
        btn_row1.addWidget(self.btn_pause)

        self.btn_stop = _FluentButton("Arresta elaborazione")
        self.btn_stop.setProperty("btnRole", "danger")
        self.btn_stop.setEnabled(False)
        self.btn_stop.clicked.connect(self._cmd_stop_selected)
        btn_row1.addWidget(self.btn_stop)

        btn_retry = _FluentButton("🔄 Riprova")
        btn_retry.clicked.connect(self._cmd_retry_selected)
        btn_row1.addWidget(btn_retry)

        btn_import = _FluentButton("📂 Importa Registrazione")
        btn_import.clicked.connect(self._cmd_import_recording)
        btn_row1.addWidget(btn_import)
        btn_row1.addStretch(1)
        btn_vbox.addLayout(btn_row1)

        btn_row2 = QtWidgets.QHBoxLayout()
        btn_row2.setSpacing(6)

        btn_docx = _FluentButton("📄 Apri DOCX")
        btn_docx.clicked.connect(self._open_selected_docx)
        btn_row2.addWidget(btn_docx)

        btn_folder = _FluentButton("📁 Apri cartella")
        btn_folder.clicked.connect(self._open_selected_folder)
        btn_row2.addWidget(btn_folder)

        btn_email = _FluentButton("✉️ Copia bozza mail")
        btn_email.clicked.connect(self._cmd_copy_email)
        btn_row2.addWidget(btn_email)

        self.btn_delete_active_job = _FluentButton("Elimina transazione")
        self.btn_delete_active_job.setProperty("btnRole", "danger")
        self.btn_delete_active_job.setEnabled(False)
        self.btn_delete_active_job.clicked.connect(self._on_delete_active_selected)
        btn_row2.addWidget(self.btn_delete_active_job)
        btn_row2.addStretch(1)
        btn_vbox.addLayout(btn_row2)

        layout.addLayout(btn_vbox)

        # Splitter verticale tra Tabella Sessioni Attive e Dettaglio Elaborazione (ottimizzato per 14")
        splitter = QtWidgets.QSplitter(Qt.Orientation.Vertical)
        splitter.setChildrenCollapsible(False)

        columns = ("title", "start", "status", "stage", "progress", "in_stage", "total")
        headings = ("Titolo", "Inizio", "Stato", "Fase", "Avanzamento", "Tempo in fase", "Tempo totale")
        widths = (220, 85, 110, 210, 130, 90, 90)
        self.active_tree = _FluentTreeWidget(show_tree_column=False)
        self.active_tree.configure_columns(columns, headings, widths)
        self.active_tree.setMinimumHeight(78)
        self.active_tree.itemSelectionChanged.connect(self._on_active_select)
        self.active_tree.itemDoubleClicked.connect(lambda _it, _col: self._open_selected_docx())
        self.active_tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.active_tree.customContextMenuRequested.connect(self._on_active_context_menu)

        del_active_shortcut = QtGui.QShortcut(QtGui.QKeySequence(Qt.Key.Key_Delete), self.active_tree)
        del_active_shortcut.setContext(Qt.ShortcutContext.WidgetShortcut)
        del_active_shortcut.activated.connect(self._on_delete_active_selected)

        self._active_menu = QtWidgets.QMenu(self.active_tree)
        self._active_menu.addAction("Apri Word", self._open_selected_docx)
        self._active_menu.addAction("Riprova", self._cmd_retry_selected)
        self._active_menu.addAction("Pausa / Riprendi", self._cmd_toggle_pause)
        self._active_menu.addAction("Arresta", self._cmd_stop_selected)
        self._active_menu.addAction("Importa Registrazione", self._cmd_import_recording)
        self._active_menu.addSeparator()
        self._active_menu.addAction("Elimina completamente la transazione", self._on_delete_active_selected)
        splitter.addWidget(self.active_tree)

        # Pannello Dettaglio e Pipeline (espanso verticalmente per una navigazione agevole su 14")
        detail_box = QtWidgets.QGroupBox("Dettaglio Elaborazione e Ultimi Eventi")
        detail_layout = QtWidgets.QVBoxLayout(detail_box)
        detail_layout.setContentsMargins(8, 4, 8, 6)
        detail_layout.setSpacing(4)

        steps_row = QtWidgets.QHBoxLayout()
        steps_row.setSpacing(4)
        self.step_vars: list[_ReactiveVar] = []
        for index, name in enumerate(PIPELINE_STEPS):
            var = _ReactiveVar(f"○ {name}")
            self.step_vars.append(var)
            lbl_step = var.bind_Label(QtWidgets.QLabel())
            lbl_step.setProperty("labelRole", "step")
            steps_row.addWidget(lbl_step)
            if index < len(PIPELINE_STEPS) - 1:
                arrow = QtWidgets.QLabel("→")
                arrow.setAlignment(Qt.AlignmentFlag.AlignCenter)
                steps_row.addWidget(arrow)
        steps_row.addStretch(1)
        detail_layout.addLayout(steps_row)

        prog_row = QtWidgets.QHBoxLayout()
        prog_row.setSpacing(10)
        self.progress_bar = _FluentProgressBar()
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        self.progress_bar.setFixedWidth(180)
        prog_row.addWidget(self.progress_bar)

        self.var_progress_pct = _ReactiveVar("Avanzamento elaborazione: -")
        prog_row.addWidget(self.var_progress_pct.bind_Label(QtWidgets.QLabel()), 1)
        detail_layout.addLayout(prog_row)

        self.var_error = _ReactiveVar("")
        self._lbl_error = self.var_error.bind_Label(QtWidgets.QLabel())
        self._lbl_error.setProperty("labelRole", "warn")
        self._lbl_error.setWordWrap(True)
        self._lbl_error.setVisible(False)
        detail_layout.addWidget(self._lbl_error)

        self.events_list = _FluentEventsList()
        self.events_list.setObjectName("eventsList")
        self.events_list.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.events_list.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        detail_layout.addWidget(self.events_list, 1)

        splitter.addWidget(detail_box)
        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 3)
        splitter.setSizes([105, 340])
        layout.addWidget(splitter, 1)

    def _send(self, command: str, payload: dict[str, object] | None = None) -> None:
        try:
            self.registry.enqueue_command(command, payload)
        except Exception as exc:
            self._error("Comando non inviato", str(exc))

    def _cmd_start_manual(self) -> None:
        self._send(UiCommandType.START_MANUAL.value)

    def _cmd_finalize(self) -> None:
        self._send(UiCommandType.FINALIZE_NOW.value)

    def _cmd_toggle_pause(self) -> None:
        if hasattr(self, "btn_pause"):
            text = self.btn_pause.cget("text")
            if "Riprendi" in text:
                self._send(UiCommandType.RESUME_RECORDING.value)
            else:
                self._send(UiCommandType.PAUSE_RECORDING.value)
        else:
            self._send(UiCommandType.TOGGLE_PAUSE.value)

    def _cmd_import_recording(self) -> None:
        import config as _cfg

        filter_str = (
            "File Audio e Video (*.wav *.mp3 *.m4a *.mp4 *.mkv *.ogg *.flac *.webm *.aac *.wma);;"
            "File Audio (*.wav *.mp3 *.m4a *.ogg *.flac *.aac *.wma);;"
            "File Video (*.mp4 *.mkv *.webm);;"
            "Tutti i file (*.*)"
        )
        filename, _ = QtWidgets.QFileDialog.getOpenFileName(
            self,
            "Seleziona file audio o video da importare",
            "",
            filter_str,
        )
        if not filename:
            return
        src_path = Path(filename).resolve()
        ext = src_path.suffix.lower()
        if ext not in _cfg.SUPPORTED_IMPORT_EXTENSIONS:
            self._error(
                "Formato non supportato",
                f"Il formato '{ext}' non è supportato.\nEstensioni ammesse: {', '.join(_cfg.SUPPORTED_IMPORT_EXTENSIONS)}",
            )
            return

        default_title = src_path.stem.replace("_", " ").title()
        title, ok = QtWidgets.QInputDialog.getText(
            self,
            "Titolo Riunione",
            "Inserisci il titolo della riunione (opzionale):",
            QtWidgets.QLineEdit.EchoMode.Normal,
            default_title,
        )
        if not ok:
            return
        meeting_title = title.strip() or default_title

        status = self.registry.get_status()
        heartbeat = parse_iso(status.get("heartbeat"))
        alive = heartbeat is not None and (now_tz() - heartbeat).total_seconds() < HEARTBEAT_STALE_SEC

        if alive:
            self._send(
                UiCommandType.IMPORT_AUDIO.value,
                {"file_path": str(src_path), "title": meeting_title},
            )
            self._info(
                "Importazione avviata",
                f"Il file '{src_path.name}' è stato inviato all'applicazione per l'elaborazione.",
            )
        else:
            def _work() -> Path:
                from modules.orchestrator import MeetingScribeApplication

                app = MeetingScribeApplication(headless=True)
                return app.run_import_pipeline(src_path, title=meeting_title)

            def _done(result: object) -> None:
                if isinstance(result, Exception):
                    self._error("Errore importazione", f"Impossibile completare l'elaborazione: {result}")
                else:
                    self._info("Importazione completata", f"Verbale generato con successo:\n{result}")
                    self._refresh()

            self._info(
                "Importazione avviata",
                f"Elaborazione in background di '{src_path.name}' in corso...",
            )
            _run_in_background(self, _work, _done)

    def _cmd_stop_selected(self) -> None:
        session_id = self._selected_active
        if not session_id:
            self._info("Arresta", "Selezionare prima una sessione attiva nella tabella.")
            return
        job = self._active_jobs.get(session_id)
        title = str(job.get("title") or session_id) if job else session_id
        if not self._confirm(
            "Arresta elaborazione",
            f"Sei sicuro di voler arrestare l'elaborazione per '{title}'?\n\n"
            "Tutti i dati e i file audio finora prodotti saranno preservati.\n"
            "Potrai riprendere l'elaborazione in qualsiasi momento cliccando 'Riprova'.",
        ):
            return
        self._send(UiCommandType.CANCEL_JOB.value, {"session_id": session_id})
        self._info("Arresta elaborazione", "Richiesta inviata: l'elaborazione verrà arrestata.")

    def _cmd_copy_email(self) -> None:
        job = self._job_for_action()
        if not job:
            self._info("Copia bozza mail", "Selezionare prima una sessione.")
            return

        text = ""
        session_dir_str = job.get("session_dir")
        if isinstance(session_dir_str, str):
            candidate = Path(session_dir_str) / "reduce" / "recap_email.md"
            if candidate.is_file():
                try:
                    text = candidate.read_text(encoding="utf-8").strip()
                except Exception as exc:
                    logger.debug("Lettura recap_email.md fallita: %s", exc)

        if not text:
            docx_str = job.get("docx_path")
            if isinstance(docx_str, str) and Path(docx_str).is_file():
                try:
                    import docx

                    d = docx.Document(docx_str)
                    extracted: list[str] = []
                    capturing = False
                    for p in d.paragraphs:
                        p_text = p.text.strip()
                        if "Parte 1 - Bozza Email di Recap" in p_text:
                            capturing = True
                            continue
                        elif "Parte 2 - Verbale Dettagliato" in p_text:
                            break
                        if capturing and p_text:
                            extracted.append(p_text)
                    if extracted:
                        text = "\n\n".join(extracted)
                except Exception as exc:
                    logger.debug("Estrazione mail da docx non riuscita: %s", exc)

        if not text:
            self._info(
                "Copia bozza mail",
                "Bozza email non disponibile per questa sessione "
                "(potrebbe essere stata disattivata nelle impostazioni o la sessione non è ancora completata).",
            )
            return

        clipboard = QtWidgets.QApplication.clipboard()
        if clipboard is not None:
            clipboard.setText(text)
        self._info("Copia bozza mail", "Bozza email copiata negli appunti con successo!")

    def _cmd_retry_selected(self) -> None:
        session_id = self._selected_active or self._selected_history_id()
        if session_id is None:
            self._info("Riprova", "Selezionare prima una sessione in errore.")
            return
        self._send(UiCommandType.RETRY_JOB.value, {"session_id": session_id})
        self._info("Riprova", "Richiesta inviata: la sessione verrà rimessa in coda.")

    def _job_for_action(self) -> dict[str, object] | None:
        if self.notebook.currentIndex() == 1:
            session_id = self._selected_history_id()
            return self._history_rows.get(session_id) if session_id else None
        return self._active_jobs.get(self._selected_active or "")

    def _open_selected_docx(self) -> None:
        job = self._job_for_action()
        docx_path = job.get("docx_path") if job else None
        if not isinstance(docx_path, str) or not _open_path(Path(docx_path)):
            self._info("Apri DOCX", "Nessun documento Word disponibile per la sessione selezionata.")

    def _open_selected_folder(self) -> None:
        job = self._job_for_action()
        docx_path = job.get("docx_path") if job else None
        if isinstance(docx_path, str) and Path(docx_path).parent.is_dir():
            _open_path(Path(docx_path).parent)
            return
        started = parse_iso(job.get("call_started_at")) if job else None
        day_dir = self.output_dir / (started or now_tz()).strftime("%Y-%m-%d")
        if not _open_path(day_dir if day_dir.is_dir() else self.output_dir):
            self._info("Apri cartella", "Cartella non disponibile.")

    def _update_action_buttons(self) -> None:
        if hasattr(self, "btn_stop"):
            curr_job = self._active_jobs.get(self._selected_active or "")
            status = str(curr_job.get("status") or "") if curr_job else ""
            if status in ("processing", "queued", "recording", "paused"):
                self.btn_stop.state(["!disabled"])
            else:
                self.btn_stop.state(["disabled"])

        if hasattr(self, "btn_delete_active_job"):
            if self._selected_active:
                self.btn_delete_active_job.state(["!disabled"])
            else:
                self.btn_delete_active_job.state(["disabled"])

        if hasattr(self, "btn_pause"):
            recording_job = None
            if self._selected_active and self._selected_active in self._active_jobs:
                cand = self._active_jobs[self._selected_active]
                if cand.get("status") in ("recording", "paused") or cand.get("stage") in (
                    Stage.RECORDING.value,
                    Stage.RECORDING_AND_TRANSCRIBING.value,
                    Stage.PAUSED.value,
                    Stage.GRACE_PERIOD.value,
                ):
                    recording_job = cand
            if recording_job is None:
                for j in self._active_jobs.values():
                    if j.get("status") in ("recording", "paused") or j.get("stage") in (
                        Stage.RECORDING.value,
                        Stage.RECORDING_AND_TRANSCRIBING.value,
                        Stage.PAUSED.value,
                        Stage.GRACE_PERIOD.value,
                    ):
                        recording_job = j
                        break

            if recording_job is not None:
                self.btn_pause.state(["!disabled"])
                is_p = (recording_job.get("status") == "paused") or (
                    str(recording_job.get("stage") or "").lower() == Stage.PAUSED.value
                )
                if is_p:
                    self.btn_pause.config(text="▶ Riprendi")
                else:
                    self.btn_pause.config(text="⏸ Pausa")
            else:
                self.btn_pause.config(text="⏸ Pausa")
                self.btn_pause.state(["disabled"])

    def _on_active_select(self) -> None:
        selection = self.active_tree.selection()
        self._selected_active = selection[0] if selection else None
        self._update_action_buttons()
        self._refresh_detail()

    def _on_active_context_menu(self, pos: QtCore.QPoint) -> None:
        item = self.active_tree.itemAt(pos)
        if item is not None:
            iid = item.data(0, Qt.ItemDataRole.UserRole)
            if iid:
                self.active_tree.selection_set(str(iid))
                self._on_active_select()
                self._active_menu.exec(self.active_tree.viewport().mapToGlobal(pos))

    def _on_delete_active_selected(self) -> None:
        sid = self._selected_active
        if not sid:
            return
        self._delete_transaction(sid, self._active_jobs.get(sid))

    def _refresh(self) -> None:
        try:
            self._refresh_status()
            self._refresh_active_jobs()
            self._refresh_detail()
            self._refresh_history_if_changed()
        except Exception as exc:
            logger.debug("Aggiornamento del cruscotto non riuscito: %s", exc)

    def _on_tab_changed(self, index: int = -1) -> None:
        try:
            idx = index if index >= 0 else self.notebook.currentIndex()
            if idx == 1:
                self._load_history()
        except Exception as exc:
            logger.debug("Aggiornamento al cambio scheda non riuscito: %s", exc)

    def _refresh_history_if_changed(self) -> None:
        today_iso = date.today().isoformat()
        day_shifted = False
        if today_iso != self._last_auto_day:
            if hasattr(self, "var_day") and str(self.var_day.get()).strip() == self._last_auto_day:
                self.var_day.set(today_iso)
                day_shifted = True
            self._last_auto_day = today_iso
        sig = self.registry.jobs_signature()
        if day_shifted or sig != self._last_history_signature:
            self._load_history()

    def _refresh_status(self) -> None:
        status = self.registry.get_status()
        heartbeat = parse_iso(status.get("heartbeat"))
        alive = heartbeat is not None and (now_tz() - heartbeat).total_seconds() < HEARTBEAT_STALE_SEC
        self.var_app.set(f"Applicazione: {'🟢 attiva' if alive else '⚪ non in esecuzione'}")

        incognito_state = status.get("incognito_mode")
        if isinstance(incognito_state, bool) and alive:
            self.incognito_var.set(incognito_state)

        watcher_stage = status.get("watcher_stage") if alive else None
        watcher_text = _stage_label(watcher_stage) if watcher_stage else "-"
        started = status.get("recording_started_at")
        if alive and isinstance(started, str) and watcher_stage in (
            Stage.RECORDING.value,
            Stage.RECORDING_AND_TRANSCRIBING.value,
            Stage.GRACE_PERIOD.value,
            Stage.PAUSED.value,
        ):
            watcher_text += f" ({format_duration(_elapsed(started))})"
        detail = status.get("watcher_detail")
        if alive and watcher_stage == Stage.GRACE_PERIOD.value and isinstance(detail, str):
            watcher_text = detail
        self.var_watcher.set(f"Watcher: {watcher_text}")

        gpu = status.get("llm_gpu_percent")
        gpu_text = f"{float(gpu):.0f}%" if isinstance(gpu, (int, float)) else "non misurato"
        parallel = status.get("ai_parallel")
        mode_text = "parallelo" if parallel else "sequenziale"
        self.var_gpu.set(f"LLM su GPU: {gpu_text} ({mode_text})")
        self.var_models.set(
            f"Modelli: {status.get('llm_model') or self.settings.ollama.model} / "
            f"Whisper {status.get('whisper_model') or self.settings.whisper.model}"
        )
        queue_len = status.get("queue_length")
        self.var_queue.set(f"Coda: {queue_len if isinstance(queue_len, int) else '-'}")

        if self._update_installed_version:
            self.var_version.set(
                f"Versione: v{APP_VERSION} → v{self._update_installed_version} (riavvio richiesto)"
            )
        elif self._update_info is None and not self._update_banner_dismissed:
            try:
                raw_upd = self.registry.get_meta("update_info")
                if raw_upd:
                    parsed_upd = json.loads(raw_upd)
                    if isinstance(parsed_upd, dict):
                        cached_info = UpdateInfo.from_dict(parsed_upd)
                        if is_newer_version(cached_info.latest_version, APP_VERSION):
                            self._apply_update_info(cached_info, show_up_to_date_dialog=False)
            except Exception as exc:
                logger.debug("Lettura stato aggiornamento da SQLite non riuscita: %s", exc)

    def _refresh_active_jobs(self) -> None:
        jobs = self.registry.list_active_jobs()
        self._active_jobs = {str(job["session_id"]): job for job in jobs}
        tree = self.active_tree
        existing = set(tree.get_children())
        for job in jobs:
            session_id = str(job["session_id"])
            started = parse_iso(job.get("call_started_at"))
            end_marker = job.get("processing_ended_at") if job.get("status") == "completed" else None
            values = (
                job.get("title") or "Riunione Teams",
                started.strftime("%d/%m %H:%M") if started else "-",
                JOB_STATUS_LABELS_IT.get(str(job.get("status")), str(job.get("status"))),
                _stage_label(job.get("stage")),
                describe_progress(job),
                format_duration(_elapsed(job.get("stage_started_at"), end_marker)),
                format_duration(_elapsed(job.get("call_started_at"), end_marker)),
            )
            if session_id in existing:
                tree.item(session_id, values=values)
                existing.discard(session_id)
            else:
                tree.insert("", "end", iid=session_id, values=values)
        for stale in existing:
            tree.delete(stale)
        if self._selected_active not in self._active_jobs:
            self._selected_active = None
        self._update_action_buttons()

    def _refresh_detail(self) -> None:
        job = self._active_jobs.get(self._selected_active or "")
        if job is None and self._active_jobs:
            job = next(iter(self._active_jobs.values()))
        if job is None:
            for index, name in enumerate(PIPELINE_STEPS):
                self.step_vars[index].set(f"○ {name}")
            if hasattr(self, "progress_bar"):
                self.progress_bar["value"] = 0
            if hasattr(self, "var_progress_pct"):
                self.var_progress_pct.set("Avanzamento elaborazione: -")
            self.var_error.set("Nessuna sessione attiva.")
            if hasattr(self, "_lbl_error"):
                self._lbl_error.setVisible(False)
            self.events_list.delete(0, "end")
            return
        marks = pipeline_marks(str(job.get("stage") or ""), str(job.get("status") or ""))
        for index, name in enumerate(PIPELINE_STEPS):
            self.step_vars[index].set(f"{marks[index]} {name}")
        pct = compute_job_progress_percent(job)
        prog_text = describe_progress(job)
        if hasattr(self, "progress_bar"):
            self.progress_bar["value"] = pct if pct is not None else 0
        if hasattr(self, "var_progress_pct"):
            if pct is not None:
                detail_str = str(job.get("detail") or "").strip()
                suffix = f" — {detail_str}" if detail_str else ""
                self.var_progress_pct.set(f"Avanzamento elaborazione: {prog_text}{suffix}")
            elif str(job.get("status") or "") in ("recording", "paused") or str(job.get("stage") or "") in (
                Stage.RECORDING.value,
                Stage.RECORDING_AND_TRANSCRIBING.value,
                Stage.PAUSED.value,
                Stage.GRACE_PERIOD.value,
            ):
                extra = f" ({prog_text})" if prog_text else ""
                self.var_progress_pct.set(f"Avanzamento elaborazione: in attesa di fine registrazione{extra}")
            else:
                self.var_progress_pct.set(f"Avanzamento elaborazione: {prog_text or '-'}")
        error = job.get("error")
        retry_at = parse_iso(job.get("next_retry_at"))
        message = f"Errore: {error}" if error else ""
        if retry_at is not None:
            message += f"  — nuovo tentativo alle {retry_at.strftime('%H:%M:%S')}"
        if job.get("status") == "parked":
            message += "  — usare Riprova per rimetterla in coda"
        cleaned_msg = message.strip()
        self.var_error.set(cleaned_msg)
        if hasattr(self, "_lbl_error"):
            self._lbl_error.setVisible(bool(cleaned_msg))
        events = self.registry.get_events(str(job["session_id"]), limit=40)
        self.events_list.delete(0, "end")
        for event in reversed(events):
            at = parse_iso(event.get("occurred_at"))
            stamp = at.strftime("%H:%M:%S") if at else "--:--:--"
            label = _stage_label(event.get("stage")) if event.get("stage") else str(event.get("kind"))
            detail = event.get("detail") or ""
            self.events_list.insert("end", f"{stamp}  {label}  {detail}")

    # ============================================================== storico
    def _build_history_tab(self) -> None:
        layout = QtWidgets.QVBoxLayout(self.tab_history)
        layout.setContentsMargins(8, 6, 8, 6)
        layout.setSpacing(6)

        filters_vbox = QtWidgets.QVBoxLayout()
        filters_vbox.setSpacing(4)

        self.var_day = _ReactiveVar(date.today().isoformat())
        self.var_all_days = _ReactiveVar(False)
        self.var_from = _ReactiveVar("")
        self.var_to = _ReactiveVar("")
        self.var_status = _ReactiveVar(HISTORY_STATUS_FILTERS[0][0])
        self.var_text = _ReactiveVar("")

        filters_row1 = QtWidgets.QHBoxLayout()
        filters_row1.setSpacing(6)
        filters_row1.addWidget(QtWidgets.QLabel("Giorno"))
        btn_prev = _FluentButton("◀")
        btn_prev.setFixedWidth(30)
        btn_prev.clicked.connect(lambda: self._shift_day(-1))
        filters_row1.addWidget(btn_prev)

        day_entry = self.var_day.bind_LineEdit(QtWidgets.QLineEdit())
        day_entry.setFixedWidth(96)
        day_entry.setMinimumHeight(26)
        day_entry.returnPressed.connect(self._load_history)
        filters_row1.addWidget(day_entry)

        btn_next = _FluentButton("▶")
        btn_next.setFixedWidth(30)
        btn_next.clicked.connect(lambda: self._shift_day(1))
        filters_row1.addWidget(btn_next)

        btn_today = _FluentButton("Oggi")
        btn_today.clicked.connect(self._today)
        filters_row1.addWidget(btn_today)

        chk_all = self.var_all_days.bind_CheckBox(QtWidgets.QCheckBox("Tutti i giorni"))
        chk_all.clicked.connect(self._load_history)
        filters_row1.addWidget(chk_all)

        filters_row1.addSpacing(10)
        filters_row1.addWidget(QtWidgets.QLabel("Dal"))
        from_entry = self.var_from.bind_LineEdit(QtWidgets.QLineEdit())
        from_entry.setFixedWidth(96)
        from_entry.setMinimumHeight(26)
        from_entry.setPlaceholderText("YYYY-MM-DD")
        from_entry.returnPressed.connect(self._load_history)
        filters_row1.addWidget(from_entry)

        filters_row1.addWidget(QtWidgets.QLabel("Al"))
        to_entry = self.var_to.bind_LineEdit(QtWidgets.QLineEdit())
        to_entry.setFixedWidth(96)
        to_entry.setMinimumHeight(26)
        to_entry.setPlaceholderText("YYYY-MM-DD")
        to_entry.returnPressed.connect(self._load_history)
        filters_row1.addWidget(to_entry)
        filters_row1.addStretch(1)
        filters_vbox.addLayout(filters_row1)

        filters_row2 = QtWidgets.QHBoxLayout()
        filters_row2.setSpacing(6)
        filters_row2.addWidget(QtWidgets.QLabel("Esito"))
        status_combo = QtWidgets.QComboBox()
        status_combo.setMinimumWidth(165)
        status_combo.setMinimumHeight(26)
        status_combo.addItems([label for label, _ in HISTORY_STATUS_FILTERS])
        self.var_status.bind_ComboBox(status_combo)
        status_combo.currentIndexChanged.connect(lambda _i: self._load_history())
        filters_row2.addWidget(status_combo)

        filters_row2.addSpacing(10)
        filters_row2.addWidget(QtWidgets.QLabel("Testo"))
        text_entry = self.var_text.bind_LineEdit(QtWidgets.QLineEdit())
        text_entry.setMinimumWidth(200)
        text_entry.setMinimumHeight(26)
        text_entry.setPlaceholderText("Cerca nel titolo...")
        text_entry.returnPressed.connect(self._load_history)
        filters_row2.addWidget(text_entry)

        btn_search = _FluentButton("🔍 Cerca")
        btn_search.setProperty("btnRole", "primary")
        btn_search.clicked.connect(self._load_history)
        filters_row2.addWidget(btn_search)
        filters_row2.addStretch(1)
        filters_vbox.addLayout(filters_row2)

        layout.addLayout(filters_vbox)

        columns = ("time", "title", "call", "processing", "outcome", "model", "file")
        headings = ("Data", "Titolo", "Durata call", "Durata elaborazione", "Esito", "Modello", "File")
        widths = (110, 225, 85, 130, 125, 100, 200)
        self.history_tree = _FluentTreeWidget(show_tree_column=True)
        self.history_tree.configure_columns(
            columns,
            headings,
            widths,
            tree_heading="Giorno",
            tree_width=115,
        )
        self.history_tree.itemSelectionChanged.connect(self._on_history_select)
        self.history_tree.itemDoubleClicked.connect(lambda _it, _col: self._show_timeline())
        self.history_tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.history_tree.customContextMenuRequested.connect(self._on_history_context_menu)

        del_hist_shortcut = QtGui.QShortcut(QtGui.QKeySequence(Qt.Key.Key_Delete), self.history_tree)
        del_hist_shortcut.setContext(Qt.ShortcutContext.WidgetShortcut)
        del_hist_shortcut.activated.connect(self._on_history_delete)

        self._history_menu = QtWidgets.QMenu(self.history_tree)
        self._history_menu.addAction("Apri Word", self._open_selected_docx)
        self._history_menu.addAction("Apri cartella", self._open_selected_folder)
        self._history_menu.addAction("Riprocessa", self._reprocess_selected)
        self._history_menu.addSeparator()
        self._history_menu.addAction("Elimina completamente la transazione", self._on_history_delete)
        layout.addWidget(self.history_tree, 1)

        actions_vbox = QtWidgets.QVBoxLayout()
        actions_vbox.setSpacing(6)

        actions_row1 = QtWidgets.QHBoxLayout()
        actions_row1.setSpacing(8)
        btn_h_docx = _FluentButton("📄 Apri DOCX")
        btn_h_docx.clicked.connect(self._open_selected_docx)
        actions_row1.addWidget(btn_h_docx)

        btn_h_folder = _FluentButton("📁 Apri cartella del giorno")
        btn_h_folder.clicked.connect(self._open_selected_folder)
        actions_row1.addWidget(btn_h_folder)

        btn_h_mail = _FluentButton("✉️ Copia bozza mail")
        btn_h_mail.clicked.connect(self._cmd_copy_email)
        actions_row1.addWidget(btn_h_mail)

        btn_h_timeline = _FluentButton("⏱️ Timeline")
        btn_h_timeline.clicked.connect(self._show_timeline)
        actions_row1.addWidget(btn_h_timeline)

        btn_h_csv = _FluentButton("📊 Esporta CSV")
        btn_h_csv.clicked.connect(self._export_csv)
        actions_row1.addWidget(btn_h_csv)
        actions_row1.addStretch(1)
        actions_vbox.addLayout(actions_row1)

        actions_row2 = QtWidgets.QHBoxLayout()
        actions_row2.setSpacing(8)
        btn_h_reproc = _FluentButton("🔄 Riprocessa")
        btn_h_reproc.clicked.connect(self._reprocess_selected)
        actions_row2.addWidget(btn_h_reproc)

        self.btn_hist_delete = _FluentButton("Elimina transazione")
        self.btn_hist_delete.setProperty("btnRole", "danger")
        self.btn_hist_delete.setEnabled(False)
        self.btn_hist_delete.clicked.connect(self._on_history_delete)
        actions_row2.addWidget(self.btn_hist_delete)

        actions_row2.addStretch(1)
        btn_h_log = _FluentButton("📥 Importa storico dal log")
        btn_h_log.clicked.connect(self._import_log)
        actions_row2.addWidget(btn_h_log)
        actions_vbox.addLayout(actions_row2)

        layout.addLayout(actions_vbox)

    def _today(self) -> None:
        self.var_day.set(date.today().isoformat())
        self.var_all_days.set(False)
        self._load_history()

    def _shift_day(self, delta: int) -> None:
        try:
            current = date.fromisoformat(str(self.var_day.get()).strip())
        except ValueError:
            current = date.today()
        self.var_day.set((current + timedelta(days=delta)).isoformat())
        self.var_all_days.set(False)
        self._load_history()

    def _load_history(self) -> None:
        status_value = dict(HISTORY_STATUS_FILTERS).get(str(self.var_status.get()))
        day_str = str(self.var_day.get()).strip()
        from_str = str(self.var_from.get()).strip()
        to_str = str(self.var_to.get()).strip()
        day = None if bool(self.var_all_days.get()) or from_str or to_str else day_str
        try:
            rows = self.registry.list_history(
                day=day or None,
                date_from=from_str or None,
                date_to=to_str or None,
                status=status_value,
                text=str(self.var_text.get()).strip() or None,
            )
            self._last_history_signature = self.registry.jobs_signature()
        except Exception as exc:
            self._error("Storico non disponibile", str(exc))
            return
        prev_selected = self._selected_history_id()
        tree = self.history_tree
        tree.delete(*tree.get_children())
        if hasattr(self, "btn_hist_delete"):
            self.btn_hist_delete.state(["disabled"])
        self._history_rows = {}
        groups: dict[str, list[dict[str, object]]] = {}
        for row in rows:
            started = parse_iso(row.get("call_started_at")) or parse_iso(row.get("created_at"))
            key = started.strftime("%Y-%m-%d") if started else "Senza data"
            groups.setdefault(key, []).append(row)
        for day_key, day_rows in groups.items():
            parent = tree.insert("", "end", iid=f"day:{day_key}", text=f"{day_key} ({len(day_rows)})", open=True)
            for row in day_rows:
                session_id = str(row["session_id"])
                self._history_rows[session_id] = row
                started = parse_iso(row.get("call_started_at"))
                docx_path = row.get("docx_path")
                tree.insert(
                    parent,
                    "end",
                    iid=session_id,
                    text="",
                    values=(
                        started.strftime("%d/%m/%Y %H:%M") if started else "-",
                        row.get("title") or "Riunione Teams",
                        format_duration(_elapsed(row.get("call_started_at"), row.get("call_ended_at")))
                        if row.get("call_ended_at")
                        else "-",
                        format_duration(
                            _elapsed(row.get("processing_started_at"), row.get("processing_ended_at"))
                        )
                        if row.get("processing_started_at") and row.get("processing_ended_at")
                        else "-",
                        JOB_STATUS_LABELS_IT.get(str(row.get("status")), str(row.get("status"))),
                        row.get("llm_model") or "-",
                        Path(str(docx_path)).name if docx_path else "-",
                    ),
                )
        if prev_selected and prev_selected in self._history_rows and tree.exists(prev_selected):
            tree.selection_set(prev_selected)
            tree.see(prev_selected)
            self._on_history_select()

    def _selected_history_id(self) -> str | None:
        selection = self.history_tree.selection()
        if not selection or selection[0].startswith("day:"):
            return None
        return str(selection[0])

    def _on_history_select(self, _event: object = None) -> None:
        sid = self._selected_history_id()
        if hasattr(self, "btn_hist_delete"):
            if sid is not None:
                self.btn_hist_delete.state(["!disabled"])
            else:
                self.btn_hist_delete.state(["disabled"])

    def _on_history_context_menu(self, pos: QtCore.QPoint) -> None:
        item = self.history_tree.itemAt(pos)
        if item is not None:
            iid = str(item.data(0, Qt.ItemDataRole.UserRole) or "")
            if iid and not iid.startswith("day:"):
                self.history_tree.selection_set(iid)
                self._on_history_select()
                self._history_menu.exec(self.history_tree.viewport().mapToGlobal(pos))

    def _on_history_delete(self) -> None:
        sid = self._selected_history_id()
        if sid is not None:
            self._delete_transaction(sid, self._history_rows.get(sid))

    def _is_background_alive(self) -> bool:
        status = self.registry.get_status()
        heartbeat = parse_iso(status.get("heartbeat"))
        return heartbeat is not None and (now_tz() - heartbeat).total_seconds() < HEARTBEAT_STALE_SEC

    def _delete_transaction(
        self,
        session_id: str,
        row: dict[str, object] | None = None,
    ) -> None:
        info = row or self.registry.get_job(session_id) or {}
        title = str(info.get("title") or session_id)
        status = str(info.get("status") or "")
        docx_path = info.get("docx_path")
        session_dir = info.get("session_dir")

        details: list[str] = ["• La voce e la cronologia eventi nel registro (Storico e Cruscotto)"]
        if status in ("recording", "queued", "processing"):
            details.append("• L'elaborazione o registrazione attualmente in corso verrà interrotta")
        details.append(f"• I file temporanei di sessione (session_{session_id})")
        if isinstance(docx_path, str) and docx_path:
            details.append(f"• Il documento Word generato ({Path(docx_path).name})")

        msg = (
            f"Vuoi eliminare definitivamente la transazione «{title}» ({session_id})?\n\n"
            "Verranno rimossi in modo irreversibile:\n"
            + "\n".join(details)
        )
        if not self._confirm("Elimina completamente la transazione", msg):
            return

        if self._is_background_alive():
            self._send(
                UiCommandType.DELETE_JOB.value,
                {"session_id": session_id},
            )
            if status not in ("recording", "queued", "processing"):
                from modules.orchestrator import purge_session_files

                self.registry.delete_job(session_id)
                purge_session_files(
                    session_id=session_id,
                    session_dir=str(session_dir) if session_dir else None,
                    docx_path=str(docx_path) if docx_path else None,
                )
        else:
            from modules.orchestrator import purge_session_files

            self.registry.delete_job(session_id)
            purge_session_files(
                session_id=session_id,
                session_dir=str(session_dir) if session_dir else None,
                docx_path=str(docx_path) if docx_path else None,
            )

        self._active_jobs.pop(session_id, None)
        self._history_rows.pop(session_id, None)
        if self.active_tree.exists(session_id):
            self.active_tree.delete(session_id)
        if self.history_tree.exists(session_id):
            self.history_tree.delete(session_id)
        self._on_active_select()
        self._load_history()

    def _on_toggle_incognito_quick(self) -> None:
        enabled = bool(self.incognito_var.get())
        updated = _dc_replace(
            self.settings,
            detection=_dc_replace(self.settings.detection, incognito_mode=enabled),
        )
        save_settings(updated)
        self.settings = updated
        apply_to_config(updated)
        if ("detection", "incognito_mode") in self._setting_vars:
            self._setting_vars[("detection", "incognito_mode")].set(enabled)
        if self._is_background_alive():
            self._send(
                UiCommandType.TOGGLE_INCOGNITO.value,
                {"enabled": enabled},
            )

    def _reprocess_selected(self) -> None:
        session_id = self._selected_history_id()
        row = self._history_rows.get(session_id or "")
        session_dir = row.get("session_dir") if row else None
        if not session_id or not isinstance(session_dir, str) or not (Path(session_dir) / "state.json").is_file():
            self._info("Riprocessa", "La cartella della sessione non esiste più: non è possibile riprocessarla.")
            return
        self._send(UiCommandType.RETRY_JOB.value, {"session_id": session_id})
        self._info("Riprocessa", "Richiesta inviata all'applicazione.")

    def _show_timeline(self) -> None:
        session_id = self._selected_history_id() or self._selected_active
        if not session_id:
            return
        row = self._history_rows.get(session_id) or self._active_jobs.get(session_id) or {}
        dlg = QtWidgets.QDialog(self)
        dlg.setWindowTitle(f"Timeline - {row.get('title') or session_id}")
        dlg.resize(640, 440)
        dlg_layout = QtWidgets.QVBoxLayout(dlg)
        dlg_layout.setContentsMargins(12, 12, 12, 12)

        tree = _FluentTreeWidget(show_tree_column=False, parent=dlg)
        tree.configure_columns(("stage", "duration"), ("Fase", "Durata"), (400, 180))
        for idx, (stage, seconds) in enumerate(self.registry.stage_durations(session_id)):
            tree.insert("", "end", iid=f"step_{idx}", values=(_stage_label(stage), format_duration(seconds)))
        dlg_layout.addWidget(tree, 1)

        if row.get("error"):
            err_lbl = QtWidgets.QLabel(f"Errore: {row.get('error')}")
            err_lbl.setProperty("labelRole", "warn")
            err_lbl.setWordWrap(True)
            dlg_layout.addWidget(err_lbl)
        dlg.exec()

    def _export_csv(self) -> None:
        target, _ = QtWidgets.QFileDialog.getSaveFileName(
            self,
            "Esporta CSV",
            f"storico_riunioni_{date.today().isoformat()}.csv",
            "CSV (*.csv)",
        )
        if not target:
            return
        JobRegistry.export_csv(list(self._history_rows.values()), Path(target))
        self._info("Esporta CSV", f"Esportate {len(self._history_rows)} righe.")

    def _import_log(self) -> None:
        def _work() -> object:
            self.registry.set_meta("log_import_done", "0")
            return self.registry.import_history_from_log(output_dir=self.output_dir)

        def _done(result: object) -> None:
            if isinstance(result, Exception):
                self._error("Importazione non riuscita", str(result))
                return
            self._info("Importazione completata", f"Importate {result} sessioni dal log.")
            self.var_all_days.set(True)
            self._load_history()

        _run_in_background(self, _work, _done)

    # ========================================================== impostazioni
    def _build_settings_tab(self) -> None:
        outer_layout = QtWidgets.QVBoxLayout(self.tab_settings)
        outer_layout.setContentsMargins(6, 4, 6, 6)
        outer_layout.setSpacing(6)

        scroll = QtWidgets.QScrollArea(self.tab_settings)
        scroll.setWidgetResizable(True)
        scroll.viewport().setObjectName("settingsViewport")
        content = QtWidgets.QWidget()
        content.setObjectName("settingsScrollContent")
        content_layout = QtWidgets.QVBoxLayout(content)
        content_layout.setContentsMargins(8, 4, 8, 8)
        content_layout.setSpacing(8)

        self._help_labels.clear()
        settings = self.settings
        for group_field in fields(settings):
            group_name = group_field.name
            group = getattr(settings, group_name)
            box = QtWidgets.QGroupBox(SETTINGS_GROUP_LABELS.get(group_name, group_name))
            grid = QtWidgets.QGridLayout(box)
            grid.setContentsMargins(10, 8, 10, 8)
            grid.setHorizontalSpacing(14)
            grid.setVerticalSpacing(6)
            grid.setColumnStretch(2, 1)

            for row_index, item in enumerate(fields(group)):
                spec = dict(item.metadata)
                label_text = str(spec.get("label", item.name)) + (" (riavvio)" if spec.get("restart") else "")
                lbl_field = QtWidgets.QLabel(label_text)
                grid.addWidget(lbl_field, row_index, 0, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)

                value = getattr(group, item.name)
                key = (group_name, item.name)

                if group_name == "output" and item.name == "glossary":
                    glossary_edit = _GlossaryEdit()
                    glossary_edit.setFixedHeight(52)
                    glossary_edit.setMinimumWidth(260)
                    glossary_edit.setPlainText(str(value))
                    grid.addWidget(glossary_edit, row_index, 1, Qt.AlignmentFlag.AlignLeft)
                    self._glossary_text = glossary_edit
                elif isinstance(value, bool):
                    var = _ReactiveVar(value)
                    chk = var.bind_CheckBox(QtWidgets.QCheckBox())
                    grid.addWidget(chk, row_index, 1, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
                    self._setting_vars[key] = var
                elif group_name == "ollama" and item.name == "model":
                    var = _ReactiveVar(str(value))
                    model_wrap = QtWidgets.QWidget()
                    model_row = QtWidgets.QHBoxLayout(model_wrap)
                    model_row.setContentsMargins(0, 0, 0, 0)
                    model_row.setSpacing(6)
                    combo = QtWidgets.QComboBox()
                    combo.setEditable(True)
                    combo.setMinimumWidth(185)
                    combo.setMinimumHeight(26)
                    combo.addItem(str(value))
                    var.bind_ComboBox(combo)
                    self._model_combo = combo
                    btn_refresh_models = _FluentButton("Aggiorna elenco")
                    btn_refresh_models.clicked.connect(lambda: self._refresh_models(silent=False))
                    model_row.addWidget(combo)
                    model_row.addWidget(btn_refresh_models)
                    grid.addWidget(model_wrap, row_index, 1, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
                    self._setting_vars[key] = var
                elif spec.get("choices"):
                    var = _ReactiveVar(str(value))
                    combo = QtWidgets.QComboBox()
                    combo.setMinimumWidth(175)
                    combo.setMinimumHeight(26)
                    combo.addItems([str(c) for c in spec["choices"]])
                    var.bind_ComboBox(combo)
                    grid.addWidget(combo, row_index, 1, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
                    self._setting_vars[key] = var
                else:
                    var = _ReactiveVar(str(value))
                    edit = var.bind_LineEdit(QtWidgets.QLineEdit())
                    edit.setMinimumWidth(260 if isinstance(value, str) else 100)
                    edit.setMinimumHeight(26)
                    grid.addWidget(edit, row_index, 1, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
                    self._setting_vars[key] = var

                help_text = spec.get("help")
                if help_text:
                    lbl_help = QtWidgets.QLabel(str(help_text))
                    lbl_help.setProperty("labelRole", "help")
                    lbl_help.setWordWrap(True)
                    lbl_help.setContentsMargins(0, 1, 0, 1)
                    grid.addWidget(lbl_help, row_index, 2, Qt.AlignmentFlag.AlignVCenter)
                    self._help_labels.append(lbl_help)

            content_layout.addWidget(box)

        content_layout.addStretch(1)
        scroll.setWidget(content)
        outer_layout.addWidget(scroll, 1)

        bar = QtWidgets.QHBoxLayout()
        bar.setContentsMargins(6, 4, 6, 4)
        bar.setSpacing(8)
        btn_save = _FluentButton("💾 Salva")
        btn_save.setProperty("btnRole", "primary")
        btn_save.clicked.connect(self._save_settings)
        bar.addWidget(btn_save)

        btn_defaults = _FluentButton("Ripristina default")
        btn_defaults.clicked.connect(self._restore_defaults)
        bar.addWidget(btn_defaults)

        btn_test_ollama = _FluentButton("🔌 Test Ollama")
        btn_test_ollama.clicked.connect(self._test_ollama)
        bar.addWidget(btn_test_ollama)

        btn_gpu = _FluentButton("🖥️ Verifica GPU")
        btn_gpu.clicked.connect(self._check_gpu)
        bar.addWidget(btn_gpu)

        btn_shortcut = _FluentButton("📌 Crea icona sul Desktop")
        btn_shortcut.clicked.connect(self._cmd_create_desktop_shortcut)
        bar.addWidget(btn_shortcut)
        bar.addStretch(1)

        lbl_restart_note = QtWidgets.QLabel(
            "I campi marcati (riavvio) hanno effetto al prossimo avvio dell'applicazione."
        )
        lbl_restart_note.setProperty("labelRole", "help")
        bar.addWidget(lbl_restart_note)
        outer_layout.addLayout(bar)

    def rendered_help_texts(self) -> list[str]:
        """Restituisce i testi di aiuto effettivamente renderizzati nella scheda Impostazioni."""
        return [lbl.text() for lbl in self._help_labels if lbl.text()]

    def _collect_settings_payload(self) -> dict[str, object]:
        payload: dict[str, object] = {}
        for (group_name, field_name), var in self._setting_vars.items():
            group_payload = payload.setdefault(group_name, {})
            assert isinstance(group_payload, dict)
            group_payload[field_name] = var.get()
        if self._glossary_text is not None:
            group_payload = payload.setdefault("output", {})
            assert isinstance(group_payload, dict)
            group_payload["glossary"] = self._glossary_text.get("1.0", "end").strip()
        return payload

    def _apply_settings_to_form(self, settings: AppSettings) -> None:
        data = settings_to_dict(settings)
        for (group_name, field_name), var in self._setting_vars.items():
            group = data.get(group_name)
            if isinstance(group, dict) and field_name in group:
                var.set(group[field_name])
        if self._glossary_text is not None:
            self._glossary_text.delete("1.0", "end")
            self._glossary_text.insert("1.0", settings.output.glossary)

    def _save_settings(self) -> None:
        new_settings, problems = settings_from_dict(self._collect_settings_payload(), self.settings)
        if problems:
            self._error("Impostazioni non valide", "\n".join(problems))
            return
        restart = restart_required_fields(self.settings, new_settings)
        try:
            save_settings(new_settings)
        except OSError as exc:
            self._error("Salvataggio non riuscito", str(exc))
            return
        self.settings = new_settings
        self.output_dir = resolve_output_dir(new_settings)
        self.incognito_var.set(bool(new_settings.detection.incognito_mode))
        self._send("reload_settings")
        message = "Impostazioni salvate: valgono dalla prossima call o elaborazione."
        if restart:
            message += "\n\nRichiedono il riavvio dell'applicazione:\n- " + "\n- ".join(restart)
        self._info("Impostazioni", message)

    def _restore_defaults(self) -> None:
        if self._confirm(
            "Ripristina default",
            "Ripristinare i valori predefiniti nel modulo? (premere Salva per confermarli)",
        ):
            self._apply_settings_to_form(default_settings())

    def _current_ollama_url(self) -> str:
        var = self._setting_vars.get(("ollama", "base_url"))
        return str(var.get()).strip() if var is not None else self.settings.ollama.base_url

    def _refresh_models(self, silent: bool = False) -> None:
        from bootstrap import _fetch_ollama_models

        url = self._current_ollama_url()

        def _done(result: object) -> None:
            if isinstance(result, list) and self._model_combo is not None:
                current_text = self._model_combo.currentText()
                self._model_combo.blockSignals(True)
                self._model_combo.clear()
                self._model_combo.addItems([str(m) for m in result])
                self._model_combo.setEditText(current_text)
                self._model_combo.blockSignals(False)
            elif not silent:
                self._error("Ollama", f"Ollama non raggiungibile su {url}.")

        _run_in_background(self, lambda: _fetch_ollama_models(url, timeout=3.0), _done)

    def _test_ollama(self) -> None:
        from bootstrap import _fetch_ollama_models, _is_model_available

        url = self._current_ollama_url()
        model_var = self._setting_vars.get(("ollama", "model"))
        model = str(model_var.get()).strip() if model_var is not None else self.settings.ollama.model

        def _done(result: object) -> None:
            if not isinstance(result, list):
                self._error("Test Ollama", f"Ollama non raggiungibile su {url}.")
                return
            present = _is_model_available(result, model)
            self._info(
                "Test Ollama",
                f"Ollama raggiungibile su {url}.\nModelli installati: {len(result)}.\n"
                f"Modello {model}: {'presente' if present else 'NON installato (ollama pull ' + model + ')'}.",
            )

        _run_in_background(self, lambda: _fetch_ollama_models(url, timeout=3.0), _done)

    def _check_gpu(self) -> None:
        from bootstrap import probe_ollama_gpu

        url = self._current_ollama_url()
        model_var = self._setting_vars.get(("ollama", "model"))
        model = str(model_var.get()).strip() if model_var is not None else self.settings.ollama.model

        def _done(result: object) -> None:
            if not isinstance(result, dict) or not result.get("reachable"):
                self._error("Verifica GPU", f"Ollama non raggiungibile su {url}.")
                return
            percent = result.get("gpu_percent")
            if percent is None:
                text = (
                    "Nessun modello è caricato in memoria in questo momento: la quota su GPU si "
                    "misura dopo la prima inferenza (o con 'ollama run <modello>')."
                )
            else:
                text = f"Il modello caricato è in VRAM al {percent}%."
            hint = result.get("hint")
            if hint:
                text += f"\n\n{hint}"
            self._info("Verifica GPU", text)

        _run_in_background(self, lambda: probe_ollama_gpu(url, model), _done)

    # ================================================================ utility
    def _confirm(self, title: str, message: str) -> bool:
        ans = QtWidgets.QMessageBox.question(
            self,
            title,
            message,
            QtWidgets.QMessageBox.StandardButton.Yes | QtWidgets.QMessageBox.StandardButton.No,
            QtWidgets.QMessageBox.StandardButton.No,
        )
        return ans == QtWidgets.QMessageBox.StandardButton.Yes

    def _cmd_create_desktop_shortcut(self) -> None:
        try:
            from tools.create_shortcut import create_desktop_shortcut

            shortcut = create_desktop_shortcut()
            self._info("Collegamento Desktop", f"Icona creata con successo sul Desktop:\n{shortcut.name}")
        except Exception as exc:
            self._error("Collegamento Desktop", f"Impossibile creare il collegamento sul Desktop: {exc}")

    # ============================================== aggiornamenti e riavvio
    def _dismiss_update_banner(self) -> None:
        self._update_banner_dismissed = True
        if hasattr(self, "update_banner"):
            self.update_banner.setVisible(False)

    def _cmd_check_updates(self) -> None:
        self._update_banner_dismissed = False
        self._check_updates(silent=False)

    def _check_updates(self, *, silent: bool = False) -> None:
        if self._checking_updates or self._installing_update:
            return
        self._checking_updates = True
        if hasattr(self, "btn_check_updates"):
            self.btn_check_updates.setEnabled(False)
            self.btn_check_updates.setText("⏳ Verifica...")

        def _work() -> UpdateInfo:
            return check_for_updates(current_version=APP_VERSION)

        def _done(result: object) -> None:
            self._checking_updates = False
            if hasattr(self, "btn_check_updates"):
                self.btn_check_updates.setEnabled(True)
                self.btn_check_updates.setText("🔄 Verifica aggiornamenti")
            if isinstance(result, UpdateInfo):
                self._apply_update_info(result, show_up_to_date_dialog=not silent)
            elif not silent:
                self._error("Verifica aggiornamenti", f"Errore imprevisto: {result}")

        _run_in_background(self, _work, _done)

    def _apply_update_info(self, info: UpdateInfo, *, show_up_to_date_dialog: bool = False) -> None:
        self._update_info = info
        if info.error is None:
            try:
                self.registry.set_meta("update_info", json.dumps(info.to_dict(), ensure_ascii=False))
            except Exception as exc:
                logger.debug("Salvataggio update_info su SQLite non riuscito: %s", exc)

        if self._update_installed_version:
            return

        if info.update_available:
            self.var_version.set(f"Versione: v{APP_VERSION} (disponibile v{info.latest_version})")
            self.var_update_banner.set(
                f"⬆️ Nuova versione disponibile su GitHub: v{info.latest_version} "
                f"(versione installata: v{info.current_version})"
            )
            self.update_progress_bar.setVisible(False)
            self.btn_release_notes.setVisible(True)
            self.btn_install_update.setVisible(True)
            self.btn_install_update.setEnabled(True)
            self.btn_restart_after_update.setVisible(False)
            if not self._update_banner_dismissed:
                self.update_banner.setVisible(True)

            if show_up_to_date_dialog:
                notes_preview = (info.release_notes or "").strip()
                if len(notes_preview) > 400:
                    notes_preview = notes_preview[:400].rstrip() + "..."
                msg = (
                    f"È disponibile una nuova versione di {APP_NAME}: v{info.latest_version}\n"
                    f"Versione attualmente in uso: v{info.current_version}"
                )
                if notes_preview:
                    msg += f"\n\nNovità principali:\n{notes_preview}"
                msg += "\n\nVuoi scaricare e installare subito l'aggiornamento?"
                if self._confirm("Aggiornamento Disponibile", msg):
                    self._cmd_install_update()
        else:
            self.var_version.set(f"Versione: v{APP_VERSION}")
            self.update_banner.setVisible(False)
            if show_up_to_date_dialog:
                if info.error:
                    self._error(
                        "Verifica aggiornamenti",
                        f"Impossibile verificare la presenza di aggiornamenti su GitHub:\n{info.error}",
                    )
                else:
                    self._info(
                        "Verifica aggiornamenti",
                        f"Stai già utilizzando l'ultima versione disponibile ({APP_NAME} v{APP_VERSION}).",
                    )

    def _show_release_notes(self) -> None:
        info = self._update_info
        ver = info.latest_version if info is not None else APP_VERSION
        notes = (info.release_notes if info is not None else "").strip()
        if not notes:
            changelog_file = PROJECT_ROOT / "CHANGELOG.md"
            if changelog_file.is_file():
                try:
                    raw_cl = changelog_file.read_text(encoding="utf-8")
                    notes = extract_changelog_section(raw_cl, ver) or raw_cl
                except OSError:
                    notes = ""
        if not notes:
            notes = f"Note di rilascio per la versione v{ver} non disponibili."

        dlg = QtWidgets.QDialog(self)
        dlg.setWindowTitle(f"Note di Rilascio - {APP_NAME} v{ver}")
        dlg.resize(620, 420)
        vbox = QtWidgets.QVBoxLayout(dlg)
        vbox.setContentsMargins(12, 12, 12, 12)
        vbox.setSpacing(8)

        header_lbl = QtWidgets.QLabel(f"Novità della versione v{ver} (versione corrente: v{APP_VERSION})")
        header_lbl.setProperty("labelRole", "updateTitle")
        vbox.addWidget(header_lbl)

        txt = QtWidgets.QPlainTextEdit(dlg)
        txt.setReadOnly(True)
        txt.setPlainText(notes)
        vbox.addWidget(txt, 1)

        btn_row = QtWidgets.QHBoxLayout()
        btn_row.addStretch(1)
        if info is not None and info.update_available and not self._update_installed_version:
            btn_dl = _FluentButton("⬇️ Scarica e installa ora")
            btn_dl.setProperty("btnRole", "primary")

            def _accept_and_install() -> None:
                dlg.accept()
                self._cmd_install_update()

            btn_dl.clicked.connect(_accept_and_install)
            btn_row.addWidget(btn_dl)

        btn_close = _FluentButton("Chiudi")
        btn_close.clicked.connect(dlg.close)
        btn_row.addWidget(btn_close)
        vbox.addLayout(btn_row)
        dlg.exec()

    def _on_update_progress(self, pct: int, msg: str) -> None:
        if hasattr(self, "update_progress_bar"):
            self.update_progress_bar.setVisible(True)
            self.update_progress_bar.setValue(max(0, min(100, int(pct))))
        if hasattr(self, "var_update_banner") and msg:
            self.var_update_banner.set(f"⏳ {msg} ({pct}%)")

    def _cmd_install_update(self) -> None:
        info = self._update_info
        if info is None or not info.update_available or self._installing_update:
            return
        self._installing_update = True
        self._update_banner_dismissed = False
        self.update_banner.setVisible(True)
        self.btn_install_update.setEnabled(False)
        if hasattr(self, "btn_check_updates"):
            self.btn_check_updates.setEnabled(False)
        self.update_progress_bar.setValue(0)
        self.update_progress_bar.setVisible(True)
        self.var_update_banner.set(f"⏳ Avvio download di {APP_NAME} v{info.latest_version}...")

        def _progress_cb(pct: int, msg: str) -> None:
            try:
                self._bg_bridge.progress.emit(self._on_update_progress, pct, msg)
            except RuntimeError:
                pass

        def _work() -> UpdateResult:
            return download_and_apply_update(
                info,
                project_root=PROJECT_ROOT,
                progress_callback=_progress_cb,
            )

        def _done(result: object) -> None:
            self._installing_update = False
            if hasattr(self, "btn_check_updates"):
                self.btn_check_updates.setEnabled(True)

            if isinstance(result, UpdateResult) and result.success:
                self._update_installed_version = result.installed_version
                self.update_progress_bar.setVisible(False)
                self.btn_install_update.setVisible(False)
                self.btn_restart_after_update.setVisible(True)
                self.btn_restart_after_update.setEnabled(True)
                self.var_update_banner.set(
                    f"✅ Aggiornamento a {APP_NAME} v{result.installed_version} installato con successo! "
                    "Clicca 'Riavvia ora' per applicare la nuova versione."
                )
                self.var_version.set(
                    f"Versione: v{APP_VERSION} → v{result.installed_version} (riavvio richiesto)"
                )
                try:
                    self.registry.set_meta("update_info", "")
                except Exception:
                    pass
                if self._confirm(
                    "Aggiornamento Completato",
                    f"{result.message}\n\n"
                    f"Vuoi riavviare {APP_NAME} adesso per attivare subito la versione v{result.installed_version}?",
                ):
                    self._cmd_restart_app(skip_confirm=True)
            else:
                self.update_progress_bar.setVisible(False)
                self.btn_install_update.setEnabled(True)
                err_msg = result.message if isinstance(result, UpdateResult) else str(result)
                self.var_update_banner.set(
                    f"⚠️ Aggiornamento a v{info.latest_version} non riuscito. Riprova."
                )
                self._error("Errore aggiornamento", err_msg)

        _run_in_background(self, _work, _done)

    def _has_active_recording(self) -> bool:
        for job in self._active_jobs.values():
            st = str(job.get("status") or "").lower()
            stg = str(job.get("stage") or "").lower()
            if st in ("recording", "paused") or stg in (
                Stage.RECORDING.value,
                Stage.RECORDING_AND_TRANSCRIBING.value,
                Stage.PAUSED.value,
                Stage.GRACE_PERIOD.value,
            ):
                return True
        return False

    def _cmd_restart_app(self, *, skip_confirm: bool = False) -> None:
        if self._has_active_recording():
            if not self._confirm(
                "Registrazione in corso",
                "Attenzione: c'è una registrazione di chiamata attualmente attiva.\n\n"
                "Se riavvii ora, la registrazione verrà interrotta e la sessione verrà "
                "recuperata automaticamente al riavvio.\n\n"
                "Vuoi procedere comunque con il riavvio?",
            ):
                return
        elif not skip_confirm:
            if not self._confirm(
                "Riavvia applicazione",
                f"Vuoi riavviare {APP_NAME} (servizio in background e Cruscotto)?",
            ):
                return

        if hasattr(self, "btn_restart_app"):
            self.btn_restart_app.setEnabled(False)
            self.btn_restart_app.setText("⏳ Riavvio...")
        if hasattr(self, "btn_restart_after_update"):
            self.btn_restart_after_update.setEnabled(False)
        if hasattr(self, "_timer"):
            self._timer.stop()

        def _work() -> bool:
            stop_background_instance(self.registry, timeout_sec=6.0)
            spawn_restarted_processes(
                project_root=PROJECT_ROOT,
                start_background=True,
                reopen_dashboard=True,
                delay_sec=1.0,
            )
            return True

        def _done(_result: object) -> None:
            self.close()
            qapp = QtWidgets.QApplication.instance()
            if qapp is not None:
                qapp.quit()

        _run_in_background(self, _work, _done)

    def _info(self, title: str, message: str) -> None:
        QtWidgets.QMessageBox.information(self, title, message)

    def _error(self, title: str, message: str) -> None:
        QtWidgets.QMessageBox.critical(self, title, message)


def run_dashboard(initial_tab: str | None = None) -> int:
    """Entry point di `main.py --dashboard`: istanza singola PySide6, nessun processo audio coinvolto."""
    if sys.platform == "win32":
        try:
            import ctypes

            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
                f"{APP_NAME}.MeetingScribe.{APP_VERSION}"
            )
        except Exception:
            pass

    instance = SingleInstance(DASHBOARD_MUTEX_NAME)
    if not instance.acquire():
        focus_window_by_title(DASHBOARD_TITLE)
        return EXIT_SUCCESS
    try:
        qapp = _ensure_qt_app()
        qapp.setApplicationName(APP_NAME)
        qapp.setApplicationVersion(APP_VERSION)
        if APP_ICON_PATH.exists():
            qapp.setWindowIcon(QtGui.QIcon(str(APP_ICON_PATH)))

        registry = JobRegistry()
        window = DashboardApp(None, registry, initial_tab=initial_tab)
        window.setWindowTitle(DASHBOARD_TITLE)
        window.show()
        qapp.exec()
        logger.info("Cruscotto %s v%s chiuso.", APP_NAME, APP_VERSION)
        return EXIT_SUCCESS
    except ImportError:
        sys.stderr.write(
            "Il cruscotto richiede PySide6, assente in questa installazione di Python.\n"
            "Eseguire: pip install PySide6-Essentials\n"
        )
        return EXIT_MISSING_DEPENDENCY
    finally:
        instance.release()
