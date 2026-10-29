from __future__ import annotations

import queue
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

from modules.contracts import (
    InvalidStageTransitionError,
    MeetingMetadata,
    SessionContext,
    SessionMode,
    Stage,
    StateValidationError,
    now_tz,
    validate_stage_transition,
)
from modules.logging_setup import get_logger, sanitize_for_log

if TYPE_CHECKING:
    from modules.job_registry import JobRegistry
    from modules.recorder import DualTrackRecorder
    from modules.settings import AppSettings
    from modules.state_manager import StateManager
    from modules.transcriber import RollingTranscriber
    from modules.tray_ui import StageMonitorTray

logger = get_logger("session_runtime")

EVENT_STAGE = "stage"
EVENT_PROGRESS = "progress"
EVENT_APP = "app"
EVENT_JOB = "job"
EVENT_NOTIFY = "notify"
EVENT_STATUS = "status"
EVENT_LAST_OUTPUT = "last_output"

_BUS_MAXSIZE: int = 10000


@dataclass(slots=True)
class StageEvent:
    """Evento pubblicato sul bus: aggiornamento di stage, avanzamento, stato job o notifica."""

    kind: str
    session_id: str | None = None
    stage: Stage | None = None
    detail: str = ""
    current: int | None = None
    total: int | None = None
    fields: dict[str, object] = field(default_factory=dict)
    occurred_at: datetime = field(default_factory=now_tz)


class EventBus:
    """Coda non bloccante: chi pubblica (anche il thread audio) non attende mai tray, PIL o SQLite."""

    def __init__(self, maxsize: int = _BUS_MAXSIZE) -> None:
        self._queue: queue.Queue[StageEvent] = queue.Queue(maxsize=maxsize)
        self._counter_lock = threading.Lock()
        self.published: int = 0
        self.dropped: int = 0

    def publish(self, event: StageEvent) -> None:
        try:
            self._queue.put_nowait(event)
        except queue.Full:
            with self._counter_lock:
                self.dropped += 1
            return
        with self._counter_lock:
            self.published += 1

    def get(self, timeout: float | None = None) -> StageEvent | None:
        try:
            return self._queue.get(timeout=timeout)
        except queue.Empty:
            return None

    def empty(self) -> bool:
        return self._queue.empty()


