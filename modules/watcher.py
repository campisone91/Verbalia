from __future__ import annotations

import ctypes
import dataclasses
import math
import re
import threading
import time
from collections.abc import Callable, Mapping
from datetime import datetime, timedelta
from enum import Enum

try:
    import psutil
except ImportError:  # pragma: no cover - gestito in ambienti privi di psutil
    class _PsutilFallback:
        class NoSuchProcess(Exception):
            pass

        class AccessDenied(Exception):
            pass

        class ZombieProcess(Exception):
            pass

        @staticmethod
        def process_iter(attrs: list[str] | None = None) -> list[object]:
            return []

    psutil = _PsutilFallback()  # type: ignore[assignment]

from config import (
    OUTLOOK_AGENDA_MAX_CHARS,
    OUTLOOK_CALENDAR_FOLDER_ID,
    OUTLOOK_INCLUDE_ALL_DAY_EVENTS,
    OUTLOOK_MATCH_TOLERANCE_MIN,
    START_DEBOUNCE_SEC,
    STOP_GRACE_PERIOD_SEC,
    TEAMS_CONFIDENCE_THRESHOLD,
    TEAMS_MIC_CONSENT_KEYS,
    TEAMS_PROCESS_NAMES,
    TEAMS_SIGNAL_WEIGHTS,
    WATCHER_POLL_INTERVAL_SEC,
    WATCHER_USE_MIC_SIGNAL,
    validate_signal_weights,
)
from modules.contracts import (
    MeetingMetadata,
    ShutdownReason,
    Stage,
    StageReporter,
    now_tz,
)
from modules.logging_setup import get_logger

try:
    import winreg
except ImportError:  # pragma: no cover - gestito su piattaforme non Windows
    winreg = None  # type: ignore[assignment]

try:
    import pythoncom
except ImportError:  # pragma: no cover - gestito in ambienti senza pywin32
    pythoncom = None  # type: ignore[assignment]

try:
    import win32com.client
except ImportError:  # pragma: no cover - gestito in ambienti senza pywin32
    win32com = None  # type: ignore[assignment]

try:
    from pycaw.pycaw import AudioUtilities, IAudioMeterInformation
except ImportError:  # pragma: no cover - gestito in ambienti senza pycaw
    AudioUtilities = None  # type: ignore[assignment]
    IAudioMeterInformation = None  # type: ignore[assignment]

logger = get_logger("watcher")

_ACTIVE_AUDIO_SESSION_STATE: int = 1
_PEAK_ACTIVITY_EPSILON: float = 1e-4


class WatcherTransition(str, Enum):
    """Transizioni di stato notificate dal watcher (Sezione 19 / F5)."""

    NONE = "none"
    CALL_STARTED = "call_started"
    CALL_RESUMED = "call_resumed"
    SUSPECTED_END = "suspected_end"
    CALL_ENDED = "call_ended"


@dataclasses.dataclass(frozen=True)
class WatcherSnapshot:
    """Istantanea immutabile dello stato corrente del watcher."""

    state: Stage
    transition: WatcherTransition = WatcherTransition.NONE
    reason: str = ""


def compute_confidence_score(
    active_signals: Mapping[str, bool],
    weights: Mapping[str, float] | None = None,
) -> float:
    """Calcola il confidence score per il rilevamento call Teams (Sezione 19.2).

    Il solo segnale Outlook non può mai portare lo score sopra la soglia
    di avvio automatico della registrazione.
    """
    target_weights: Mapping[str, float] = (
        weights if weights is not None else TEAMS_SIGNAL_WEIGHTS
    )
    validate_signal_weights(dict(target_weights))

    score = sum(
        float(weight)
        for signal, weight in target_weights.items()
        if bool(active_signals.get(signal, False))
    )

    non_outlook_active = any(
        bool(active_signals.get(signal, False))
        for signal in target_weights
        if signal != "outlook_event"
    )
    if bool(active_signals.get("outlook_event", False)) and not non_outlook_active:
        score = min(score, TEAMS_CONFIDENCE_THRESHOLD - 0.01)

    return round(max(0.0, min(1.0, score)), 6)


def is_teams_process_running(
    process_names: set[str] | None = None,
) -> bool:
    """Verifica se almeno un processo Microsoft Teams è in esecuzione."""
    target_names = {
        name.strip().lower()
        for name in (
            process_names if process_names is not None else TEAMS_PROCESS_NAMES
        )
        if name.strip()
    }
    if not target_names:
        return False

    try:
        iterator = psutil.process_iter(["name"])
    except Exception as exc:
        logger.warning("Impossibile enumerare i processi di sistema: %s", exc)
        return False

    for proc in iterator:
        try:
            proc_name: str | None = None
            info = getattr(proc, "info", None)
            if isinstance(info, Mapping):
                raw_name = info.get("name")
                if isinstance(raw_name, str):
                    proc_name = raw_name
            if proc_name is None and hasattr(proc, "name"):
                name_attr = getattr(proc, "name")
                proc_name = name_attr() if callable(name_attr) else str(name_attr)
            if proc_name and proc_name.strip().lower() in target_names:
                return True
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            continue
        except OSError as exc:
            logger.debug("Errore OS leggendo processo Teams: %s", exc)
            continue

    return False


