from __future__ import annotations

import os
import sys
import threading
from collections.abc import Callable
from dataclasses import fields
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from config import APP_ICON_PATH, APP_NAME, APP_VERSION, EXIT_MISSING_DEPENDENCY, EXIT_SUCCESS
from modules.contracts import Stage, UiCommandType, now_tz
from modules.job_registry import JOB_STATUS_LABELS_IT, JobRegistry, parse_iso
from modules.logging_setup import get_logger
from modules.settings import (
    SETTINGS_GROUP_LABELS,
    AppSettings,
    default_settings,
    load_settings,
    resolve_output_dir,
    restart_required_fields,
    save_settings,
    settings_from_dict,
    settings_to_dict,
)
from modules.single_instance import DASHBOARD_MUTEX_NAME, SingleInstance, focus_window_by_title
from modules.tray_ui import STAGE_LABELS_IT

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
)


def _stage_label(stage: object) -> str:
    if isinstance(stage, str):
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


def describe_progress(job: dict[str, object]) -> str:
    """Testo di avanzamento del job: MAP 3/6, token generati."""
    parts: list[str] = []
    current, total = job.get("progress_current"), job.get("progress_total")
    stage = str(job.get("stage") or "")
    if isinstance(current, int) and isinstance(total, int) and total > 0:
        prefix = "MAP " if stage == Stage.SUMMARIZING_MAP.value else ""
        parts.append(f"{prefix}{current}/{total}")
    tokens = job.get("tokens_generated")
    if isinstance(tokens, int) and tokens > 0:
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


def _run_in_background(root: Any, work: Callable[[], object], done: Callable[[object], None]) -> None:
    """Esegue work fuori dal thread Tk e consegna il risultato con root.after."""

    def _target() -> None:
        try:
            result: object = work()
        except Exception as exc:  # pragma: no cover - mostrato all'utente
            result = exc
        try:
            root.after(0, lambda: done(result))
        except RuntimeError:
            # Finestra già chiusa: il risultato non serve più
            pass

    threading.Thread(target=_target, daemon=True).start()


