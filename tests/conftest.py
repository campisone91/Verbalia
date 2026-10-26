from __future__ import annotations

import importlib.util
import os
import sys
import types
from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

os.environ["TEAMS_SCRIBE_SKIP_BOOTSTRAP"] = "1"
os.environ["TEAMS_SCRIBE_HEADLESS_TRAY"] = "1"
os.environ["TEAMS_SCRIBE_AUTO_ACCEPT_CONSENT"] = "1"

if importlib.util.find_spec("psutil") is None:
    _fake_psutil = types.ModuleType("psutil")

    class _PsutilError(Exception):
        pass

    class _NoSuchProcess(_PsutilError):
        pass

    class _AccessDenied(_PsutilError):
        pass

    class _ZombieProcess(_PsutilError):
        pass

    class _FakeProcess:
        def __init__(self, pid: int | None = None) -> None:
            self.pid = pid

        def nice(self, value: int | None = None) -> int:
            return int(value or 0)

    _fake_psutil.Error = _PsutilError  # type: ignore[attr-defined]
    _fake_psutil.NoSuchProcess = _NoSuchProcess  # type: ignore[attr-defined]
    _fake_psutil.AccessDenied = _AccessDenied  # type: ignore[attr-defined]
    _fake_psutil.ZombieProcess = _ZombieProcess  # type: ignore[attr-defined]
    _fake_psutil.BELOW_NORMAL_PRIORITY_CLASS = 0x00004000  # type: ignore[attr-defined]
    _fake_psutil.Process = _FakeProcess  # type: ignore[attr-defined]
    _fake_psutil.process_iter = lambda _attrs=None: iter(())  # type: ignore[attr-defined]
    sys.modules["psutil"] = _fake_psutil

from config import STATE_SCHEMA_VERSION  # noqa: E402
from modules.audio_utils import create_silent_wav  # noqa: E402
from modules.contracts import (  # noqa: E402
    DeviceDescriptor,
    MeetingMetadata,
    SessionContext,
    SessionMode,
    Stage,
)
from modules.logging_setup import (  # noqa: E402
    LOG_DIR_ENV_VAR,
    close_logging_handlers,
    configure_logging,
    set_log_session_id,
)