def _is_single_session_active(
    session: object,
    target_names: set[str],
) -> bool:
    """Valuta in sicurezza se una singola sessione pycaw appartiene a Teams ed è attiva."""
    try:
        proc = getattr(session, "Process", None)
        if proc is None:
            return False

        name_attr = getattr(proc, "name", None)
        proc_name = name_attr() if callable(name_attr) else name_attr
        if not isinstance(proc_name, str) or proc_name.strip().lower() not in target_names:
            return False
    except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
        return False
    except OSError as exc:
        logger.debug("Errore accesso processo sessione audio: %s", exc)
        return False

    try:
        # Una sessione Teams in stato Active indica una call in corso anche quando nessuno parla:
        # il peak meter da solo chiuderebbe la sessione durante i silenzi o se parla solo l'utente.
        if hasattr(session, "State"):
            state_val = getattr(session, "State")
            if state_val is not None:
                return int(state_val) == _ACTIVE_AUDIO_SESSION_STATE

        if hasattr(session, "is_active"):
            raw_active = getattr(session, "is_active")
            active_flag = raw_active() if callable(raw_active) else raw_active
            if not bool(active_flag):
                return False

        if hasattr(session, "peak_value"):
            peak_val = getattr(session, "peak_value")
            if peak_val is not None:
                return float(peak_val) > _PEAK_ACTIVITY_EPSILON

        if hasattr(session, "audio_meter"):
            meter = getattr(session, "audio_meter")
            if meter is not None and hasattr(meter, "GetPeakValue"):
                return float(meter.GetPeakValue()) > _PEAK_ACTIVITY_EPSILON

        ctl = getattr(session, "_ctl", None)
        if ctl is not None and IAudioMeterInformation is not None and hasattr(ctl, "QueryInterface"):
            try:
                meter_interface = ctl.QueryInterface(IAudioMeterInformation)
                peak = float(meter_interface.GetPeakValue())
                if peak > _PEAK_ACTIVITY_EPSILON:
                    return True
            except Exception as meter_exc:
                logger.debug("Lettura IAudioMeterInformation non riuscita: %s", meter_exc)

        return True
    except (psutil.NoSuchProcess, psutil.AccessDenied, OSError) as exc:
        logger.debug("Sessione audio Teams non più accessibile: %s", exc)
        return False
    except Exception as exc:
        logger.debug("Errore imprevisto ispezionando sessione audio Teams: %s", exc)
        return False


def is_teams_audio_active(
    process_names: set[str] | None = None,
) -> bool:
    """Verifica tramite pycaw se esiste almeno una sessione audio Teams attiva (Sezione 19.3)."""
    target_names = {
        name.strip().lower()
        for name in (
            process_names if process_names is not None else TEAMS_PROCESS_NAMES
        )
        if name.strip()
    }
    if not target_names or AudioUtilities is None:
        return False

    com_initialized = False
    if pythoncom is not None:
        try:
            pythoncom.CoInitialize()
            com_initialized = True
        except Exception as exc:
            logger.debug("CoInitialize in is_teams_audio_active non necessario/fallito: %s", exc)

    try:
        sessions = AudioUtilities.GetAllSessions()
        if not sessions:
            return False
        return any(
            _is_single_session_active(session, target_names)
            for session in sessions
        )
    except Exception as exc:
        logger.warning("Errore durante l'enumerazione delle sessioni pycaw: %s", exc)
        return False
    finally:
        if com_initialized and pythoncom is not None:
            try:
                pythoncom.CoUninitialize()
            except Exception as exc:
                logger.debug("Errore in CoUninitialize (pycaw): %s", exc)


_MIC_CONSENT_ROOT: str = (
    r"Software\Microsoft\Windows\CurrentVersion\CapabilityAccessManager"
    r"\ConsentStore\microphone"
)


def _consent_key_in_use(key: winreg.HKEYType) -> bool:
    """Una voce ConsentStore è 'in uso' se LastUsedTimeStart>0 e LastUsedTimeStop==0."""
    assert winreg is not None
    try:
        start_val, _ = winreg.QueryValueEx(key, "LastUsedTimeStart")
        stop_val, _ = winreg.QueryValueEx(key, "LastUsedTimeStop")
    except OSError:
        return False
    try:
        return int(start_val) > 0 and int(stop_val) == 0
    except (TypeError, ValueError):
        return False


def is_teams_microphone_in_use(
    app_keys: tuple[str, ...] | None = None,
    process_names: set[str] | None = None,
) -> bool:
    """Verifica tramite il registro (CapabilityAccessManager) se Teams sta usando il microfono.

    Copre il nuovo Teams (app pacchettizzata MSTeams_8wekyb3d8bbwe) e il Teams classico
    (voci NonPackaged con il percorso dell'eseguibile). Il segnale resta attivo anche con
    il microfono silenziato in call, quindi integra il rilevamento basato sulle sessioni audio.
    """
    if winreg is None:
        return False
    packaged = app_keys if app_keys is not None else TEAMS_MIC_CONSENT_KEYS
    target_names = {
        name.strip().lower()
        for name in (process_names if process_names is not None else TEAMS_PROCESS_NAMES)
        if name.strip()
    }
    try:
        root = winreg.OpenKey(winreg.HKEY_CURRENT_USER, _MIC_CONSENT_ROOT)
    except OSError:
        return False

    try:
        for app_key in packaged:
            try:
                with winreg.OpenKey(root, app_key) as key:
                    if _consent_key_in_use(key):
                        return True
            except OSError:
                continue

        try:
            non_packaged = winreg.OpenKey(root, "NonPackaged")
        except OSError:
            return False
        with non_packaged:
            index = 0
            while True:
                try:
                    sub_name = winreg.EnumKey(non_packaged, index)
                except OSError:
                    break
                index += 1
                exe_name = sub_name.rsplit("#", 1)[-1].lower()
                if exe_name not in target_names:
                    continue
                try:
                    with winreg.OpenKey(non_packaged, sub_name) as key:
                        if _consent_key_in_use(key):
                            return True
                except OSError:
                    continue
        return False
    except Exception as exc:
        logger.debug("Lettura stato microfono Teams dal registro non riuscita: %s", exc)
        return False
    finally:
        try:
            winreg.CloseKey(root)
        except OSError:
            pass