class SessionStageReporter:
    """StageReporter di una singola sessione.

    Valida le transizioni senza mai forzarle: una transizione non valida produce solo un
    warning e viene ignorata. Lo stage corrente è tenuto in memoria, persistito nello
    state.json della propria sessione e pubblicato sul bus per tray e storico.
    """

    def __init__(
        self,
        session_id: str,
        state_manager: StateManager | None,
        bus: EventBus | None,
        *,
        initial_stage: Stage = Stage.IDLE,
        persist: bool = True,
    ) -> None:
        self.session_id = session_id
        self._state_manager = state_manager
        self._bus = bus
        self._persist_enabled = persist
        self._lock = threading.Lock()
        self._stage: Stage = initial_stage

    @property
    def stage(self) -> Stage:
        with self._lock:
            return self._stage

    def set_stage(
        self,
        stage: Stage,
        detail_text: str = "",
        current: int | None = None,
        total: int | None = None,
    ) -> None:
        with self._lock:
            previous = self._stage
            changed = stage != previous
            if changed:
                try:
                    validate_stage_transition(previous, stage)
                except InvalidStageTransitionError:
                    logger.warning(
                        "Transizione di stage ignorata per la sessione %s: %s -> %s",
                        self.session_id,
                        previous.value,
                        stage.value,
                    )
                    return
                self._stage = stage
        if changed:
            self._persist(stage, previous, detail_text)
        self._publish(EVENT_STAGE, stage, detail_text, current, total)

    def rebase(self, stage: Stage, detail_text: str = "") -> None:
        """Riallinea lo stage senza validazione (presa in carico di una sessione recuperata da disco)."""
        with self._lock:
            previous = self._stage
            self._stage = stage
        self._persist(stage, previous, detail_text)
        self._publish(EVENT_STAGE, stage, detail_text, None, None)

    def mark_error(self, detail_text: str = "") -> None:
        """Porta la sessione in ERROR (lo state.json è già aggiornato da record_error)."""
        with self._lock:
            self._stage = Stage.ERROR
        self._publish(EVENT_STAGE, Stage.ERROR, detail_text, None, None)

    def progress_tokens(self, tokens: int) -> None:
        """Avanzamento dello streaming Ollama (token generati nella chiamata corrente)."""
        if self._bus is None:
            return
        self._bus.publish(
            StageEvent(
                kind=EVENT_PROGRESS,
                session_id=self.session_id,
                stage=self.stage,
                fields={"tokens_generated": int(tokens)},
            )
        )

    def progress_steps(
        self,
        current: int,
        total: int,
        detail_text: str | None = None,
    ) -> None:
        """Aggiorna i contatori progress_current/progress_total senza cambiare lo stage corrente."""
        if self._bus is None:
            return
        fields: dict[str, object] = {
            "progress_current": int(current),
            "progress_total": int(total),
        }
        if detail_text is not None:
            fields["detail"] = detail_text
        self._bus.publish(
            StageEvent(
                kind=EVENT_PROGRESS,
                session_id=self.session_id,
                stage=self.stage,
                detail=detail_text or "",
                current=int(current),
                total=int(total),
                fields=fields,
            )
        )

    def _persist(self, stage: Stage, previous: Stage, detail_text: str) -> None:
        sm = self._state_manager
        if not self._persist_enabled or sm is None or sm.closed:
            return

        def _apply(ctx: SessionContext) -> None:
            ctx.stage = stage

        try:
            sm.mutate(_apply)
            logger.info(
                "Transizione stage: %s -> %s (detail=%s)",
                previous.value,
                stage.value,
                sanitize_for_log(detail_text) if detail_text else "-",
            )
        except (StateValidationError, OSError) as exc:
            logger.debug("Stage %s non persistito per %s: %s", stage.value, self.session_id, exc)

    def _publish(
        self,
        kind: str,
        stage: Stage,
        detail_text: str,
        current: int | None,
        total: int | None,
    ) -> None:
        if self._bus is None:
            return
        self._bus.publish(
            StageEvent(
                kind=kind,
                session_id=self.session_id,
                stage=stage,
                detail=detail_text,
                current=current,
                total=total,
            )
        )


class AppStatusReporter:
    """StageReporter del watcher: IDLE, DEBOUNCE, GRACE... sono stati dell'app, non della sessione."""

    def __init__(self, bus: EventBus) -> None:
        self._bus = bus

    def set_stage(
        self,
        stage: Stage,
        detail_text: str = "",
        current: int | None = None,
        total: int | None = None,
    ) -> None:
        self._bus.publish(
            StageEvent(kind=EVENT_APP, stage=stage, detail=detail_text, current=current, total=total)
        )


@dataclass
class SessionRuntime:
    """Tutto ciò che appartiene a una singola sessione: niente riferimenti globali condivisi."""

    session_id: str
    session_dir: Path
    state_manager: StateManager
    reporter: SessionStageReporter
    mode: SessionMode
    metadata: MeetingMetadata
    started_at: datetime
    settings: AppSettings
    recorder: DualTrackRecorder | None = None
    transcriber: RollingTranscriber | None = None
    ended_at: datetime | None = None
    attempts: int = 0

    def transition_to(self, stage: Stage, detail_text: str = "") -> None:
        self.reporter.set_stage(stage, detail_text)


