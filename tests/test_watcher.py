from __future__ import annotations

import threading
from datetime import datetime, timedelta

import psutil
import pytest

from modules.contracts import MeetingMetadata, ShutdownReason, Stage
from modules.watcher import (
    TeamsCallWatcher,
    WatcherSnapshot,
    WatcherTransition,
    compute_confidence_score,
    fetch_outlook_meeting_metadata,
    is_teams_audio_active,
    is_teams_process_running,
)


class _DummyReporter:
    def __init__(self) -> None:
        self.history: list[tuple[Stage, str]] = []

    def set_stage(
        self,
        stage: Stage,
        detail_text: str = "",
        current: int | None = None,
        total: int | None = None,
    ) -> None:
        self.history.append((stage, detail_text))


def test_compute_confidence_score_and_outlook_only_guard() -> None:
    """Verifica il calcolo dello score e che il solo Outlook non superi mai la soglia."""
    full_score = compute_confidence_score(
        {
            "process_present": True,
            "audio_active": True,
            "debounce_completed": True,
            "outlook_event": False,
            "window_title": False,
        }
    )
    assert full_score >= 0.70

    outlook_only = compute_confidence_score(
        {
            "process_present": False,
            "audio_active": False,
            "debounce_completed": False,
            "outlook_event": True,
            "window_title": False,
        }
    )
    assert outlook_only < 0.70