_AGENDA_CUT_MARKERS: tuple[str, ...] = (
    "riunione di microsoft teams",
    "microsoft teams meeting",
    "microsoft teams need help",
    "microsoft teams serve aiuto",
    "partecipa alla riunione ora",
    "join the meeting now",
    "fai clic qui per partecipare",
    "click here to join the meeting",
)
_AGENDA_NOISE_LINE_RE: re.Pattern[str] = re.compile(
    r"(meeting id|id riunione|id della riunione|passcode|password|pin:|"
    r"dial[- ]?in|dial by your location|chiama \(solo audio\)|call in \(audio only\)|"
    r"oppure chiama|or call in|trova un numero locale|find a local number|"
    r"id conferenza|phone conference id|conference id|scarica teams|download teams|"
    r"partecipa sul web|join on the web|opzioni riunione|meeting options|"
    r"ulteriori informazioni|learn more|reimposta pin|reset pin|"
    r"partecipa dal computer|join on your computer|dispositivo della stanza|room device|"
    r"per gli organizzatori|for organizers|"
    r"teams\.microsoft\.com|aka\.ms/|microsoft\.com/)",
    re.IGNORECASE,
)
_URL_RE: re.Pattern[str] = re.compile(r"<?(?:https?://|mailto:|tel:)[^\s>]*>?", re.IGNORECASE)
_PHONE_LINE_RE: re.Pattern[str] = re.compile(r"^[+(]?\d[\d\s().,#*-]{6,}.*$")
_SEPARATOR_LINE_RE: re.Pattern[str] = re.compile(r"^[\s_\-=*~.]{8,}$")


def clean_outlook_agenda(raw_body: str, max_chars: int = OUTLOOK_AGENDA_MAX_CHARS) -> str:
    """Ripulisce il Body Outlook dal blocco di accesso Teams (link, ID, passcode, numeri dial-in).

    Il risultato contiene solo il testo scritto dall'organizzatore: il rumore dell'invito
    sprecherebbe contesto LLM e finirebbe stampato nel DOCX (passcode compreso).
    L'operazione è idempotente.
    """
    if not isinstance(raw_body, str) or not raw_body.strip():
        return ""

    text = raw_body.replace("\r\n", "\n").replace("\r", "\n").replace("\xa0", " ")
    lower = text.lower()
    cut_positions = [lower.find(marker) for marker in _AGENDA_CUT_MARKERS if marker in lower]
    if cut_positions:
        text = text[: min(cut_positions)]

    kept: list[str] = []
    for raw_line in text.split("\n"):
        line = raw_line.strip()
        if not line:
            if kept and kept[-1]:
                kept.append("")
            continue
        if _SEPARATOR_LINE_RE.match(line):
            continue
        if _AGENDA_NOISE_LINE_RE.search(line):
            continue
        line = _URL_RE.sub("", line).strip()
        if not line or _PHONE_LINE_RE.match(line):
            continue
        if not any(ch.isalpha() for ch in line):
            continue
        kept.append(" ".join(line.split()))

    cleaned = "\n".join(kept).strip()
    if max_chars > 0 and len(cleaned) > max_chars:
        cleaned = cleaned[:max_chars].rstrip()
    return cleaned


def has_teams_call_window_title() -> bool:
    """Rileva se esiste una finestra visibile riconducibile a Microsoft Teams."""
    try:
        user32 = getattr(ctypes, "windll", None)
        if user32 is None or not hasattr(user32, "user32"):
            return False
        u32 = user32.user32

        titles: list[str] = []
        enum_proc_type = ctypes.WINFUNCTYPE(
            ctypes.c_bool,
            ctypes.c_void_p,
            ctypes.c_void_p,
        )

        def _callback(hwnd: int, _lparam: int) -> bool:
            if not u32.IsWindowVisible(hwnd):
                return True
            length = int(u32.GetWindowTextLengthW(hwnd))
            if length <= 0:
                return True
            buffer = ctypes.create_unicode_buffer(length + 1)
            u32.GetWindowTextW(hwnd, buffer, length + 1)
            text = buffer.value.strip()
            if text:
                titles.append(text.lower())
            return True

        cb = enum_proc_type(_callback)
        u32.EnumWindows(cb, 0)

        call_keywords = (
            "microsoft teams",
            "| teams",
            "riunione",
            "meeting",
            "chiamata",
            "call",
        )
        return any(
            "teams" in title and any(kw in title for kw in call_keywords)
            for title in titles
        )
    except Exception as exc:
        logger.debug("Impossibile ispezionare i titoli delle finestre: %s", exc)
        return False


def _split_attendees(raw_attendees: object) -> list[str]:
    """Suddivide una stringa di partecipanti separata da punto e virgola."""
    if not isinstance(raw_attendees, str) or not raw_attendees.strip():
        return []
    result: list[str] = []
    for part in raw_attendees.split(";"):
        cleaned = part.strip()
        if cleaned and cleaned not in result:
            result.append(cleaned)
    return result


