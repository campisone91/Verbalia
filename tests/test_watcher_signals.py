from __future__ import annotations

import threading
from datetime import datetime, timedelta

import pytest

from modules.contracts import MeetingMetadata, ShutdownReason, Stage
from modules.watcher import (
    TeamsCallWatcher,
    _is_single_session_active,
    clean_outlook_agenda,
    fetch_outlook_meeting_metadata,
)


class _Reporter:
    def __init__(self) -> None:
        self.history: list[Stage] = []

    def set_stage(self, stage: Stage, detail_text: str = "", current: int | None = None, total: int | None = None) -> None:
        self.history.append(stage)


def _make_watcher(**overrides: object) -> tuple[TeamsCallWatcher, dict[str, list[object]], list[float]]:
    clock = [0.0]
    calls: dict[str, list[object]] = {"started": [], "ended": [], "resumed": [], "suspected": []}
    params: dict[str, object] = {
        "stage_reporter": _Reporter(),
        "on_call_started": lambda m, manual: calls["started"].append((m, manual)),
        "on_call_resumed": lambda: calls["resumed"].append(1),
        "on_call_suspected_end": lambda rem: calls["suspected"].append(rem),
        "on_call_ended": lambda reason: calls["ended"].append(reason),
        "shutdown_event": threading.Event(),
        "start_debounce_sec": 0.0,
        "stop_grace_period_sec": 60.0,
        "process_checker": lambda: True,
        "audio_checker": lambda: False,
        "window_checker": lambda: False,
        "mic_checker": lambda: False,
        "clock": lambda: clock[0],
    }
    params.update(overrides)
    return TeamsCallWatcher(**params), calls, clock  # type: ignore[arg-type]


def test_active_audio_session_counts_even_when_silent() -> None:
    """B5: una sessione audio Teams in stato Active è una call in corso anche con peak a zero."""
    class _Proc:
        def name(self) -> str:
            return "ms-teams.exe"

    class _Session:
        Process = _Proc()
        State = 1
        peak_value = 0.0

    class _Inactive(_Session):
        State = 0
        peak_value = 0.9

    assert _is_single_session_active(_Session(), {"ms-teams.exe"}) is True
    assert _is_single_session_active(_Inactive(), {"ms-teams.exe"}) is False


def test_microphone_signal_keeps_call_open_during_silence() -> None:
    """B5: con il microfono in uso da Teams la call resta attiva anche senza sessione audio attiva."""
    mic = [True]
    watcher, calls, clock = _make_watcher(mic_checker=lambda: mic[0])
    watcher.step()
    assert watcher.stage == Stage.RECORDING
    assert len(calls["started"]) == 1

    clock[0] = 30.0
    watcher.step()
    assert watcher.stage == Stage.RECORDING

    mic[0] = False
    clock[0] = 40.0
    watcher.step()
    assert watcher.stage == Stage.GRACE_PERIOD
    clock[0] = 101.0
    watcher.step()
    assert calls["ended"] == [ShutdownReason.GRACE_EXPIRED]
    # L'ultimo istante attivo è a t=30: 71 secondi prima della chiusura
    assert watcher.seconds_since_last_active() == pytest.approx(71.0)


def test_mic_signal_can_be_disabled() -> None:
    watcher, calls, _ = _make_watcher(mic_checker=lambda: True, use_mic_signal=False)
    watcher.step()
    assert watcher.stage == Stage.IDLE
    assert calls["started"] == []


def test_callbacks_run_outside_watcher_lock() -> None:
    """Le callback non devono essere eseguite tenendo il lock del watcher."""
    holder: dict[str, TeamsCallWatcher] = {}
    observed: list[bool] = []

    def _on_started(_m: MeetingMetadata, _manual: bool) -> None:
        # Un altro thread deve poter usare il watcher mentre la callback è in corso
        thread = threading.Thread(target=holder["w"].request_finalize)
        thread.start()
        thread.join(timeout=2)
        observed.append(not thread.is_alive())

    watcher, _, _ = _make_watcher(on_call_started=_on_started, mic_checker=lambda: True)
    holder["w"] = watcher
    watcher.step()
    assert observed == [True]


def test_watcher_does_not_query_outlook_before_recording() -> None:
    """B25: senza fetcher esplicito la registrazione parte con metadati di fallback, senza Outlook."""
    watcher, calls, _ = _make_watcher(mic_checker=lambda: True)
    watcher.step()
    started = calls["started"][0]
    assert isinstance(started, tuple)
    metadata, manual = started
    assert manual is False
    assert isinstance(metadata, MeetingMetadata) and metadata.source == "fallback"


