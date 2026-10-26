from __future__ import annotations

import os
import queue
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageFont

try:
    import pystray
except ImportError:  # pragma: no cover
    pystray = None

from config import APP_NAME, LOG_DIR, OUTPUT_DIR
from modules.contracts import Stage, StageUpdate, UiCommandType, now_tz
from modules.logging_setup import get_logger, sanitize_for_log

STAGE_COLORS: dict[Stage, dict[str, str]] = {
    Stage.BOOTSTRAPPING: {
        "fill": "#546E7A",
        "border": "#37474F",
    },
    Stage.IDLE: {
        "fill": "#607D8B",
        "border": "#455A64",
    },
    Stage.START_DEBOUNCE: {
        "fill": "#1E88E5",
        "border": "#1565C0",
    },
    Stage.RECORDING: {
        "fill": "#E53935",
        "border": "#B71C1C",
    },
    Stage.RECORDING_AND_TRANSCRIBING: {
        "fill": "#E53935",
        "border": "#B71C1C",
        "ring": "#00ACC1",
    },
    Stage.GRACE_PERIOD: {
        "fill": "#FFB300",
        "border": "#FF8F00",
    },
    Stage.FINALIZING_AUDIO: {
        "fill": "#FB8C00",
        "border": "#EF6C00",
    },
    Stage.QUEUED: {
        "fill": "#78909C",
        "border": "#546E7A",
        "ring": "#8E24AA",
    },
    Stage.TRANSCRIBING_FINAL: {
        "fill": "#00ACC1",
        "border": "#00838F",
    },
    Stage.RELEASING_WHISPER: {
        "fill": "#283593",
        "border": "#1A237E",
    },
    Stage.SUMMARIZING_MAP: {
        "fill": "#8E24AA",
        "border": "#6A1B9A",
    },
    Stage.SUMMARIZING_REDUCE: {
        "fill": "#8E24AA",
        "border": "#6A1B9A",
        "ring": "#FFD700",
    },
    Stage.BUILDING_DOCX: {
        "fill": "#F57C00",
        "border": "#E65100",
    },
    Stage.VERIFYING_DOCX: {
        "fill": "#F57C00",
        "border": "#E65100",
        "badge": "#FFFFFF",
    },
    Stage.CLEANING_UP: {
        "fill": "#66BB6A",
        "border": "#43A047",
    },
    Stage.COMPLETED: {
        "fill": "#00C853",
        "border": "#009624",
        "symbol": "#FFFFFF",
    },
    Stage.ERROR: {
        "fill": "#D32F2F",
        "border": "#9A0007",
        "symbol": "#FFFFFF",
    },
    Stage.SHUTTING_DOWN: {
        "fill": "#424242",
        "border": "#212121",
    },
}

STAGE_LABELS_IT: dict[Stage, str] = {
    Stage.BOOTSTRAPPING: "Inizializzazione ambiente",
    Stage.IDLE: "In attesa di call Teams",
    Stage.START_DEBOUNCE: "Verifica avvio call Teams",
    Stage.RECORDING: "Registrazione in corso",
    Stage.RECORDING_AND_TRANSCRIBING: "Registrazione + Trascrizione",
    Stage.GRACE_PERIOD: "Pausa temporanea (Grace Period)",
    Stage.FINALIZING_AUDIO: "Finalizzazione tracce audio",
    Stage.QUEUED: "In coda di elaborazione",
    Stage.TRANSCRIBING_FINAL: "Trascrizione finale in corso",
    Stage.RELEASING_WHISPER: "Rilascio risorse Whisper",
    Stage.SUMMARIZING_MAP: "Sintesi blocchi (Fase MAP)",
    Stage.SUMMARIZING_REDUCE: "Sintesi verbale e mail (Fase REDUCE)",
    Stage.BUILDING_DOCX: "Generazione documento Word",
    Stage.VERIFYING_DOCX: "Verifica documento Word",
    Stage.CLEANING_UP: "Pulizia file temporanei",
    Stage.COMPLETED: "Elaborazione completata",
    Stage.ERROR: "Errore applicativo",
    Stage.SHUTTING_DOWN: "Chiusura in corso",
}