@pytest.fixture(autouse=True)
def _isolated_logging(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """Dirotta app.log in tmp_path: i test non devono mai scrivere nel log di produzione."""
    log_dir = tmp_path / "logs"
    monkeypatch.setenv(LOG_DIR_ENV_VAR, str(log_dir))
    configure_logging()
    yield log_dir
    close_logging_handlers()
    set_log_session_id(None)


@pytest.fixture(autouse=True)
def _isolated_app_data(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Storico SQLite e settings.json dei test in tmp_path, mai nei file dell'utente."""
    monkeypatch.setenv("TEAMS_SCRIBE_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("TEAMS_SCRIBE_SETTINGS_PATH", str(tmp_path / "settings.json"))


@pytest.fixture(autouse=True)
def _no_real_system_probes(monkeypatch: pytest.MonkeyPatch) -> None:
    """Evita che i test leggano lo stato reale del microfono Teams dal registro di Windows."""
    monkeypatch.setattr("modules.watcher.is_teams_microphone_in_use", lambda *_a, **_kw: False)
    # Nessuna interrogazione del server Ollama reale dal processore dei job
    monkeypatch.setattr(
        "modules.orchestrator.probe_ollama_gpu",
        lambda *_a, **_kw: {"reachable": False, "loaded": False, "gpu_percent": None, "models": [], "hint": ""},
    )


@pytest.fixture
def fixed_now() -> datetime:
    """Restituisce un timestamp fisso timezone-aware per test deterministici."""
    return datetime(2026, 10, 23, 14, 0, 0, tzinfo=timezone(timedelta(hours=2)))


@pytest.fixture
def sample_metadata(fixed_now: datetime) -> MeetingMetadata:
    """Restituisce un'istanza MeetingMetadata sintetica di test."""
    return MeetingMetadata(
        title="Revisione Architettura Sprint 42",
        organizer="Mario Rossi",
        required_attendees=["Giulia Bianchi", "Luca Verdi"],
        optional_attendees=["Elena Neri"],
        agenda="1. Stato avanzamento API\n2. Piano di rilascio Q4\n3. Action Items",
        scheduled_start=fixed_now,
        scheduled_end=fixed_now + timedelta(hours=1),
        source="outlook",
    )


@pytest.fixture
def sample_session_context(
    tmp_path: Path,
    fixed_now: datetime,
    sample_metadata: MeetingMetadata,
) -> SessionContext:
    """Restituisce un SessionContext valido associato a una directory temporanea in tmp_path."""
    session_id = "20261023_140000_a1b2c3d4"
    session_dir = (tmp_path / "temp" / f"session_{session_id}").resolve()
    session_dir.mkdir(parents=True, exist_ok=True)
    return SessionContext(
        schema_version=STATE_SCHEMA_VERSION,
        session_id=session_id,
        session_dir=session_dir,
        mode=SessionMode.AUTO,
        metadata=sample_metadata,
        started_at=fixed_now,
        ended_at=fixed_now + timedelta(minutes=35),
        stage=Stage.IDLE,
        last_updated_at=fixed_now,
    )


@pytest.fixture
def fake_devices() -> tuple[DeviceDescriptor, DeviceDescriptor]:
    """Restituisce una coppia sintetica di DeviceDescriptor (microfono + WASAPI loopback)."""
    mic = DeviceDescriptor(
        index=1,
        name="Synthetic USB Microphone",
        host_api="Windows WASAPI",
        sample_rate=16000,
        channels=1,
        sample_width=2,
        is_loopback=False,
    )
    loopback = DeviceDescriptor(
        index=2,
        name="Synthetic Speakers [Loopback]",
        host_api="Windows WASAPI",
        sample_rate=16000,
        channels=1,
        sample_width=2,
        is_loopback=True,
    )
    return mic, loopback


@pytest.fixture
def synthetic_wav_16k(tmp_path: Path) -> Path:
    """Crea un file WAV 16 kHz mono 16-bit PCM sintetico di 1 secondo."""
    wav_path = tmp_path / "synthetic_16k.wav"
    return create_silent_wav(wav_path, duration_sec=1.0, sample_rate=16000, channels=1)


@pytest.fixture
def valid_map_markdown() -> str:
    """Restituisce un testo Markdown valido per la fase MAP."""
    return (
        "## Resoconto dettagliato\n"
        "### Modulo di autenticazione\n"
        "Mario apre la discussione sui requisiti del modulo di autenticazione. "
        "Gli interlocutori confermano l'endpoint `/api/v2/token` con scadenza a 900 secondi "
        "e valutano la firma RS256 rispetto a HS256, citando la latenza di handshake.\n\n"
        "## Decisioni\n- Adottare firma RS256 per tutti i token interni.\n\n"
        "## Action items\n- Responsabile: Da assegnare | Azione: Preparare benchmark di carico | "
        "Priorità: Alta | Scadenza: venerdì\n\n"
        "## Questioni aperte\n- Definire la politica di rotazione delle chiavi.\n\n"
        "## Rischi\n- Latenza aggiuntiva in fase di handshake.\n\n"
        "## Rolling Context\nConfermata adozione RS256 su `/api/v2/token`; resta aperto il benchmark.\n"
    )


@pytest.fixture
def valid_detailed_minutes_markdown() -> str:
    """Restituisce un testo Markdown valido per il verbale dettagliato REDUCE."""
    return (
        "## 1. Executive Summary\n"
        "Riunione tecnica dedicata alla revisione dell'architettura Sprint 42.\n\n"
        "## 2. Dati della Riunione\n"
        "- Titolo: Revisione Architettura Sprint 42\n"
        "- Organizzatore: Mario Rossi\n\n"
        "## 3. Discussione per Argomento\n"
        "### Modulo Autenticazione\n"
        "- Analisi delle prestazioni dell'endpoint `/api/v2/token`.\n"
        "- Valutazione impatto crittografico RS256.\n\n"
        "## 4. Decisioni Confermate\n"
        "1. Adottare RS256 per i token emessi dal servizio centrale.\n\n"
        "## 5. Questioni Aperte\n"
        "- Pianificazione della finestra di manutenzione.\n\n"
        "## 6. Rischi\n"
        "- Possibile aumento utilizzo CPU sui nodi gateway.\n\n"
        "## 7. Dipendenze\n"
        "- Aggiornamento libreria client sui microservizi consumer.\n\n"
        "## 8. Action Items\n"
        "| Responsabile | Task / Azione | Priorità | Scadenza / Note |\n"
        "| --- | --- | --- | --- |\n"
        "| Da assegnare | Eseguire test di carico su /api/v2/token | Alta | Venerdì |\n"
    )


@pytest.fixture
def valid_recap_email_markdown() -> str:
    """Restituisce un testo Markdown valido per la bozza mail di recap REDUCE."""
    return (
        "## Oggetto\nRecap: Revisione Architettura Sprint 42\n\n"
        "## Introduzione\nBuongiorno a tutti, condivido di seguito la sintesi della riunione odierna.\n\n"
        "## TL;DR\nÈ stata confermata l'adozione di RS256 per `/api/v2/token` e pianificato il test di carico.\n\n"
        "## Decisioni Principali\n- Utilizzo di RS256 con durata token 900 secondi.\n\n"
        "## Next Steps\n- Da assegnare: Eseguire test di carico entro venerdì.\n\n"
        "## Chiusura\nResto a disposizione per eventuali integrazioni.\nCordiali saluti.\n"
    )