def test_grace_split_when_meeting_changes() -> None:
    """B6: se durante il grace il segnale riprende ma il meeting in calendario è cambiato, A chiude e parte B."""
    signal = [True]
    changed = [False]
    watcher, calls, clock = _make_watcher(
        mic_checker=lambda: signal[0],
        split_checker=lambda: changed[0],
    )
    watcher.step()
    signal[0] = False
    clock[0] = 10.0
    watcher.step()
    assert watcher.stage == Stage.GRACE_PERIOD

    # Ripresa con lo stesso meeting: stessa sessione
    signal[0] = True
    clock[0] = 20.0
    watcher.step()
    assert watcher.stage == Stage.RECORDING
    assert calls["ended"] == [] and len(calls["resumed"]) == 1

    # Nuova pausa, poi ripresa con un meeting diverso: split
    signal[0] = False
    clock[0] = 30.0
    watcher.step()
    changed[0] = True
    signal[0] = True
    clock[0] = 40.0
    watcher.step()
    assert calls["ended"] == [ShutdownReason.CALL_ENDED]
    assert len(calls["started"]) == 2
    assert watcher.stage == Stage.RECORDING


def test_clean_outlook_agenda_removes_teams_join_block() -> None:
    """B8: link, ID riunione, passcode e numeri dial-in non devono arrivare a Ollama né nel DOCX."""
    body = (
        "Ciao a tutti,\r\nordine del giorno:\r\n1. Stato attributi ACR\r\n2. Piano rilascio\r\n\r\n"
        "________________________________________________________________________________\r\n"
        "Riunione di Microsoft Teams\r\n"
        "Partecipa dal computer, dall'app per dispositivi mobili o dal dispositivo della stanza\r\n"
        "Fai clic qui per partecipare alla riunione<https://teams.microsoft.com/l/meetup-join/19%3a>\r\n"
        "ID riunione: 312 456 789 012\r\nPasscode: Ab3dEf\r\n"
        "Scarica Teams<https://www.microsoft.com/microsoft-teams/download-app> | Partecipa sul Web\r\n"
        "Oppure chiama (solo audio)\r\n+39 02 0062 4567,,123456789#   Italy, Milan\r\n"
        "ID conferenza telefonica: 123 456 789#\r\n"
        "________________________________________________________________________________\r\n"
    )
    cleaned = clean_outlook_agenda(body)
    assert "Stato attributi ACR" in cleaned
    assert "Piano rilascio" in cleaned
    for noise in ("Passcode", "Ab3dEf", "312 456", "https://", "+39", "Teams"):
        assert noise not in cleaned
    assert clean_outlook_agenda(cleaned) == cleaned

    only_noise = "Microsoft Teams meeting\nJoin on your computer\nMeeting ID: 1 2 3\nPasscode: x\n"
    assert clean_outlook_agenda(only_noise) == ""


def _install_fake_outlook(monkeypatch: pytest.MonkeyPatch, appointments: list[object]) -> None:
    class _PythonCom:
        @staticmethod
        def CoInitialize() -> None:
            return None

        @staticmethod
        def CoUninitialize() -> None:
            return None

    class _Items:
        IncludeRecurrences = False

        def Sort(self, _f: str) -> None:
            return None

        def Restrict(self, _flt: str) -> list[object]:
            return appointments

    class _Folder:
        Items = _Items()

    class _Namespace:
        def GetDefaultFolder(self, _id: int) -> _Folder:
            return _Folder()

    class _App:
        def GetNamespace(self, _name: str) -> _Namespace:
            return _Namespace()

    class _Client:
        @staticmethod
        def Dispatch(_name: str) -> _App:
            return _App()

    class _Win32:
        client = _Client

    monkeypatch.setattr("modules.watcher.pythoncom", _PythonCom)
    monkeypatch.setattr("modules.watcher.win32com", _Win32)


class _Appointment:
    def __init__(self, subject: str, start: datetime, end: datetime, body: str = "") -> None:
        self.Subject = subject
        self.Organizer = "Org"
        self.RequiredAttendees = ""
        self.OptionalAttendees = ""
        self.Body = body
        self.Start = start
        self.End = end
        self.AllDayEvent = False