# Stage del canale "registrazione" (watcher); gli altri appartengono al canale "elaborazione"
RECORDING_CHANNEL_STAGES: frozenset[Stage] = frozenset(
    {
        Stage.BOOTSTRAPPING,
        Stage.IDLE,
        Stage.START_DEBOUNCE,
        Stage.RECORDING,
        Stage.RECORDING_AND_TRANSCRIBING,
        Stage.GRACE_PERIOD,
        Stage.FINALIZING_AUDIO,
    }
)
ACTIVE_RECORDING_STAGES: frozenset[Stage] = frozenset(
    {Stage.RECORDING, Stage.RECORDING_AND_TRANSCRIBING, Stage.GRACE_PERIOD}
)


def _draw_queue_badge(image: Image.Image, count: int) -> None:
    """Disegna in alto a destra un badge con il numero di job in coda."""
    draw = ImageDraw.Draw(image)
    dim = image.size[0]
    radius = max(7, dim // 5)
    cx, cy = dim - radius - 1, radius + 1
    draw.ellipse(
        (cx - radius, cy - radius, cx + radius, cy + radius),
        fill="#8E24AA",
        outline="#FFFFFF",
        width=max(1, dim // 32),
    )
    label = str(count) if count < 10 else "9+"
    try:
        font: Any = ImageFont.load_default(size=max(10, int(radius * 1.3)))
    except TypeError:  # Pillow < 10.1 non accetta size
        font = ImageFont.load_default()
    bbox = draw.textbbox((0, 0), label, font=font)
    text_w, text_h = bbox[2] - bbox[0], bbox[3] - bbox[1]
    draw.text(
        (cx - text_w / 2 - bbox[0], cy - text_h / 2 - bbox[1]),
        label,
        fill="#FFFFFF",
        font=font,
    )


def create_stage_icon_image(
    stage: Stage,
    size: int = 64,
    badge_count: int | None = None,
) -> Image.Image:
    """Genera un'icona RGBA 64x64 con indicatori visivi specifici per lo Stage indicato."""
    dim = max(16, int(size))
    image = Image.new("RGBA", (dim, dim), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)

    palette = STAGE_COLORS.get(stage, STAGE_COLORS[Stage.IDLE])
    fill_color = palette["fill"]
    border_color = palette["border"]

    margin = max(2, dim // 10)
    bbox = (margin, margin, dim - margin - 1, dim - margin - 1)
    border_width = max(2, dim // 16)

    draw.ellipse(
        bbox,
        fill=fill_color,
        outline=border_color,
        width=border_width,
    )

    ring_color = palette.get("ring")
    if ring_color:
        ring_margin = margin + border_width + max(2, dim // 16)
        ring_bbox = (
            ring_margin,
            ring_margin,
            dim - ring_margin - 1,
            dim - ring_margin - 1,
        )
        draw.ellipse(
            ring_bbox,
            outline=ring_color,
            width=max(2, dim // 14),
        )

    badge_color = palette.get("badge")
    if badge_color:
        badge_radius = max(4, dim // 6)
        badge_center_x = dim - margin - badge_radius
        badge_center_y = dim - margin - badge_radius
        badge_bbox = (
            badge_center_x - badge_radius,
            badge_center_y - badge_radius,
            badge_center_x + badge_radius,
            badge_center_y + badge_radius,
        )
        draw.ellipse(
            badge_bbox,
            fill=badge_color,
            outline=border_color,
            width=max(1, border_width // 2),
        )

    symbol_color = palette.get("symbol", "#FFFFFF")
    stroke = max(3, dim // 12)
    if stage == Stage.COMPLETED:
        p1 = (int(dim * 0.28), int(dim * 0.52))
        p2 = (int(dim * 0.44), int(dim * 0.68))
        p3 = (int(dim * 0.72), int(dim * 0.34))
        draw.line([p1, p2, p3], fill=symbol_color, width=stroke)
    elif stage == Stage.ERROR:
        offset = int(dim * 0.30)
        end = dim - offset - 1
        draw.line([(offset, offset), (end, end)], fill=symbol_color, width=stroke)
        draw.line([(offset, end), (end, offset)], fill=symbol_color, width=stroke)

    if badge_count is not None and badge_count > 0:
        _draw_queue_badge(image, badge_count)

    return image


class StageMonitorTray:
    """Gestore thread-safe della System Tray con icone dinamiche e menu in italiano."""

    def __init__(
        self,
        command_queue: queue.Queue[UiCommandType] | None = None,
        *,
        output_dir: Path | None = None,
        log_path: Path | None = None,
        headless: bool = False,
        on_open_dashboard: Callable[[str | None], None] | None = None,
    ) -> None:
        self._lock = threading.RLock()
        self._logger = get_logger("tray_ui")
        self.ready_event = threading.Event()
        self.stop_event = threading.Event()
        self.command_queue: queue.Queue[UiCommandType] = (
            command_queue
            if command_queue is not None
            else queue.Queue(maxsize=64)
        )

        self._stage: Stage = Stage.IDLE
        self._detail: str = ""
        self._current: int | None = None
        self._total: int | None = None
        self._occurred_at = now_tz()
        self._last_output_file: Path | None = None
        self._on_open_dashboard = on_open_dashboard

        # Stato composito: canale registrazione (watcher) e canale elaborazione (job in coda)
        self._recording_stage: Stage = Stage.IDLE
        self._recording_detail: str = ""
        self._processing_stage: Stage | None = None
        self._processing_detail: str = ""
        self._processing_current: int | None = None
        self._processing_total: int | None = None
        self._queue_count: int = 0
        self._shutting_down: bool = False

        self._output_dir: Path = (
            output_dir if output_dir is not None else OUTPUT_DIR
        ).resolve()
        self._log_path: Path = (
            log_path if log_path is not None else (LOG_DIR / "app.log")
        ).resolve()
        self._headless: bool = bool(
            headless
            or os.environ.get("TEAMS_SCRIBE_HEADLESS_TRAY") == "1"
            or pystray is None
        )

        self._icon: Any = None
        self._thread: threading.Thread | None = None
        self._current_image: Image.Image = create_stage_icon_image(self._stage)

    @property
    def last_output_file(self) -> Path | None:
        """Restituisce il percorso dell'ultimo documento DOCX generato."""
        with self._lock:
            return self._last_output_file

    def get_snapshot(self) -> StageUpdate:
        """Restituisce uno snapshot immutabile dello stato corrente della Tray."""
        with self._lock:
            return StageUpdate(
                stage=self._stage,
                detail=self._detail,
                current=self._current,
                total=self._total,
                occurred_at=self._occurred_at,
            )

    @property
    def queue_count(self) -> int:
        with self._lock:
            return self._queue_count

    def format_status_label(self) -> str:
        """Formatta l'etichetta di stato corrente in italiano per tooltip e prima voce del menu."""
        with self._lock:
            base_label = STAGE_LABELS_IT.get(self._stage, self._stage.value)
            parts: list[str] = [base_label]
            if self._current is not None and self._total is not None and self._total > 0:
                parts.append(f"({self._current}/{self._total})")
            elif self._current is not None:
                parts.append(f"({self._current})")
            if self._detail:
                parts.append(f"- {self._detail}")
            if (
                self._recording_stage in ACTIVE_RECORDING_STAGES
                and self._processing_stage is not None
            ):
                parts.append(
                    f"| Elaborazione: {STAGE_LABELS_IT.get(self._processing_stage, self._processing_stage.value)}"
                )
            if self._queue_count > 0:
                parts.append(f"| Job attivi: {self._queue_count}")
            return " ".join(parts)

    def get_menu_labels(self) -> list[str]:
        """Restituisce le voci del menu contestuale nell'ordine previsto dalla specifica."""
        return [
            self.format_status_label(),
            "Avvia Registrazione Manuale Ora",
            "Termina Call e Genera Recap Ora",
            "Apri Cruscotto",
            "Impostazioni",
            "Apri Ultimo File Word",
            "Apri Cartella Output",
            "Mostra Log",
            "Esci",
        ]

    def _enqueue_command(self, command: UiCommandType) -> None:
        """Inserisce un comando nella coda UI senza bloccare il thread della Tray."""
        try:
            self.command_queue.put_nowait(command)
        except queue.Full:
            self._logger.warning(
                "Coda comandi UI piena; comando ignorato: %s", command.value
            )

    def _open_path_safely(self, target: Path, description: str) -> bool:
        """Apre un file o una cartella su Windows tramite os.startfile gestendo eventuali errori."""
        if not target.exists():
            self._logger.warning(
                "%s non trovato sul filesystem: %s",
                description,
                sanitize_for_log(str(target)),
            )
            self.notify(
                "Percorso non disponibile",
                f"{description} non trovato: {target.name}",
            )
            return False

        startfile_fn = getattr(os, "startfile", None)
        if not callable(startfile_fn):
            self._logger.warning(
                "os.startfile non disponibile sulla piattaforma corrente."
            )
            return False

        try:
            startfile_fn(str(target))
            return True
        except OSError as exc:
            self._logger.warning(
                "Impossibile aprire %s (%s): %s",
                description,
                sanitize_for_log(str(target)),
                sanitize_for_log(str(exc)),
            )
            self.notify(
                "Errore apertura file",
                f"Impossibile aprire {target.name}.",
            )
            return False

    def _on_start_manual(self, *_args: object) -> None:
        self._enqueue_command(UiCommandType.START_MANUAL)

    def _on_finalize_now(self, *_args: object) -> None:
        self._enqueue_command(UiCommandType.FINALIZE_NOW)

    def _on_open_dashboard_item(self, *_args: object) -> None:
        self._open_dashboard(None)

    def _on_open_settings_item(self, *_args: object) -> None:
        self._open_dashboard("impostazioni")

    def _open_dashboard(self, tab: str | None) -> None:
        callback = self._on_open_dashboard
        if callback is None:
            self.notify("Cruscotto non disponibile", "Il cruscotto non è disponibile in questa modalità.")
            return
        try:
            callback(tab)
        except Exception as exc:
            self._logger.warning("Apertura del cruscotto non riuscita: %s", sanitize_for_log(str(exc)))
            self.notify("Cruscotto non disponibile", "Impossibile avviare il cruscotto.")

    # I comandi OPEN_* sono gestiti qui, senza passare dalla coda: con l'app occupata la coda
    # si riempirebbe e comandi importanti come "Esci" verrebbero scartati.
    def _on_open_last_docx(self, *_args: object) -> bool:
        with self._lock:
            target = self._last_output_file
        if target is None:
            self._logger.warning("Nessun documento Word generato nella sessione.")
            self.notify(
                "Documento non disponibile",
                "Non è ancora stato generato alcun file Word.",
            )
            return False
        return self._open_path_safely(target, "Ultimo file Word")

    def _on_open_output(self, *_args: object) -> bool:
        from modules.docx_builder import latest_output_folder

        return self._open_path_safely(latest_output_folder(self._output_dir), "Cartella output")

    def _on_open_log(self, *_args: object) -> bool:
        return self._open_path_safely(self._log_path, "File di log")

    def _on_exit(self, *_args: object) -> None:
        self._enqueue_command(UiCommandType.EXIT)

    def _can_start_manual(self) -> bool:
        """Avvio manuale possibile ogni volta che non c'è una registrazione attiva (anche durante l'elaborazione)."""
        with self._lock:
            return not self._shutting_down and self._recording_stage not in ACTIVE_RECORDING_STAGES

    def _can_finalize_now(self) -> bool:
        with self._lock:
            return self._recording_stage in ACTIVE_RECORDING_STAGES

    def _has_last_docx(self) -> bool:
        with self._lock:
            return (
                self._last_output_file is not None
                and self._last_output_file.is_file()
            )

    def _build_pystray_menu(self) -> Any:
        """Costruisce il menu contestuale di pystray con le voci dinamiche in italiano."""
        if pystray is None:
            return None
        return pystray.Menu(
            pystray.MenuItem(
                lambda _item: self.format_status_label(),
                None,
                enabled=False,
            ),
            pystray.MenuItem(
                "Avvia Registrazione Manuale Ora",
                self._on_start_manual,
                enabled=lambda _item: self._can_start_manual(),
            ),
            pystray.MenuItem(
                "Termina Call e Genera Recap Ora",
                self._on_finalize_now,
                enabled=lambda _item: self._can_finalize_now(),
            ),
            pystray.MenuItem(
                "Apri Cruscotto",
                self._on_open_dashboard_item,
                default=True,
            ),
            pystray.MenuItem(
                "Impostazioni",
                self._on_open_settings_item,
            ),
            pystray.MenuItem(
                "Apri Ultimo File Word",
                self._on_open_last_docx,
                enabled=lambda _item: self._has_last_docx(),
            ),
            pystray.MenuItem(
                "Apri Cartella Output",
                self._on_open_output,
            ),
            pystray.MenuItem(
                "Mostra Log",
                self._on_open_log,
            ),
            pystray.MenuItem(
                "Esci",
                self._on_exit,
            ),
        )

    def start(self) -> None:
        """Avvia il loop dell'icona System Tray in un thread dedicato."""
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self.stop_event.clear()

            if self._headless or pystray is None:
                self.ready_event.set()
                return

            icon = pystray.Icon(
                APP_NAME,
                icon=self._current_image,
                title=self.format_status_label()[:120],
                menu=self._build_pystray_menu(),
            )
            self._icon = icon

            def _run_icon() -> None:
                def _setup(ic: Any) -> None:
                    try:
                        ic.visible = True
                    finally:
                        self.ready_event.set()

                try:
                    icon.run(setup=_setup)
                except Exception as exc:  # pragma: no cover
                    self._logger.warning(
                        "Loop pystray terminato con errore: %s",
                        sanitize_for_log(str(exc)),
                    )
                    self.ready_event.set()

            self._thread = threading.Thread(
                target=_run_icon,
                name="TrayUiThread",
                daemon=True,
            )
            self._thread.start()

        self.ready_event.wait(timeout=3.0)

    def stop(self) -> None:
        """Arresta in modo ordinato l'icona della System Tray e attende il thread UI."""
        self.stop_event.set()
        with self._lock:
            icon = self._icon
            self._icon = None
            thread = self._thread
            self._thread = None

        if icon is not None:
            try:
                icon.stop()
            except Exception as exc:  # pragma: no cover
                self._logger.warning(
                    "Errore durante l'arresto dell'icona Tray: %s",
                    sanitize_for_log(str(exc)),
                )

        if (
            thread is not None
            and thread.is_alive()
            and thread is not threading.current_thread()
        ):
            thread.join(timeout=3.0)

    def set_stage(
        self,
        stage: Stage,
        detail_text: str = "",
        current: int | None = None,
        total: int | None = None,
    ) -> None:
        """Imposta direttamente lo stato mostrato (modalità a singola sessione: dry-run, resume, test).

        In modalità automatica si usano invece set_recording_status e set_queue_status.
        """
        with self._lock:
            if stage == Stage.SHUTTING_DOWN:
                self._shutting_down = True
            elif stage in RECORDING_CHANNEL_STAGES:
                self._recording_stage = stage
                self._recording_detail = detail_text.strip()
                self._processing_stage = None
            else:
                self._recording_stage = Stage.IDLE
                self._recording_detail = ""
                self._processing_stage = stage
                self._processing_detail = detail_text.strip()
                self._processing_current = current
                self._processing_total = total
        self._refresh_display(direct=(stage, detail_text, current, total))

    def set_recording_status(self, stage: Stage, detail_text: str = "") -> None:
        """Aggiorna il canale registrazione (stato del watcher: attesa, debounce, registrazione, grace)."""
        with self._lock:
            self._recording_stage = stage
            self._recording_detail = detail_text.strip()
        self._refresh_display()

    def set_queue_status(
        self,
        stage: Stage | None,
        detail_text: str = "",
        current: int | None = None,
        total: int | None = None,
        queued: int = 0,
    ) -> None:
        """Aggiorna il canale elaborazione: stage del job in corso (None se nessuno) e numero di job attivi."""
        with self._lock:
            self._processing_stage = stage
            self._processing_detail = detail_text.strip() if stage is not None else ""
            self._processing_current = current
            self._processing_total = total
            self._queue_count = max(0, int(queued))
        self._refresh_display()

    def update_queue_count(self, queued: int) -> None:
        with self._lock:
            self._queue_count = max(0, int(queued))
        self._refresh_display()

    def _refresh_display(
        self,
        direct: tuple[Stage, str, int | None, int | None] | None = None,
    ) -> None:
        """Ricalcola stato mostrato, icona e tooltip: la registrazione attiva ha la precedenza."""
        with self._lock:
            if direct is not None:
                stage, detail, current, total = direct
            elif self._shutting_down:
                stage, detail, current, total = Stage.SHUTTING_DOWN, "", None, None
            elif self._recording_stage in ACTIVE_RECORDING_STAGES or self._processing_stage is None:
                stage, detail, current, total = (
                    self._recording_stage,
                    self._recording_detail,
                    None,
                    None,
                )
            else:
                stage = self._processing_stage
                detail = self._processing_detail
                current, total = self._processing_current, self._processing_total
            self._stage = stage
            self._detail = detail.strip()
            self._current = current
            self._total = total
            self._occurred_at = now_tz()
            self._current_image = create_stage_icon_image(
                stage, badge_count=self._queue_count or None
            )
            icon = self._icon
            status_title = self.format_status_label()[:120]

        if icon is not None:
            try:
                icon.icon = self._current_image
                icon.title = status_title
                if hasattr(icon, "update_menu"):
                    icon.update_menu()
            except Exception as exc:  # pragma: no cover
                self._logger.warning(
                    "Impossibile aggiornare l'icona Tray nello stage %s: %s",
                    stage.value,
                    sanitize_for_log(str(exc)),
                )

    def set_last_output_file(self, path: Path | None) -> None:
        """Registra il percorso dell'ultimo file Word generato e aggiorna il menu."""
        with self._lock:
            self._last_output_file = path.resolve() if path is not None else None
            icon = self._icon

        if icon is not None and hasattr(icon, "update_menu"):
            try:
                icon.update_menu()
            except Exception as exc:  # pragma: no cover
                self._logger.warning(
                    "Impossibile aggiornare il menu Tray: %s",
                    sanitize_for_log(str(exc)),
                )

    def notify(self, title: str, message: str) -> None:
        """Mostra una notifica desktop tramite la System Tray se supportata."""
        clean_title = title.strip() or APP_NAME
        clean_message = message.strip()
        if not clean_message:
            return

        with self._lock:
            icon = self._icon

        if icon is not None and hasattr(icon, "notify"):
            try:
                icon.notify(clean_message[:250], clean_title[:60])
            except Exception as exc:  # pragma: no cover
                self._logger.warning(
                    "Notifica Tray non inviata (%s): %s",
                    sanitize_for_log(clean_title),
                    sanitize_for_log(str(exc)),
                )