class EventWriter(threading.Thread):
    """Unico consumatore del bus: aggiorna la tray e scrive lo storico SQLite (unico writer)."""

    def __init__(
        self,
        bus: EventBus,
        tray: StageMonitorTray,
        registry: JobRegistry | None,
        *,
        live_session_getter: Callable[[], str | None] | None = None,
    ) -> None:
        super().__init__(name="EventWriter", daemon=True)
        self._bus = bus
        self._tray = tray
        self._registry = registry
        self._live_session_getter = live_session_getter
        self._stop_requested = threading.Event()
        self._job_status: dict[str, str] = {}
        self._processing_session: str | None = None
        self._handled_cond = threading.Condition()
        self.handled: int = 0

    def stop(self, timeout: float = 5.0) -> None:
        self._stop_requested.set()
        if self.is_alive() and threading.current_thread() is not self:
            self.join(timeout=timeout)

    def flush(self, timeout: float = 2.0) -> bool:
        """Attende che tutti gli eventi pubblicati finora siano stati gestiti (test e shutdown)."""
        target = self._bus.published
        with self._handled_cond:
            return self._handled_cond.wait_for(lambda: self.handled >= target, timeout=timeout)

    def run(self) -> None:
        while not (self._stop_requested.is_set() and self._bus.empty()):
            event = self._bus.get(timeout=0.2)
            if event is None:
                continue
            try:
                self._dispatch(event)
            except Exception as exc:
                logger.warning("Evento %s non gestito: %s", event.kind, exc, exc_info=True)
            finally:
                with self._handled_cond:
                    self.handled += 1
                    self._handled_cond.notify_all()

    def _queued_count(self) -> int:
        return sum(1 for status in self._job_status.values() if status in ("queued", "processing"))

    def _dispatch(self, event: StageEvent) -> None:
        if event.kind == EVENT_APP and event.stage is not None:
            self._tray.set_recording_status(event.stage, event.detail)
            if self._registry is not None:
                self._registry.set_status(
                    {"watcher_stage": event.stage.value, "watcher_detail": event.detail}
                )
            return

        if event.kind == EVENT_NOTIFY:
            import config as _cfg

            if not getattr(_cfg, "INCOGNITO_MODE", False):
                self._tray.notify(str(event.fields.get("title", "")), event.detail)
            return

        if event.kind == EVENT_LAST_OUTPUT:
            raw_path = event.fields.get("path")
            if isinstance(raw_path, str):
                self._tray.set_last_output_file(Path(raw_path))
            return

        if event.kind == EVENT_STATUS:
            if self._registry is not None and event.fields:
                self._registry.set_status(event.fields)
            return

        session_id = event.session_id
        if session_id is None:
            return

        if event.kind == EVENT_JOB:
            status = event.fields.get("status")
            if status == "deleted":
                self._job_status.pop(session_id, None)
                if self._processing_session == session_id:
                    self._processing_session = None
                self._refresh_tray_queue(event)
                return
            if isinstance(status, str):
                self._job_status[session_id] = status
                if status == "processing":
                    self._processing_session = session_id
                elif self._processing_session == session_id:
                    self._processing_session = None
            if self._registry is not None:
                self._registry.upsert_job(session_id, **event.fields)
            self._refresh_tray_queue(event)
            return

        if event.kind == EVENT_PROGRESS:
            if self._registry is not None:
                self._registry.upsert_job(session_id, **event.fields)
            return

        if event.kind == EVENT_STAGE and event.stage is not None:
            if self._registry is not None:
                self._registry.update_stage(
                    session_id,
                    event.stage.value,
                    event.detail,
                    event.current,
                    event.total,
                    event.occurred_at,
                )
            live_id = self._live_session_getter() if self._live_session_getter else None
            if session_id != live_id:
                self._refresh_tray_queue(event)

    def _refresh_tray_queue(self, event: StageEvent) -> None:
        processing = self._processing_session
        if processing is not None and event.session_id == processing and event.stage is not None:
            self._tray.set_queue_status(
                event.stage, event.detail, event.current, event.total, self._queued_count()
            )
        elif processing is None:
            self._tray.set_queue_status(None, "", None, None, self._queued_count())
        else:
            self._tray.update_queue_count(self._queued_count())