def test_outlook_match_rejects_future_meeting_beyond_tolerance(
    fixed_now: datetime,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """B7: alle 12:01 un meeting delle 12:30 non va abbinato; uno delle 12:08 sì (tolleranza 10 min)."""
    ref = fixed_now.replace(hour=12, minute=1)
    far_future = _Appointment("Attributi ACR - Allineamento", ref + timedelta(minutes=29), ref + timedelta(minutes=59))
    _install_fake_outlook(monkeypatch, [far_future])
    assert fetch_outlook_meeting_metadata(reference_time=ref).source == "fallback"

    near_future = _Appointment("Sync rapido", ref + timedelta(minutes=7), ref + timedelta(minutes=37))
    _install_fake_outlook(monkeypatch, [far_future, near_future])
    assert fetch_outlook_meeting_metadata(reference_time=ref).title == "Sync rapido"

    ongoing = _Appointment(
        "Call in corso",
        ref - timedelta(minutes=1),
        ref + timedelta(minutes=29),
        body="Agenda\n\nMicrosoft Teams meeting\nMeeting ID: 1",
    )
    _install_fake_outlook(monkeypatch, [near_future, ongoing])
    selected = fetch_outlook_meeting_metadata(reference_time=ref)
    assert selected.title == "Call in corso"
    assert selected.agenda == "Agenda"
    assert fetch_outlook_meeting_metadata(reference_time=ref, tolerance_min=0).title == "Call in corso"


def test_reset_to_idle_is_reported() -> None:
    """Dopo la chiusura della call tray e cruscotto devono tornare su "In attesa"."""
    reporter = _Reporter()
    watcher, _, _ = _make_watcher(stage_reporter=reporter, mic_checker=lambda: True)
    watcher.step()
    watcher.request_finalize()
    watcher.step()
    assert reporter.history[-1] == Stage.FINALIZING_AUDIO
    watcher.reset_to_idle()
    assert reporter.history[-1] == Stage.IDLE


def test_outlook_pywintypes_local_time_not_shifted_by_fake_utc_tzinfo(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """pywin32 restituisce Start/End in ora locale ma con TimeZoneInfo('GMT Standard Time') (+00:00):
    non deve essere applicato astimezone (+1h/+2h) che associava la call precedente a quella corrente.
    """
    from datetime import timezone, tzinfo

    class TimeZoneInfo(tzinfo):
        """Simula pywintypes.TimeZoneInfo('GMT Standard Time', True) che espone offset 0 su orari locali."""

        def utcoffset(self, _dt: datetime | None) -> timedelta:
            return timedelta(0)

        def dst(self, _dt: datetime | None) -> timedelta:
            return timedelta(0)

        def tzname(self, _dt: datetime | None) -> str:
            return "GMT Standard Time"

    fake_pywin_tz = TimeZoneInfo()
    cet = timezone(timedelta(hours=1))

    # Come in Outlook COM reale: Start/End contengono 15:00 e 16:00 (ora locale), ma tzinfo è TimeZoneInfo (+00:00)
    prev_call = _Appointment(
        "DP Corp - Allocato Conferito",
        datetime(2026, 10, 28, 15, 0, tzinfo=fake_pywin_tz),
        datetime(2026, 10, 28, 16, 0, tzinfo=fake_pywin_tz),
    )
    curr_call = _Appointment(
        "Enilive | Internal - Soluzione Monitoraggio - Follow-up",
        datetime(2026, 10, 28, 16, 0, tzinfo=fake_pywin_tz),
        datetime(2026, 10, 28, 16, 30, tzinfo=fake_pywin_tz),
    )
    _install_fake_outlook(monkeypatch, [prev_call, curr_call])

    # Alle 15:01 deve abbinare la call delle 15:00-16:00
    ref_1501 = datetime(2026, 10, 28, 15, 1, 45, tzinfo=cet)
    matched_1501 = fetch_outlook_meeting_metadata(reference_time=ref_1501, tolerance_min=15)
    assert matched_1501.source == "outlook"
    assert matched_1501.title == "DP Corp - Allocato Conferito"
    assert matched_1501.scheduled_start == datetime(2026, 10, 28, 15, 0, tzinfo=cet)
    assert matched_1501.scheduled_end == datetime(2026, 10, 28, 16, 0, tzinfo=cet)

    # Alle 16:03 deve abbinare la call corrente (16:00-16:30) e NON quella precedente (15:00-16:00)
    ref_1603 = datetime(2026, 10, 28, 16, 3, 12, tzinfo=cet)
    matched_1603 = fetch_outlook_meeting_metadata(reference_time=ref_1603, tolerance_min=15)
    assert matched_1603.source == "outlook"
    assert matched_1603.title == "Enilive | Internal - Soluzione Monitoraggio - Follow-up"
    assert matched_1603.scheduled_start == datetime(2026, 10, 28, 16, 0, tzinfo=cet)
    assert matched_1603.scheduled_end == datetime(2026, 10, 28, 16, 30, tzinfo=cet)


def test_outlook_back_to_back_calls_prefers_starting_call_near_boundary(
    fixed_now: datetime,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tra due call consecutive (15:00-16:00 e 16:00-16:30), entrando alle 15:58 o alle 16:00
    viene associata la call entrante delle 16:00 e non quella in chiusura delle 15:00.
    """
    base_1500 = fixed_now.replace(hour=15, minute=0, second=0, microsecond=0)
    call_a = _Appointment("Call Precedente 15-16", base_1500, base_1500 + timedelta(hours=1))
    call_b = _Appointment(
        "Call Corrente 16-16:30",
        base_1500 + timedelta(hours=1),
        base_1500 + timedelta(hours=1, minutes=30),
    )
    _install_fake_outlook(monkeypatch, [call_a, call_b])

    # Ingresso anticipato di 2 minuti (15:58) nella call delle 16:00
    at_1558 = base_1500 + timedelta(minutes=58)
    matched_early = fetch_outlook_meeting_metadata(reference_time=at_1558, tolerance_min=15)
    assert matched_early.title == "Call Corrente 16-16:30"

    # Ingresso esatto al confine d'ora (16:00:00)
    at_1600 = base_1500 + timedelta(hours=1)
    matched_boundary = fetch_outlook_meeting_metadata(reference_time=at_1600, tolerance_min=15)
    assert matched_boundary.title == "Call Corrente 16-16:30"