def _coerce_outlook_datetime(
    raw_dt: object,
    reference_tz_dt: datetime,
) -> datetime | None:
    """Converte un oggetto data/ora restituito da pywin32 in datetime timezone-aware."""
    if raw_dt is None:
        return None
    if isinstance(raw_dt, datetime):
        tz = raw_dt.tzinfo
        # pywin32 restituisce pywintypes.datetime per item.Start/End con l'ora locale
        # ma con tzinfo=TimeZoneInfo('GMT Standard Time', True) (+00:00): non va convertito con astimezone.
        if (
            tz is None
            or type(raw_dt).__module__.startswith("pywintypes")
            or type(tz).__name__ == "TimeZoneInfo"
        ):
            return datetime(
                raw_dt.year,
                raw_dt.month,
                raw_dt.day,
                raw_dt.hour,
                raw_dt.minute,
                raw_dt.second,
                raw_dt.microsecond,
                tzinfo=reference_tz_dt.tzinfo,
            )
        return raw_dt.astimezone(reference_tz_dt.tzinfo)
    if isinstance(raw_dt, str) and raw_dt.strip():
        try:
            parsed = datetime.fromisoformat(raw_dt.strip())
            if parsed.tzinfo is None:
                return parsed.replace(tzinfo=reference_tz_dt.tzinfo)
            return parsed.astimezone(reference_tz_dt.tzinfo)
        except ValueError:
            return None
    return None


def _outlook_match_sort_key(
    item_start: datetime,
    item_end: datetime,
    ref_time: datetime,
    tolerance: timedelta,
) -> tuple[int, float, float] | None:
    """Classifica un appuntamento rispetto all'istante di riferimento; None se fuori tolleranza.

    0 = in corso (non in coda finale),
    1 = inizia entro la tolleranza oppure in corso ma in coda finale (confrontati per vicinanza all'inizio),
    2 = terminato da meno della tolleranza.
    """
    if item_start <= ref_time < item_end:
        elapsed = ref_time - item_start
        remaining = item_end - ref_time
        if remaining <= tolerance and elapsed > remaining:
            category = 1
        else:
            category = 0
    elif ref_time < item_start <= ref_time + tolerance:
        category = 1
    elif item_end <= ref_time <= item_end + tolerance:
        category = 2
    else:
        return None
    start_distance = abs((item_start - ref_time).total_seconds())
    duration_sec = max(0.0, (item_end - item_start).total_seconds())
    return (category, start_distance, duration_sec)


def fetch_outlook_meeting_metadata(
    reference_time: datetime | None = None,
    *,
    tolerance_min: int | None = None,
) -> MeetingMetadata:
    """Recupera i metadati della riunione corrente da Outlook tramite COM (Sezione 19.6).

    Ha la priorità il meeting in corso a reference_time; un meeting futuro è accettato solo
    se inizia entro tolerance_min minuti. In caso di errore o nessun evento compatibile
    restituisce il fallback sicuro. Non utilizza mai il campo [Body] nel filtro Restrict.
    """
    fallback = MeetingMetadata(
        title="Riunione Teams",
        source="fallback",
    )

    if pythoncom is None or win32com is None:
        return fallback

    ref_time = reference_time if reference_time is not None else now_tz()
    if ref_time.tzinfo is None:
        ref_time = ref_time.astimezone()

    tolerance = timedelta(
        minutes=max(
            0,
            int(tolerance_min if tolerance_min is not None else OUTLOOK_MATCH_TOLERANCE_MIN),
        )
    )
    window_start = ref_time - tolerance
    window_end = ref_time + tolerance

    com_initialized = False
    try:
        pythoncom.CoInitialize()
        com_initialized = True

        outlook_app = win32com.client.Dispatch("Outlook.Application")
        namespace = outlook_app.GetNamespace("MAPI")
        calendar_folder = namespace.GetDefaultFolder(OUTLOOK_CALENDAR_FOLDER_ID)
        items = calendar_folder.Items
        items.Sort("[Start]")
        items.IncludeRecurrences = True

        start_filter = window_end.strftime("%m/%d/%Y %I:%M %p")
        end_filter = window_start.strftime("%m/%d/%Y %I:%M %p")
        restriction = (
            f"[Start] <= '{start_filter}' AND [End] >= '{end_filter}'"
        )
        restricted_items = items.Restrict(restriction)

        candidates: list[tuple[tuple[int, float, float], MeetingMetadata]] = []
        for item in restricted_items:
            try:
                is_all_day = bool(getattr(item, "AllDayEvent", False))
                if is_all_day and not OUTLOOK_INCLUDE_ALL_DAY_EVENTS:
                    continue

                item_start = _coerce_outlook_datetime(
                    getattr(item, "Start", None),
                    ref_time,
                )
                item_end = _coerce_outlook_datetime(
                    getattr(item, "End", None),
                    ref_time,
                )

                sort_key: tuple[int, float, float]
                if item_start is not None and item_end is not None:
                    match_key = _outlook_match_sort_key(item_start, item_end, ref_time, tolerance)
                    if match_key is None:
                        continue
                    sort_key = match_key
                else:
                    sort_key = (3, float("inf"), float("inf"))

                raw_subject = str(getattr(item, "Subject", "") or "").strip()
                title = raw_subject if raw_subject else "Riunione Teams"
                organizer = (
                    str(getattr(item, "Organizer", "") or "").strip()
                    or "Non disponibile"
                )
                required_attendees = _split_attendees(
                    getattr(item, "RequiredAttendees", "")
                )
                optional_attendees = _split_attendees(
                    getattr(item, "OptionalAttendees", "")
                )
                raw_body = str(getattr(item, "Body", "") or "").strip()
                agenda = clean_outlook_agenda(raw_body)

                metadata = MeetingMetadata(
                    title=title,
                    organizer=organizer,
                    required_attendees=required_attendees,
                    optional_attendees=optional_attendees,
                    agenda=agenda,
                    scheduled_start=item_start,
                    scheduled_end=item_end,
                    source="outlook",
                )

                candidates.append((sort_key, metadata))
            except Exception as item_exc:
                logger.debug("Elemento calendario Outlook ignorato: %s", item_exc)
                continue

        if not candidates:
            return fallback

        candidates.sort(key=lambda pair: pair[0])
        return candidates[0][1]
    except Exception as exc:
        logger.warning(
            "Impossibile leggere i metadati da Outlook COM, uso fallback: %s",
            exc,
        )
        return fallback
    finally:
        if com_initialized:
            try:
                pythoncom.CoUninitialize()
            except Exception as exc:
                logger.debug("Errore durante CoUninitialize (Outlook): %s", exc)