class DashboardApp:
    """Finestra del cruscotto: legge SQLite ogni secondo, scrive settings.json e i comandi per l'app."""

    def __init__(
        self,
        root: Any,
        registry: JobRegistry,
        *,
        initial_tab: str | None = None,
    ) -> None:
        import tkinter as tk
        from tkinter import ttk

        self.tk = tk
        self.ttk = ttk
        self.root = root
        self.registry = registry
        self.settings: AppSettings = load_settings()
        self.output_dir: Path = resolve_output_dir(self.settings)
        self._active_jobs: dict[str, dict[str, object]] = {}
        self._history_rows: dict[str, dict[str, object]] = {}
        self._selected_active: str | None = None
        self._setting_vars: dict[tuple[str, str], Any] = {}
        self._glossary_text: Any = None
        self._model_combo: Any = None

        root.title(DASHBOARD_TITLE)
        root.geometry("1180x720")
        root.minsize(940, 560)
        style = ttk.Style(root)
        if "vista" in style.theme_names():
            style.theme_use("vista")
        style.configure("Status.TLabel", font=("Segoe UI", 10))
        style.configure("Warn.TLabel", foreground="#B45309")
        style.configure("Step.TLabel", font=("Segoe UI", 10))

        self.notebook = ttk.Notebook(root)
        self.notebook.pack(fill="both", expand=True, padx=8, pady=8)
        self.tab_dashboard = ttk.Frame(self.notebook, padding=8)
        self.tab_history = ttk.Frame(self.notebook, padding=8)
        self.tab_settings = ttk.Frame(self.notebook, padding=8)
        self.notebook.add(self.tab_dashboard, text="Cruscotto")
        self.notebook.add(self.tab_history, text="Storico")
        self.notebook.add(self.tab_settings, text="Impostazioni")

        self._build_dashboard_tab()
        self._build_history_tab()
        self._build_settings_tab()

        tabs = {"cruscotto": 0, "storico": 1, "impostazioni": 2}
        if initial_tab in tabs:
            self.notebook.select(tabs[initial_tab])

        self._refresh()
        self._load_history()

    # ============================================================ cruscotto
    def _build_dashboard_tab(self) -> None:
        tk, ttk = self.tk, self.ttk
        frame = self.tab_dashboard

        status = ttk.LabelFrame(frame, text="Stato", padding=6)
        status.pack(fill="x")
        self.var_app = tk.StringVar(value="Applicazione: -")
        self.var_watcher = tk.StringVar(value="Watcher: -")
        self.var_gpu = tk.StringVar(value="LLM su GPU: -")
        self.var_models = tk.StringVar(value="Modelli: -")
        self.var_queue = tk.StringVar(value="Coda: -")
        for column, var in enumerate(
            (self.var_app, self.var_watcher, self.var_gpu, self.var_models, self.var_queue)
        ):
            ttk.Label(status, textvariable=var, style="Status.TLabel").grid(
                row=0, column=column, sticky="w", padx=(0, 18)
            )

        buttons = ttk.Frame(frame)
        buttons.pack(fill="x", pady=(8, 4))
        ttk.Button(buttons, text="Avvia registrazione manuale", command=self._cmd_start_manual).pack(side="left")
        ttk.Button(buttons, text="Termina call ora", command=self._cmd_finalize).pack(side="left", padx=4)
        self.btn_stop = ttk.Button(buttons, text="Arresta elaborazione", command=self._cmd_stop_selected)
        self.btn_stop.pack(side="left", padx=4)
        self.btn_stop.state(["disabled"])
        ttk.Button(buttons, text="Riprova", command=self._cmd_retry_selected).pack(side="left", padx=4)
        ttk.Button(buttons, text="Copia bozza mail", command=self._cmd_copy_email).pack(side="left", padx=4)
        ttk.Button(buttons, text="Apri DOCX", command=self._open_selected_docx).pack(side="left", padx=4)
        ttk.Button(buttons, text="Apri cartella", command=self._open_selected_folder).pack(side="left", padx=4)

        columns = ("title", "start", "status", "stage", "progress", "in_stage", "total")
        headings = ("Titolo", "Inizio", "Stato", "Fase", "Avanzamento", "Tempo in fase", "Tempo totale")
        widths = (300, 110, 170, 210, 140, 100, 100)
        self.active_tree = ttk.Treeview(frame, columns=columns, show="headings", height=8, selectmode="browse")
        for col, heading, width in zip(columns, headings, widths, strict=True):
            self.active_tree.heading(col, text=heading)
            self.active_tree.column(col, width=width, anchor="w")
        self.active_tree.pack(fill="x", pady=(4, 6))
        self.active_tree.bind("<<TreeviewSelect>>", lambda _e: self._on_active_select())

        detail = ttk.LabelFrame(frame, text="Dettaglio", padding=6)
        detail.pack(fill="both", expand=True)
        steps = ttk.Frame(detail)
        steps.pack(fill="x")
        self.step_vars: list[Any] = []
        for index, name in enumerate(PIPELINE_STEPS):
            var = tk.StringVar(value=f"○ {name}")
            self.step_vars.append(var)
            ttk.Label(steps, textvariable=var, style="Step.TLabel").grid(row=0, column=index * 2, padx=2)
            if index < len(PIPELINE_STEPS) - 1:
                ttk.Label(steps, text="→").grid(row=0, column=index * 2 + 1)
        self.var_error = tk.StringVar(value="")
        ttk.Label(detail, textvariable=self.var_error, style="Warn.TLabel", wraplength=1050).pack(
            fill="x", pady=(6, 2)
        )
        ttk.Label(detail, text="Ultimi eventi").pack(anchor="w")
        self.events_list = tk.Listbox(detail, height=10, font=("Consolas", 9))
        self.events_list.pack(fill="both", expand=True)

    def _send(self, command: str, payload: dict[str, object] | None = None) -> None:
        try:
            self.registry.enqueue_command(command, payload)
        except Exception as exc:
            self._error("Comando non inviato", str(exc))

    def _cmd_start_manual(self) -> None:
        self._send(UiCommandType.START_MANUAL.value)

    def _cmd_finalize(self) -> None:
        self._send(UiCommandType.FINALIZE_NOW.value)

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

        self.root.clipboard_clear()
        self.root.clipboard_append(text)
        self._info("Copia bozza mail", "Bozza email copiata negli appunti con successo!")

    def _cmd_retry_selected(self) -> None:
        session_id = self._selected_active or self._selected_history_id()
        if session_id is None:
            self._info("Riprova", "Selezionare prima una sessione in errore.")
            return
        self._send(UiCommandType.RETRY_JOB.value, {"session_id": session_id})
        self._info("Riprova", "Richiesta inviata: la sessione verrà rimessa in coda.")

    def _job_for_action(self) -> dict[str, object] | None:
        if self.notebook.index(self.notebook.select()) == 1:
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

    def _on_active_select(self) -> None:
        selection = self.active_tree.selection()
        self._selected_active = selection[0] if selection else None
        job = self._active_jobs.get(self._selected_active or "")
        status = str(job.get("status") or "") if job else ""
        if hasattr(self, "btn_stop"):
            if status in ("processing", "queued", "recording"):
                self.btn_stop.state(["!disabled"])
            else:
                self.btn_stop.state(["disabled"])
        self._refresh_detail()

    def _refresh(self) -> None:
        try:
            self._refresh_status()
            self._refresh_active_jobs()
            self._refresh_detail()
        except Exception as exc:
            logger.debug("Aggiornamento del cruscotto non riuscito: %s", exc)
        finally:
            self.root.after(REFRESH_INTERVAL_MS, self._refresh)

    def _refresh_status(self) -> None:
        status = self.registry.get_status()
        heartbeat = parse_iso(status.get("heartbeat"))
        alive = heartbeat is not None and (now_tz() - heartbeat).total_seconds() < HEARTBEAT_STALE_SEC
        self.var_app.set(f"Applicazione: {'attiva' if alive else 'non in esecuzione'}")

        watcher_stage = status.get("watcher_stage") if alive else None
        watcher_text = _stage_label(watcher_stage) if watcher_stage else "-"
        started = status.get("recording_started_at")
        if alive and isinstance(started, str) and watcher_stage in (
            Stage.RECORDING.value,
            Stage.RECORDING_AND_TRANSCRIBING.value,
            Stage.GRACE_PERIOD.value,
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
            if hasattr(self, "btn_stop"):
                self.btn_stop.state(["disabled"])
        elif hasattr(self, "btn_stop"):
            curr_job = self._active_jobs.get(self._selected_active)
            status = str(curr_job.get("status") or "") if curr_job else ""
            if status in ("processing", "queued", "recording"):
                self.btn_stop.state(["!disabled"])
            else:
                self.btn_stop.state(["disabled"])

    def _refresh_detail(self) -> None:
        job = self._active_jobs.get(self._selected_active or "")
        if job is None and self._active_jobs:
            job = next(iter(self._active_jobs.values()))
        if job is None:
            for index, name in enumerate(PIPELINE_STEPS):
                self.step_vars[index].set(f"○ {name}")
            self.var_error.set("Nessuna sessione attiva.")
            self.events_list.delete(0, "end")
            return
        marks = pipeline_marks(str(job.get("stage") or ""), str(job.get("status") or ""))
        for index, name in enumerate(PIPELINE_STEPS):
            self.step_vars[index].set(f"{marks[index]} {name}")
        error = job.get("error")
        retry_at = parse_iso(job.get("next_retry_at"))
        message = f"Errore: {error}" if error else ""
        if retry_at is not None:
            message += f"  — nuovo tentativo alle {retry_at.strftime('%H:%M:%S')}"
        if job.get("status") == "parked":
            message += "  — usare Riprova per rimetterla in coda"
        self.var_error.set(message.strip())
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
        tk, ttk = self.tk, self.ttk
        frame = self.tab_history
        filters = ttk.Frame(frame)
        filters.pack(fill="x")

        self.var_day = tk.StringVar(value=date.today().isoformat())
        self.var_all_days = tk.BooleanVar(value=False)
        self.var_from = tk.StringVar(value="")
        self.var_to = tk.StringVar(value="")
        self.var_status = tk.StringVar(value=HISTORY_STATUS_FILTERS[0][0])
        self.var_text = tk.StringVar(value="")

        ttk.Label(filters, text="Giorno").grid(row=0, column=0, sticky="w")
        ttk.Button(filters, text="◀", width=3, command=lambda: self._shift_day(-1)).grid(row=0, column=1)
        ttk.Entry(filters, textvariable=self.var_day, width=11).grid(row=0, column=2)
        ttk.Button(filters, text="▶", width=3, command=lambda: self._shift_day(1)).grid(row=0, column=3)
        ttk.Button(filters, text="Oggi", command=self._today).grid(row=0, column=4, padx=(4, 12))
        ttk.Checkbutton(filters, text="Tutti i giorni", variable=self.var_all_days, command=self._load_history).grid(
            row=0, column=5, padx=(0, 12)
        )
        ttk.Label(filters, text="Dal").grid(row=0, column=6)
        ttk.Entry(filters, textvariable=self.var_from, width=11).grid(row=0, column=7, padx=2)
        ttk.Label(filters, text="Al").grid(row=0, column=8)
        ttk.Entry(filters, textvariable=self.var_to, width=11).grid(row=0, column=9, padx=(2, 12))
        ttk.Label(filters, text="Esito").grid(row=0, column=10)
        ttk.Combobox(
            filters,
            textvariable=self.var_status,
            values=[label for label, _ in HISTORY_STATUS_FILTERS],
            state="readonly",
            width=20,
        ).grid(row=0, column=11, padx=(2, 12))
        ttk.Label(filters, text="Testo").grid(row=0, column=12)
        text_entry = ttk.Entry(filters, textvariable=self.var_text, width=18)
        text_entry.grid(row=0, column=13, padx=2)
        text_entry.bind("<Return>", lambda _e: self._load_history())
        ttk.Button(filters, text="Cerca", command=self._load_history).grid(row=0, column=14, padx=4)

        columns = ("time", "title", "call", "processing", "outcome", "model", "file")
        headings = ("Data", "Titolo", "Durata call", "Durata elaborazione", "Esito", "Modello", "File")
        widths = (130, 280, 90, 120, 170, 110, 260)
        self.history_tree = ttk.Treeview(frame, columns=columns, show="tree headings", selectmode="browse")
        self.history_tree.column("#0", width=150, anchor="w")
        self.history_tree.heading("#0", text="Giorno")
        for col, heading, width in zip(columns, headings, widths, strict=True):
            self.history_tree.heading(col, text=heading)
            self.history_tree.column(col, width=width, anchor="w")
        self.history_tree.pack(fill="both", expand=True, pady=6)
        self.history_tree.bind("<Double-1>", lambda _e: self._show_timeline())

        actions = ttk.Frame(frame)
        actions.pack(fill="x")
        ttk.Button(actions, text="Apri DOCX", command=self._open_selected_docx).pack(side="left")
        ttk.Button(actions, text="Copia bozza mail", command=self._cmd_copy_email).pack(side="left", padx=4)
        ttk.Button(actions, text="Apri cartella del giorno", command=self._open_selected_folder).pack(
            side="left", padx=4
        )
        ttk.Button(actions, text="Riprocessa", command=self._reprocess_selected).pack(side="left", padx=4)
        ttk.Button(actions, text="Timeline", command=self._show_timeline).pack(side="left", padx=4)
        ttk.Button(actions, text="Esporta CSV", command=self._export_csv).pack(side="left", padx=4)
        ttk.Button(actions, text="Importa storico dal log", command=self._import_log).pack(side="right")

    def _today(self) -> None:
        self.var_day.set(date.today().isoformat())
        self.var_all_days.set(False)
        self._load_history()

    def _shift_day(self, delta: int) -> None:
        try:
            current = date.fromisoformat(self.var_day.get().strip())
        except ValueError:
            current = date.today()
        self.var_day.set((current + timedelta(days=delta)).isoformat())
        self.var_all_days.set(False)
        self._load_history()

    def _load_history(self) -> None:
        status_value = dict(HISTORY_STATUS_FILTERS).get(self.var_status.get())
        day = None if self.var_all_days.get() or self.var_from.get() or self.var_to.get() else self.var_day.get().strip()
        try:
            rows = self.registry.list_history(
                day=day or None,
                date_from=self.var_from.get().strip() or None,
                date_to=self.var_to.get().strip() or None,
                status=status_value,
                text=self.var_text.get().strip() or None,
            )
        except Exception as exc:
            self._error("Storico non disponibile", str(exc))
            return
        tree = self.history_tree
        tree.delete(*tree.get_children())
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

    def _selected_history_id(self) -> str | None:
        selection = self.history_tree.selection()
        if not selection or selection[0].startswith("day:"):
            return None
        return str(selection[0])

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
        tk, ttk = self.tk, self.ttk
        row = self._history_rows.get(session_id) or self._active_jobs.get(session_id) or {}
        window = tk.Toplevel(self.root)
        window.title(f"Timeline - {row.get('title') or session_id}")
        window.geometry("620x420")
        tree = ttk.Treeview(window, columns=("stage", "duration"), show="headings")
        tree.heading("stage", text="Fase")
        tree.heading("duration", text="Durata")
        tree.column("stage", width=380)
        tree.column("duration", width=160)
        for stage, seconds in self.registry.stage_durations(session_id):
            tree.insert("", "end", values=(_stage_label(stage), format_duration(seconds)))
        tree.pack(fill="both", expand=True, padx=8, pady=8)
        if row.get("error"):
            ttk.Label(window, text=f"Errore: {row.get('error')}", style="Warn.TLabel", wraplength=580).pack(
                fill="x", padx=8, pady=(0, 8)
            )

    def _export_csv(self) -> None:
        from tkinter import filedialog

        target = filedialog.asksaveasfilename(
            parent=self.root,
            defaultextension=".csv",
            initialfile=f"storico_riunioni_{date.today().isoformat()}.csv",
            filetypes=[("CSV", "*.csv")],
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

        _run_in_background(self.root, _work, _done)

    # ========================================================== impostazioni
    def _build_settings_tab(self) -> None:
        tk, ttk = self.tk, self.ttk
        outer = self.tab_settings
        canvas = tk.Canvas(outer, highlightthickness=0)
        scrollbar = ttk.Scrollbar(outer, orient="vertical", command=canvas.yview)
        content = ttk.Frame(canvas)
        content_id = canvas.create_window((0, 0), window=content, anchor="nw")
        content.bind("<Configure>", lambda _e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.bind("<Configure>", lambda e: canvas.itemconfig(content_id, width=e.width))
        canvas.configure(yscrollcommand=scrollbar.set)
        canvas.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")
        # La rotellina scorre le impostazioni solo quando il puntatore è sopra il modulo
        canvas.bind(
            "<Enter>",
            lambda _e: canvas.bind_all(
                "<MouseWheel>", lambda e: canvas.yview_scroll(int(-e.delta / 120), "units")
            ),
        )
        canvas.bind("<Leave>", lambda _e: canvas.unbind_all("<MouseWheel>"))

        settings = self.settings
        for group_field in fields(settings):
            group_name = group_field.name
            group = getattr(settings, group_name)
            box = ttk.LabelFrame(content, text=SETTINGS_GROUP_LABELS.get(group_name, group_name), padding=8)
            box.pack(fill="x", padx=4, pady=4)
            box.columnconfigure(2, weight=1)
            for row_index, item in enumerate(fields(group)):
                spec = dict(item.metadata)
                label = str(spec.get("label", item.name))
                restart = bool(spec.get("restart"))
                ttk.Label(box, text=label + (" (riavvio)" if restart else "")).grid(
                    row=row_index, column=0, sticky="w", padx=(0, 12), pady=2
                )
                value = getattr(group, item.name)
                key = (group_name, item.name)
                if group_name == "output" and item.name == "glossary":
                    text = tk.Text(box, width=44, height=3, font=("Segoe UI", 9))
                    text.insert("1.0", str(value))
                    text.grid(row=row_index, column=1, sticky="w", pady=2)
                    self._glossary_text = text
                    help_text = spec.get("help")
                    if help_text:
                        ttk.Label(
                            box,
                            text=str(help_text),
                            foreground="#555555",
                            wraplength=480,
                            justify="left",
                        ).grid(row=row_index, column=2, sticky="w", padx=(12, 4), pady=2)
                    continue
                if isinstance(value, bool):
                    var: Any = tk.BooleanVar(value=value)
                    ttk.Checkbutton(box, variable=var).grid(row=row_index, column=1, sticky="w")
                elif group_name == "ollama" and item.name == "model":
                    var = tk.StringVar(value=str(value))
                    frame_model = ttk.Frame(box)
                    frame_model.grid(row=row_index, column=1, sticky="w", pady=2)
                    combo = ttk.Combobox(frame_model, textvariable=var, width=28)
                    combo.pack(side="left")
                    self._model_combo = combo
                    ttk.Button(frame_model, text="Aggiorna elenco", command=self._refresh_models).pack(
                        side="left", padx=(6, 0)
                    )
                elif spec.get("choices"):
                    var = tk.StringVar(value=str(value))
                    ttk.Combobox(
                        box, textvariable=var, values=list(spec["choices"]), state="readonly", width=24
                    ).grid(row=row_index, column=1, sticky="w")
                else:
                    var = tk.StringVar(value=str(value))
                    ttk.Entry(box, textvariable=var, width=44 if isinstance(value, str) else 14).grid(
                        row=row_index, column=1, sticky="w"
                    )
                help_text = spec.get("help")
                if help_text:
                    ttk.Label(
                        box,
                        text=str(help_text),
                        foreground="#555555",
                        wraplength=480,
                        justify="left",
                    ).grid(row=row_index, column=2, sticky="w", padx=(12, 4), pady=2)
                self._setting_vars[key] = var

        bar = ttk.Frame(content)
        bar.pack(fill="x", padx=4, pady=8)
        ttk.Button(bar, text="Salva", command=self._save_settings).pack(side="left")
        ttk.Button(bar, text="Ripristina default", command=self._restore_defaults).pack(side="left", padx=4)
        ttk.Button(bar, text="Test Ollama", command=self._test_ollama).pack(side="left", padx=(20, 4))
        ttk.Button(bar, text="Verifica GPU", command=self._check_gpu).pack(side="left", padx=4)
        ttk.Button(bar, text="Crea icona sul Desktop", command=self._cmd_create_desktop_shortcut).pack(side="left", padx=4)
        ttk.Label(bar, text="I campi marcati (riavvio) hanno effetto al prossimo avvio dell'applicazione.").pack(
            side="left", padx=12
        )
        self._refresh_models(silent=True)

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
        self._send("reload_settings")
        message = "Impostazioni salvate: valgono dalla prossima call o elaborazione."
        if restart:
            message += "\n\nRichiedono il riavvio dell'applicazione:\n- " + "\n- ".join(restart)
        self._info("Impostazioni", message)

    def _restore_defaults(self) -> None:
        from tkinter import messagebox

        if messagebox.askyesno(
            "Ripristina default",
            "Ripristinare i valori predefiniti nel modulo? (premere Salva per confermarli)",
            parent=self.root,
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
                self._model_combo.configure(values=result)
            elif not silent:
                self._error("Ollama", f"Ollama non raggiungibile su {url}.")

        _run_in_background(self.root, lambda: _fetch_ollama_models(url, timeout=3.0), _done)

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

        _run_in_background(self.root, lambda: _fetch_ollama_models(url, timeout=3.0), _done)

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

        _run_in_background(self.root, lambda: probe_ollama_gpu(url, model), _done)

    # ================================================================ utility
    def _confirm(self, title: str, message: str) -> bool:
        from tkinter import messagebox

        return bool(messagebox.askyesno(title, message, parent=self.root))

    def _cmd_create_desktop_shortcut(self) -> None:
        try:
            from tools.create_shortcut import create_desktop_shortcut

            shortcut = create_desktop_shortcut()
            self._info("Collegamento Desktop", f"Icona creata con successo sul Desktop:\n{shortcut.name}")
        except Exception as exc:
            self._error("Collegamento Desktop", f"Impossibile creare il collegamento sul Desktop: {exc}")

    def _info(self, title: str, message: str) -> None:
        from tkinter import messagebox

        messagebox.showinfo(title, message, parent=self.root)

    def _error(self, title: str, message: str) -> None:
        from tkinter import messagebox

        messagebox.showerror(title, message, parent=self.root)


def run_dashboard(initial_tab: str | None = None) -> int:
    """Entry point di `main.py --dashboard`: istanza singola, nessun processo audio coinvolto."""
    try:
        import tkinter as tk
    except ImportError:
        sys.stderr.write(
            "Il cruscotto richiede tkinter, assente in questa installazione di Python.\n"
            "Reinstallare Python da python.org includendo l'opzione 'tcl/tk and IDLE'.\n"
        )
        return EXIT_MISSING_DEPENDENCY

    if sys.platform == "win32":
        try:
            import ctypes

            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(f"{APP_NAME}.MeetingScribe.{APP_VERSION}")
        except Exception:
            pass

    instance = SingleInstance(DASHBOARD_MUTEX_NAME)
    if not instance.acquire():
        focus_window_by_title(DASHBOARD_TITLE)
        return EXIT_SUCCESS
    try:
        registry = JobRegistry()
        root = tk.Tk()
        if APP_ICON_PATH.exists():
            try:
                root.iconbitmap(default=str(APP_ICON_PATH))
                root.iconbitmap(str(APP_ICON_PATH))
            except Exception:
                pass
        logo_png = APP_ICON_PATH.parent / "logo_64.png"
        if logo_png.exists():
            try:
                photo = tk.PhotoImage(file=str(logo_png))
                root.iconphoto(True, photo)
            except Exception:
                pass
        try:
            root.iconname(APP_NAME)
        except tk.TclError:
            pass
        DashboardApp(root, registry, initial_tab=initial_tab)
        root.title(f"{DASHBOARD_TITLE}")
        root.mainloop()
        logger.info("Cruscotto %s v%s chiuso.", APP_NAME, APP_VERSION)
        return EXIT_SUCCESS
    finally:
        instance.release()