def test_is_teams_process_running_and_audio_active(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verifica rilevamento processi Teams e sessioni audio pycaw con gestione eccezioni."""
    class _FakeProc:
        def __init__(self, name: str, raise_denied: bool = False) -> None:
            self._name = name
            self._raise_denied = raise_denied
            self.info = {"name": name}

        def name(self) -> str:
            if self._raise_denied:
                raise psutil.AccessDenied()
            return self._name

    monkeypatch.setattr(
        "modules.watcher.psutil.process_iter",
        lambda _attrs=None: [_FakeProc("explorer.exe"), _FakeProc("ms-teams.exe")],
    )
    assert is_teams_process_running() is True

    monkeypatch.setattr(
        "modules.watcher.psutil.process_iter",
        lambda _attrs=None: [_FakeProc("notepad.exe")],
    )
    assert is_teams_process_running() is False

    class _FakeAudioSession:
        def __init__(self, proc_name: str | None, state: int, peak: float) -> None:
            self.Process = _FakeProc(proc_name) if proc_name else None
            self.State = state
            self.peak_value = peak

    class _FakeAudioUtilities:
        @staticmethod
        def GetAllSessions() -> list[_FakeAudioSession]:
            return [
                _FakeAudioSession(None, 1, 0.5),
                _FakeAudioSession("ms-teams.exe", 1, 0.25),
            ]

    monkeypatch.setattr("modules.watcher.AudioUtilities", _FakeAudioUtilities)
    assert is_teams_audio_active() is True


def test_watcher_debounce_false_positive_grace_resume_and_manual_controls(
    sample_metadata: MeetingMetadata,
) -> None:
    """Verifica l'intero ciclo del watcher: debounce, falso positivo, grace period, resume e comandi manuali."""
    now_val = [100.0]
    proc_active = [False]
    audio_active = [False]

    started_calls: list[tuple[MeetingMetadata, bool]] = []
    resumed_calls: list[int] = []
    suspected_end_calls: list[float] = []
    ended_calls: list[ShutdownReason] = []
    reporter = _DummyReporter()
    shutdown_ev = threading.Event()

    watcher = TeamsCallWatcher(
        stage_reporter=reporter,
        on_call_started=lambda m, manual: started_calls.append((m, manual)),
        on_call_resumed=lambda: resumed_calls.append(1),
        on_call_suspected_end=lambda rem: suspected_end_calls.append(rem),
        on_call_ended=lambda reason: ended_calls.append(reason),
        shutdown_event=shutdown_ev,
        start_debounce_sec=20.0,
        stop_grace_period_sec=300.0,
        process_checker=lambda: proc_active[0],
        audio_checker=lambda: audio_active[0],
        window_checker=lambda: True,
        metadata_fetcher=lambda: sample_metadata,
        clock=lambda: now_val[0],
    )

    # 1. Segnale attivo -> entra in START_DEBOUNCE
    proc_active[0] = True
    audio_active[0] = True
    watcher.step()
    assert watcher.stage == Stage.START_DEBOUNCE

    # 2. Falso positivo (audio cade prima di 20s) -> torna in IDLE
    now_val[0] = 110.0
    audio_active[0] = False
    watcher.step()
    assert watcher.stage == Stage.IDLE
    assert len(started_calls) == 0

    # 3. Nuovo segnale stabile per >= 20s -> avvia RECORDING
    audio_active[0] = True
    now_val[0] = 200.0
    watcher.step()
    assert watcher.stage == Stage.START_DEBOUNCE
    now_val[0] = 221.0
    watcher.step()
    assert watcher.stage == Stage.RECORDING
    assert len(started_calls) == 1
    assert started_calls[0][1] is False

    # 4. Caduta temporanea audio -> entra in GRACE_PERIOD
    audio_active[0] = False
    now_val[0] = 300.0
    watcher.step()
    assert watcher.stage == Stage.GRACE_PERIOD
    assert len(suspected_end_calls) == 1

    # 5. Ripresa audio entro i 300s -> torna in RECORDING nella stessa sessione (AC08)
    audio_active[0] = True
    now_val[0] = 400.0
    watcher.step()
    assert watcher.stage == Stage.RECORDING
    assert len(resumed_calls) == 1

    # 6. Nuova caduta audio oltre i 300s -> scade il grace period e finalizza
    audio_active[0] = False
    now_val[0] = 500.0
    watcher.step()
    assert watcher.stage == Stage.GRACE_PERIOD
    now_val[0] = 805.0
    watcher.step()
    assert watcher.stage == Stage.FINALIZING_AUDIO
    assert ended_calls == [ShutdownReason.GRACE_EXPIRED]

    # 7. Reset e test avvio + finalizzazione manuale
    watcher.reset_to_idle()
    watcher.request_manual_start()
    watcher.step()
    assert watcher.stage == Stage.RECORDING
    assert started_calls[-1][1] is True

    watcher.request_finalize()
    watcher.step()
    assert watcher.stage == Stage.FINALIZING_AUDIO
    assert ended_calls[-1] == ShutdownReason.USER_REQUEST


def test_fetch_outlook_meeting_metadata_fallback_and_overlapping_events(
    fixed_now: datetime,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verifica il fallback di Outlook COM in caso di errore e la selezione tra eventi sovrapposti."""
    class _FakePythonCom:
        @staticmethod
        def CoInitialize() -> None:
            return None

        @staticmethod
        def CoUninitialize() -> None:
            return None

    class _FakeAppointment:
        def __init__(
            self,
            subject: str,
            start: datetime,
            end: datetime,
            all_day: bool = False,
        ) -> None:
            self.Subject = subject
            self.Organizer = "Org"
            self.RequiredAttendees = "Alice; Bob"
            self.OptionalAttendees = ""
            self.Body = "Agenda riservata"
            self.Start = start
            self.End = end
            self.AllDayEvent = all_day

    class _FakeItems:
        def __init__(self, items: list[_FakeAppointment]) -> None:
            self._items = items
            self.IncludeRecurrences = False

        def Sort(self, _field: str) -> None:
            return None

        def Restrict(self, filter_str: str) -> list[_FakeAppointment]:
            assert "[Body]" not in filter_str
            return self._items

    class _FakeFolder:
        def __init__(self, items: _FakeItems) -> None:
            self.Items = items

    class _FakeNamespace:
        def __init__(self, folder: _FakeFolder) -> None:
            self._folder = folder

        def GetDefaultFolder(self, _folder_id: int) -> _FakeFolder:
            return self._folder

    class _FakeOutlookApp:
        def __init__(self, ns: _FakeNamespace) -> None:
            self._ns = ns

        def GetNamespace(self, _name: str) -> _FakeNamespace:
            return self._ns

    ev_future = _FakeAppointment(
        "Call Tra Poco",
        fixed_now + timedelta(minutes=15),
        fixed_now + timedelta(minutes=45),
    )
    ev_ongoing = _FakeAppointment(
        "Call In Corso Ora",
        fixed_now - timedelta(minutes=5),
        fixed_now + timedelta(minutes=25),
    )

    class _FakeWin32Client:
        @staticmethod
        def Dispatch(_prog_id: str) -> _FakeOutlookApp:
            return _FakeOutlookApp(
                _FakeNamespace(_FakeFolder(_FakeItems([ev_future, ev_ongoing])))
            )

    class _FakeWin32Module:
        client = _FakeWin32Client

    monkeypatch.setattr("modules.watcher.pythoncom", _FakePythonCom)
    monkeypatch.setattr("modules.watcher.win32com", _FakeWin32Module)

    selected = fetch_outlook_meeting_metadata(reference_time=fixed_now)
    assert selected.title == "Call In Corso Ora"
    assert selected.source == "outlook"
    assert selected.required_attendees == ["Alice", "Bob"]

    # Verifica fallback in caso di eccezione COM
    class _BrokenWin32Client:
        @staticmethod
        def Dispatch(_prog_id: str) -> None:
            raise RuntimeError("COM server non disponibile")

    class _BrokenWin32Module:
        client = _BrokenWin32Client

    monkeypatch.setattr("modules.watcher.win32com", _BrokenWin32Module)
    fallback = fetch_outlook_meeting_metadata(reference_time=fixed_now)
    assert fallback.title == "Riunione Teams"
    assert fallback.source == "fallback"


def test_watcher_pause_suspends_grace_period(sample_metadata: MeetingMetadata) -> None:
    """Verifica che la pausa manuale della registrazione sospenda o annulli il grace period (F5)."""
    now_val = [100.0]
    proc_active = [True]
    audio_active = [True]
    reporter = _DummyReporter()
    shutdown_ev = threading.Event()

    watcher = TeamsCallWatcher(
        stage_reporter=reporter,
        on_call_started=lambda m, manual: None,
        on_call_resumed=lambda: None,
        on_call_suspected_end=lambda rem: None,
        on_call_ended=lambda reason: None,
        shutdown_event=shutdown_ev,
        start_debounce_sec=10.0,
        stop_grace_period_sec=60.0,
        process_checker=lambda: proc_active[0],
        audio_checker=lambda: audio_active[0],
        window_checker=lambda: True,
        metadata_fetcher=lambda: sample_metadata,
        clock=lambda: now_val[0],
    )

    # 1. Avvia call
    watcher.step()
    now_val[0] = 115.0
    snap = watcher.step()
    assert snap.state == Stage.RECORDING
    assert watcher.stage == Stage.RECORDING

    # 2. Audio cade -> entra in GRACE_PERIOD
    audio_active[0] = False
    now_val[0] = 120.0
    snap = watcher.step()
    assert snap.state == Stage.GRACE_PERIOD
    assert snap.transition == WatcherTransition.SUSPECTED_END
    assert watcher.grace_started_at_monotonic == 120.0

    # 3. Pausa attivata durante GRACE_PERIOD -> ripristina RECORDING e azzera grace period
    watcher.set_recording_paused(True)
    assert watcher.is_recording_paused is True
    assert watcher.stage == Stage.RECORDING
    assert watcher.grace_started_at_monotonic is None

    # 4. Step successivi mentre in pausa non fanno partire il grace period
    now_val[0] = 200.0
    snap = watcher.step()
    assert snap.state == Stage.RECORDING
    assert snap.transition == WatcherTransition.NONE
    assert "pausa" in snap.reason.lower()
    assert watcher.grace_started_at_monotonic is None

    # 5. Ripresa della registrazione: ora senza audio il grace period può ripartire
    watcher.set_recording_paused(False)
    assert watcher.is_recording_paused is False
    snap = watcher.step()
    assert snap.state == Stage.GRACE_PERIOD
    assert snap.transition == WatcherTransition.SUSPECTED_END