class TeamsCallWatcher:
    """Monitora i segnali di una call Microsoft Teams con debounce e grace period (Sezione 19).

    step() valuta i segnali e decide le azioni sotto lock, ma esegue le callback
    (avvio/chiusura registrazione) solo dopo averlo rilasciato: una callback lenta
    non blocca così request_manual_start/request_finalize invocati da altri thread.
    """

    def __init__(
        self,
        stage_reporter: StageReporter,
        on_call_started: Callable[[MeetingMetadata, bool], None],
        on_call_resumed: Callable[[], None],
        on_call_suspected_end: Callable[[float], None],
        on_call_ended: Callable[[ShutdownReason], None],
        shutdown_event: threading.Event,
        *,
        poll_interval_sec: float | None = None,
        start_debounce_sec: float | None = None,
        stop_grace_period_sec: float | None = None,
        process_checker: Callable[[], bool] | None = None,
        audio_checker: Callable[[], bool] | None = None,
        window_checker: Callable[[], bool] | None = None,
        mic_checker: Callable[[], bool] | None = None,
        use_mic_signal: bool | None = None,
        metadata_fetcher: Callable[[], MeetingMetadata] | None = None,
        split_checker: Callable[[], bool] | None = None,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self._stage_reporter = stage_reporter
        self._on_call_started = on_call_started
        self._on_call_resumed = on_call_resumed
        self._on_call_suspected_end = on_call_suspected_end
        self._on_call_ended = on_call_ended
        self._shutdown_event = shutdown_event

        self._poll_interval_sec: float = (
            float(poll_interval_sec)
            if poll_interval_sec is not None
            else float(WATCHER_POLL_INTERVAL_SEC)
        )
        self._start_debounce_sec: float = (
            float(start_debounce_sec)
            if start_debounce_sec is not None
            else float(START_DEBOUNCE_SEC)
        )
        self._stop_grace_period_sec: float = (
            float(stop_grace_period_sec)
            if stop_grace_period_sec is not None
            else float(STOP_GRACE_PERIOD_SEC)
        )
        self._use_mic_signal: bool = (
            bool(use_mic_signal) if use_mic_signal is not None else bool(WATCHER_USE_MIC_SIGNAL)
        )

        self._process_checker: Callable[[], bool] = (
            process_checker if process_checker is not None else is_teams_process_running
        )
        self._audio_checker: Callable[[], bool] = (
            audio_checker if audio_checker is not None else is_teams_audio_active
        )
        self._window_checker: Callable[[], bool] = (
            window_checker if window_checker is not None else has_teams_call_window_title
        )
        # Risolto a ogni chiamata: i test possono sostituire la funzione di modulo
        self._mic_checker: Callable[[], bool] = (
            mic_checker if mic_checker is not None else (lambda: is_teams_microphone_in_use())
        )
        # Nessuna lettura Outlook di default: i metadati vengono letti dopo l'avvio della
        # registrazione, fuori dal lock (B25). Un fetcher esplicito resta supportato.
        self._metadata_fetcher: Callable[[], MeetingMetadata] | None = metadata_fetcher
        self._split_checker: Callable[[], bool] | None = split_checker
        self._clock: Callable[[], float] = (
            clock if clock is not None else time.monotonic
        )

        self._lock = threading.RLock()
        self._stage: Stage = Stage.IDLE
        self._manual_recording: bool = False
        self._finalize_requested: bool = False
        self._manual_start_requested: bool = False
        self._manual_finalize_requested: bool = False
        self._suppress_until_call_ends: bool = False
        self._auto_detect_enabled: bool = True
        self._recording_paused: bool = False

        self.candidate_started_at_monotonic: float | None = None
        self.last_active_at_monotonic: float | None = None
        self.grace_started_at_monotonic: float | None = None
        self.last_confidence_score: float = 0.0
        self.last_signals: dict[str, bool] = {}

    @property
    def stage(self) -> Stage:
        """Restituisce lo stage interno corrente del watcher."""
        with self._lock:
            return self._stage

    @property
    def is_recording_paused(self) -> bool:
        """Indica se la registrazione è attualmente in pausa manuale (F5)."""
        with self._lock:
            return self._recording_paused

    @property
    def _state(self) -> Stage:
        """Alias retrocompatibile per lo stage interno del watcher."""
        with self._lock:
            return self._stage

    @property
    def _grace_started_at(self) -> float | None:
        """Alias retrocompatibile per l'istante di avvio del grace period."""
        with self._lock:
            return self.grace_started_at_monotonic

    def set_recording_paused(self, paused: bool) -> None:
        """Imposta o rimuove lo stato di pausa della registrazione nel watcher (F5)."""
        with self._lock:
            self._recording_paused = bool(paused)
            if self._recording_paused:
                if self._stage == Stage.GRACE_PERIOD:
                    self._stage = Stage.RECORDING
                    self._stage_reporter.set_stage(
                        Stage.RECORDING, "Registrazione in pausa: grace period sospeso"
                    )
                self.grace_started_at_monotonic = None
            else:
                self.grace_started_at_monotonic = None

    @property
    def is_call_active(self) -> bool:
        """Indica se il watcher considera in corso una registrazione (anche in grace period)."""
        with self._lock:
            return self._stage in (
                Stage.RECORDING,
                Stage.RECORDING_AND_TRANSCRIBING,
                Stage.GRACE_PERIOD,
            )

    def apply_settings(
        self,
        *,
        start_debounce_sec: float | None = None,
        stop_grace_period_sec: float | None = None,
        use_mic_signal: bool | None = None,
        auto_record_teams_calls: bool | None = None,
    ) -> None:
        """Aggiorna i parametri di rilevamento; valgono dalla prossima call."""
        with self._lock:
            if start_debounce_sec is not None:
                self._start_debounce_sec = max(0.0, float(start_debounce_sec))
            if stop_grace_period_sec is not None:
                self._stop_grace_period_sec = max(0.0, float(stop_grace_period_sec))
            if use_mic_signal is not None:
                self._use_mic_signal = bool(use_mic_signal)
            if auto_record_teams_calls is not None:
                self._auto_detect_enabled = bool(auto_record_teams_calls)

    def configure(
        self,
        *,
        auto_detect_enabled: bool | None = None,
        start_debounce_sec: float | None = None,
        stop_grace_sec: float | None = None,
        confidence_threshold: float | None = None,
        use_mic_signal: bool | None = None,
        signal_weights: Mapping[str, float] | None = None,
    ) -> None:
        """Configura a caldo i parametri del watcher."""
        del confidence_threshold, signal_weights
        self.apply_settings(
            start_debounce_sec=start_debounce_sec,
            stop_grace_period_sec=stop_grace_sec,
            use_mic_signal=use_mic_signal,
            auto_record_teams_calls=auto_detect_enabled,
        )

    def seconds_since_last_active(self) -> float:
        """Secondi trascorsi dall'ultimo istante in cui la call risultava attiva (0 se ignoto)."""
        with self._lock:
            if self.last_active_at_monotonic is None:
                return 0.0
            return max(0.0, float(self._clock()) - self.last_active_at_monotonic)

    def suppress_current_call(self) -> None:
        """Impedisce il riavvio automatico sulla call corrente finché non termina."""
        with self._lock:
            self._suppress_until_call_ends = True
            self._manual_recording = False
            self._finalize_requested = False
            self._manual_start_requested = False
            self._manual_finalize_requested = False
            self._recording_paused = False
            self.candidate_started_at_monotonic = None
            self.last_active_at_monotonic = None
            self.grace_started_at_monotonic = None
            self.last_confidence_score = 0.0
            self._transition(
                Stage.IDLE,
                "Registrazione interrotta dall'utente; in attesa fine chiamata.",
            )

    def request_manual_start(self) -> None:
        """Richiede l'avvio immediato della registrazione bypassando score e debounce."""
        with self._lock:
            self._manual_recording = True
            self._manual_start_requested = True

    def request_finalize(self) -> None:
        """Richiede la finalizzazione immediata della sessione corrente."""
        with self._lock:
            self._finalize_requested = True
            self._manual_finalize_requested = True

    def reset_to_idle(self, detail: str = "In attesa di call Microsoft Teams") -> None:
        """Riporta il watcher nello stato IDLE azzerando i contatori e lo comunica al reporter."""
        with self._lock:
            self._stage = Stage.IDLE
            self._manual_recording = False
            self._finalize_requested = False
            self._manual_start_requested = False
            self._manual_finalize_requested = False
            self._recording_paused = False
            self.candidate_started_at_monotonic = None
            self.last_active_at_monotonic = None
            self.grace_started_at_monotonic = None
            self.last_confidence_score = 0.0
            self._stage_reporter.set_stage(Stage.IDLE, detail)

    def _resolve_start_metadata(self) -> MeetingMetadata:
        fallback = MeetingMetadata(title="Riunione Teams", source="fallback")
        if self._metadata_fetcher is None:
            return fallback
        try:
            metadata = self._metadata_fetcher()
            if isinstance(metadata, MeetingMetadata):
                return metadata
        except Exception as exc:
            logger.warning("Errore nel recupero metadati dal watcher: %s", exc)
        return fallback

    def _is_call_signal_active(self) -> tuple[bool, dict[str, bool]]:
        process_present = bool(self._process_checker())
        audio_active = bool(self._audio_checker())
        mic_active = False
        if self._use_mic_signal and process_present:
            try:
                mic_active = bool(self._mic_checker())
            except Exception as exc:
                logger.debug("Segnale microfono Teams non disponibile: %s", exc)
        window_title = bool(self._window_checker())
        signals = {
            "process_present": process_present,
            "audio_active": audio_active,
            "mic_active": mic_active,
            "window_title": window_title,
        }
        return process_present and (audio_active or mic_active), signals

    @staticmethod
    def _score(signals: Mapping[str, bool], *, debounce_completed: bool, outlook_event: bool) -> float:
        return compute_confidence_score(
            {
                "process_present": signals.get("process_present", False),
                "audio_active": signals.get("audio_active", False)
                or signals.get("mic_active", False),
                "debounce_completed": debounce_completed,
                "outlook_event": outlook_event,
                "window_title": signals.get("window_title", False),
            }
        )

    def step(self) -> WatcherSnapshot:
        """Esegue un singolo passo deterministico di valutazione del watcher."""
        actions: list[Callable[[], None]] = []
        with self._lock:
            snapshot = self._step_locked(float(self._clock()), actions)
        for action in actions:
            action()
        return snapshot

    def _transition(self, stage: Stage, detail: str) -> None:
        self._stage = stage
        self._stage_reporter.set_stage(stage, detail)

    def _step_locked(self, now_mono: float, actions: list[Callable[[], None]]) -> WatcherSnapshot:
        if self._manual_finalize_requested:
            self._manual_finalize_requested = False
            if self._stage in (
                Stage.RECORDING,
                Stage.RECORDING_AND_TRANSCRIBING,
                Stage.GRACE_PERIOD,
            ):
                self.candidate_started_at_monotonic = None
                self.grace_started_at_monotonic = None
                self._transition(Stage.FINALIZING_AUDIO, "Finalizzazione manuale richiesta")
                actions.append(lambda: self._on_call_ended(ShutdownReason.USER_REQUEST))
                return WatcherSnapshot(
                    state=Stage.FINALIZING_AUDIO,
                    transition=WatcherTransition.CALL_ENDED,
                    reason="Finalizzazione manuale richiesta",
                )
            if self._stage == Stage.START_DEBOUNCE:
                self.candidate_started_at_monotonic = None
                self._transition(Stage.IDLE, "Avvio annullato manualmente")
                return WatcherSnapshot(
                    state=Stage.IDLE,
                    transition=WatcherTransition.NONE,
                    reason="Avvio annullato manualmente",
                )

        if self._manual_start_requested:
            self._manual_start_requested = False
            if self._stage in (Stage.IDLE, Stage.START_DEBOUNCE):
                metadata = self._resolve_start_metadata()
                self.candidate_started_at_monotonic = None
                self.grace_started_at_monotonic = None
                self.last_active_at_monotonic = now_mono
                self.last_confidence_score = 1.0
                self._transition(Stage.RECORDING, f"Registrazione manuale: {metadata.title}")
                actions.append(lambda: self._on_call_started(metadata, True))
                return WatcherSnapshot(
                    state=Stage.RECORDING,
                    transition=WatcherTransition.CALL_STARTED,
                    reason=f"Registrazione manuale: {metadata.title}",
                )
            if self._stage == Stage.GRACE_PERIOD:
                self.grace_started_at_monotonic = None
                self.last_active_at_monotonic = now_mono
                self._transition(Stage.RECORDING, "Registrazione ripresa manualmente")
                actions.append(self._on_call_resumed)
                return WatcherSnapshot(
                    state=Stage.RECORDING,
                    transition=WatcherTransition.CALL_RESUMED,
                    reason="Registrazione ripresa manualmente",
                )

        if self._recording_paused and self._stage in (
            Stage.RECORDING,
            Stage.RECORDING_AND_TRANSCRIBING,
            Stage.GRACE_PERIOD,
        ):
            self.grace_started_at_monotonic = None
            if self._stage == Stage.GRACE_PERIOD:
                self._transition(Stage.RECORDING, "Registrazione in pausa manuale")
            self.last_active_at_monotonic = now_mono
            return WatcherSnapshot(
                state=self._stage,
                transition=WatcherTransition.NONE,
                reason="Registrazione in pausa manuale",
            )

        call_signal_active, signals = self._is_call_signal_active()
        self.last_signals = signals

        if self._stage in (Stage.IDLE, Stage.START_DEBOUNCE):
            if self._suppress_until_call_ends:
                if not call_signal_active:
                    self._suppress_until_call_ends = False
                return WatcherSnapshot(
                    state=self._stage,
                    transition=WatcherTransition.NONE,
                    reason="Call soppressa fino al termine",
                )

            if not self._auto_detect_enabled:
                return WatcherSnapshot(
                    state=self._stage,
                    transition=WatcherTransition.NONE,
                    reason="Rilevamento automatico disabilitato",
                )

            if not call_signal_active:
                self.last_confidence_score = self._score(
                    signals, debounce_completed=False, outlook_event=False
                )
                self.candidate_started_at_monotonic = None
                if self._stage == Stage.START_DEBOUNCE:
                    self._transition(
                        Stage.IDLE,
                        "Segnale Teams interrotto prima del completamento debounce",
                    )
                return WatcherSnapshot(
                    state=self._stage,
                    transition=WatcherTransition.NONE,
                    reason="Segnale Teams assente",
                )

            self.last_active_at_monotonic = now_mono
            if self.candidate_started_at_monotonic is None:
                self.candidate_started_at_monotonic = now_mono
                if self._start_debounce_sec > 0.0:
                    self.last_confidence_score = self._score(
                        signals, debounce_completed=False, outlook_event=False
                    )
                    self._transition(
                        Stage.START_DEBOUNCE,
                        f"Verifica stabilità call ({int(math.ceil(self._start_debounce_sec))}s)",
                    )
                    return WatcherSnapshot(
                        state=Stage.START_DEBOUNCE,
                        transition=WatcherTransition.NONE,
                        reason=f"Verifica stabilità call ({int(math.ceil(self._start_debounce_sec))}s)",
                    )

            elapsed_debounce = now_mono - self.candidate_started_at_monotonic
            if elapsed_debounce < self._start_debounce_sec:
                remaining_debounce = max(0.0, self._start_debounce_sec - elapsed_debounce)
                self.last_confidence_score = self._score(
                    signals, debounce_completed=False, outlook_event=False
                )
                self._transition(
                    Stage.START_DEBOUNCE,
                    f"Verifica stabilità call ({int(math.ceil(remaining_debounce))}s)",
                )
                return WatcherSnapshot(
                    state=Stage.START_DEBOUNCE,
                    transition=WatcherTransition.NONE,
                    reason=f"Verifica stabilità call ({int(math.ceil(remaining_debounce))}s)",
                )

            metadata = self._resolve_start_metadata()
            score = self._score(
                signals,
                debounce_completed=True,
                outlook_event=metadata.source == "outlook",
            )
            self.last_confidence_score = score
            if score >= TEAMS_CONFIDENCE_THRESHOLD:
                self.candidate_started_at_monotonic = None
                self.grace_started_at_monotonic = None
                self._transition(Stage.RECORDING, f"Registrazione attiva: {metadata.title}")
                actions.append(lambda: self._on_call_started(metadata, False))
                return WatcherSnapshot(
                    state=Stage.RECORDING,
                    transition=WatcherTransition.CALL_STARTED,
                    reason=f"Registrazione attiva: {metadata.title}",
                )
            return WatcherSnapshot(
                state=self._stage,
                transition=WatcherTransition.NONE,
                reason="Score insufficiente",
            )

        if self._stage in (Stage.RECORDING, Stage.RECORDING_AND_TRANSCRIBING):
            if call_signal_active:
                self.last_active_at_monotonic = now_mono
                self.grace_started_at_monotonic = None
                return WatcherSnapshot(
                    state=self._stage,
                    transition=WatcherTransition.NONE,
                    reason="Call attiva",
                )

            self.grace_started_at_monotonic = now_mono
            if self._stop_grace_period_sec <= 0.0:
                self.grace_started_at_monotonic = None
                self._transition(Stage.FINALIZING_AUDIO, "Call terminata, finalizzazione audio")
                actions.append(lambda: self._on_call_ended(ShutdownReason.GRACE_EXPIRED))
                return WatcherSnapshot(
                    state=Stage.FINALIZING_AUDIO,
                    transition=WatcherTransition.CALL_ENDED,
                    reason="Call terminata, finalizzazione audio",
                )

            remaining = float(self._stop_grace_period_sec)
            self._transition(
                Stage.GRACE_PERIOD,
                f"Call in pausa o terminata ({int(math.ceil(remaining))}s alla chiusura)",
            )
            actions.append(lambda: self._on_call_suspected_end(remaining))
            return WatcherSnapshot(
                state=Stage.GRACE_PERIOD,
                transition=WatcherTransition.SUSPECTED_END,
                reason=f"Call in pausa o terminata ({int(math.ceil(remaining))}s alla chiusura)",
            )

        if self._stage == Stage.GRACE_PERIOD:
            if call_signal_active:
                self.grace_started_at_monotonic = None
                self.last_active_at_monotonic = now_mono
                if self._split_checker is not None and self._safe_split_check():
                    # B6: il segnale è ripreso ma il meeting in calendario è cambiato:
                    # si chiude la sessione precedente e se ne apre subito una nuova.
                    metadata = MeetingMetadata(title="Riunione Teams", source="fallback")
                    self._transition(
                        Stage.RECORDING,
                        "Nuova call rilevata: chiusura della precedente",
                    )
                    actions.append(lambda: self._on_call_ended(ShutdownReason.CALL_ENDED))
                    actions.append(lambda: self._on_call_started(metadata, False))
                    return WatcherSnapshot(
                        state=Stage.RECORDING,
                        transition=WatcherTransition.CALL_STARTED,
                        reason="Nuova call rilevata: chiusura della precedente",
                    )
                self._transition(Stage.RECORDING, "Audio call ripreso, registrazione continua")
                actions.append(self._on_call_resumed)
                return WatcherSnapshot(
                    state=Stage.RECORDING,
                    transition=WatcherTransition.CALL_RESUMED,
                    reason="Audio call ripreso, registrazione continua",
                )

            if self.grace_started_at_monotonic is None:
                self.grace_started_at_monotonic = now_mono

            elapsed_grace = now_mono - self.grace_started_at_monotonic
            remaining_grace = max(0.0, self._stop_grace_period_sec - elapsed_grace)
            if elapsed_grace >= self._stop_grace_period_sec:
                self.grace_started_at_monotonic = None
                self._transition(
                    Stage.FINALIZING_AUDIO,
                    "Grace period scaduto, finalizzazione audio",
                )
                actions.append(lambda: self._on_call_ended(ShutdownReason.GRACE_EXPIRED))
                return WatcherSnapshot(
                    state=Stage.FINALIZING_AUDIO,
                    transition=WatcherTransition.CALL_ENDED,
                    reason="Grace period scaduto, finalizzazione audio",
                )
            else:
                self._stage_reporter.set_stage(
                    Stage.GRACE_PERIOD,
                    f"Call in pausa o terminata ({int(math.ceil(remaining_grace))}s alla chiusura)",
                )
                actions.append(lambda: self._on_call_suspected_end(remaining_grace))
                return WatcherSnapshot(
                    state=Stage.GRACE_PERIOD,
                    transition=WatcherTransition.SUSPECTED_END,
                    reason=f"Call in pausa o terminata ({int(math.ceil(remaining_grace))}s alla chiusura)",
                )

        return WatcherSnapshot(state=self._stage, transition=WatcherTransition.NONE)

    def _safe_split_check(self) -> bool:
        assert self._split_checker is not None
        try:
            return bool(self._split_checker())
        except Exception as exc:
            logger.debug("Verifica cambio meeting durante il grace non riuscita: %s", exc)
            return False

    def run(self) -> None:
        """Esegue il loop di polling del watcher fino all'attivazione di shutdown_event."""
        logger.info("Avvio loop TeamsCallWatcher.")
        while not self._shutdown_event.is_set():
            try:
                self.step()
            except Exception as exc:
                logger.error("Errore nel ciclo TeamsCallWatcher: %s", exc, exc_info=True)
            if self._shutdown_event.wait(timeout=self._poll_interval_sec):
                break
        logger.info("Loop TeamsCallWatcher terminato.")


# Alias retrocompatibile
TeamsAudioWatcher = TeamsCallWatcher

