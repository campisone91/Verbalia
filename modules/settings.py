from __future__ import annotations

import json
import os
from dataclasses import dataclass, field, fields, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any

import config as _config
from modules.contracts import atomic_write_json
from modules.logging_setup import get_logger

if TYPE_CHECKING:
    from modules.summarizer import SummarizerOptions

logger = get_logger("settings")

SETTINGS_PATH_ENV_VAR: str = "TEAMS_SCRIBE_SETTINGS_PATH"
SETTINGS_SCHEMA_VERSION: int = 1

WHISPER_MODEL_CHOICES: tuple[str, ...] = ("medium", "large-v3-turbo", "small", "large-v3")
DETAIL_LEVEL_CHOICES: tuple[str, ...] = ("standard", "dettagliato", "massimo")
PARALLEL_POLICY_CHOICES: tuple[str, ...] = ("auto", "sequential", "parallel")


def _spec(
    label: str,
    *,
    minimum: float | None = None,
    maximum: float | None = None,
    min_value: float | None = None,
    max_value: float | None = None,
    choices: tuple[str, ...] | None = None,
    restart: bool = False,
    help_text: str = "",
    ui_widget: str | None = None,
) -> dict[str, Any]:
    """Metadati di un campo: usati sia per la validazione sia per costruire il form del cruscotto."""
    eff_min = minimum if minimum is not None else min_value
    eff_max = maximum if maximum is not None else max_value
    data: dict[str, Any] = {
        "label": label,
        "min": eff_min,
        "max": eff_max,
        "choices": choices,
        "restart": restart,
        "help": help_text,
    }
    if ui_widget:
        data["ui_widget"] = ui_widget
    return data


_meta = _spec


@dataclass(frozen=True, slots=True)
class DetectionSettings:
    start_debounce_sec: int = field(
        default=20,
        metadata=_spec(
            "Debounce avvio (s)",
            minimum=0,
            maximum=300,
            help_text=(
                "Secondi di chiamata continua prima di avviare la registrazione. "
                "Aumentare evita avvii errati per chiamate lampo; ridurre avvia la registrazione più tempestivamente."
            ),
        ),
    )
    stop_grace_period_sec: int = field(
        default=90,
        metadata=_spec(
            "Grace period chiusura (s)",
            minimum=0,
            maximum=1800,
            help_text=(
                "Attesa in secondi prima di chiudere la sessione a fine call. "
                "Aumentare protegge da cadute di linea o riavvii di Teams; ridurre genera subito il verbale a fine riunione."
            ),
        ),
    )
    outlook_match_tolerance_min: int = field(
        default=10,
        metadata=_spec(
            "Tolleranza abbinamento Outlook (min)",
            minimum=0,
            maximum=60,
            help_text=(
                "Finestra in minuti rispetto all'orario per associare l'evento del calendario Outlook. "
                "Aumentare trova la call anche con anticipo o ritardo; ridurre evita scambi tra meeting consecutivi."
            ),
        ),
    )
    use_mic_signal: bool = field(
        default=True,
        metadata=_spec(
            "Usa segnale microfono in uso da Teams",
            help_text=(
                "Rileva l'uso effettivo del microfono da parte di Teams tramite API Windows. "
                "Attivo evita registrazioni a vuoto (es. Teams aperto inattivo); disattivare solo se il microfono non viene rilevato."
            ),
        ),
    )
    auto_record_teams_calls: bool = field(
        default=True,
        metadata=_spec(
            "Registra automaticamente le chiamate Teams",
            help_text=(
                "Avvia e ferma la registrazione automaticamente quando rileva una chiamata Microsoft Teams attiva. "
                "Se disattivato, la registrazione può essere avviata solo manualmente dal menu della tray."
            ),
        ),
    )
    incognito_mode: bool = field(
        default=False,
        metadata=_spec(
            "Modalità in incognito (zero popup)",
            help_text=(
                "Disattiva qualsiasi popup, notifica toast di Windows o finestra di avviso in tutte le fasi "
                "(avvio, registrazione, riconoscimento riunione, completamento ed errori)."
            ),
        ),
    )
    pause_hotkey_enabled: bool = field(
        default=_config.PAUSE_HOTKEY_ENABLED_DEFAULT,
        metadata=_meta(
            "Scorciatoia globale Pausa/Ripresa (Ctrl+Alt+P)",
            help_text=(
                "Quando attiva, registra la scorciatoia globale di Windows Ctrl+Alt+P per "
                "mettere in pausa o riprendere rapidamente la registrazione live in qualsiasi momento."
            ),
            ui_widget="checkbox",
        ),
    )


@dataclass(frozen=True, slots=True)
class AudioSettings:
    chunk_duration_sec: int = field(
        default=900,
        metadata=_spec(
            "Durata chunk audio (s)",
            minimum=60,
            maximum=3600,
            help_text=(
                "Durata in secondi dei segmenti audio registrati e trascritti. "
                "Ridurre anticipa la trascrizione progressiva e snellisce la fine call; aumentare riduce il numero di file temporanei generati."
            ),
        ),
    )
    max_session_duration_sec: int = field(
        default=14400,
        metadata=_spec(
            "Durata massima registrazione (s)",
            minimum=600,
            maximum=43200,
            help_text=(
                "Limite massimo di sicurezza per una singola registrazione continua. "
                "Aumentare consente lunghe sessioni senza interruzioni; ridurre previene file enormi in caso di call dimenticate aperte."
            ),
        ),
    )


@dataclass(frozen=True, slots=True)
class WhisperSettings:
    model: str = field(
        default="medium",
        metadata=_spec(
            "Modello Whisper (CPU)",
            choices=WHISPER_MODEL_CHOICES,
            restart=True,
            help_text=(
                "Modello faster-whisper per trascrizione su CPU. "
                "'large' massimizza l'accuratezza con gergo o audio disturbato ma è più lento; 'small' o 'large-v3-turbo' velocizzano la trascrizione."
            ),
        ),
    )
    cpu_threads: int = field(
        default=6,
        metadata=_spec(
            "Thread CPU Whisper",
            minimum=1,
            maximum=32,
            restart=True,
            help_text=(
                "Thread CPU dedicati a faster-whisper. "
                "Aumentare velocizza la trascrizione su CPU multi-core; ridurre lascia il processore più libero e reattivo per le altre applicazioni in uso."
            ),
        ),
    )
    beam_size: int = field(
        default=5,
        metadata=_spec(
            "Beam size",
            minimum=1,
            maximum=10,
            help_text=(
                "Ampiezza della ricerca per fascio nella decodifica audio. "
                "Valori alti (5-10) aumentano la fedeltà su passaggi complessi ma rallentano; valori bassi (1-2) sono più rapidi."
            ),
        ),
    )
    language: str = field(
        default="it",
        metadata=_spec(
            "Lingua",
            help_text=(
                "Codice lingua principale dell'audio (es. 'it', 'en'). "
                "Fissare 'it' previene allucinazioni su silenzi e rumori; modificare solo per riunioni svolte in un'altra lingua."
            ),
        ),
    )
    diarization_enabled: bool = field(
        default=_config.DIARIZATION_ENABLED_DEFAULT,
        metadata=_meta(
            "Distingui interlocutori remoti sulla traccia [ALTRI]",
            help_text=(
                "Analizza le caratteristiche vocali della traccia [ALTRI] per distinguere più partecipanti "
                "remoti (es. [INTERLOCUTORE 1], [INTERLOCUTORE 2]) mantenendo la memoria tra i chunk da 15 minuti."
            ),
            ui_widget="checkbox",
        ),
    )
    diarization_max_speakers: int = field(
        default=_config.DIARIZATION_MAX_SPEAKERS,
        metadata=_meta(
            "Numero massimo interlocutori remoti",
            min_value=2,
            max_value=15,
            help_text=(
                "Limite massimo di interlocutori distinti identificabili sulla traccia [ALTRI] durante una "
                "singola riunione (valori consigliati: da 2 a 10)."
            ),
            ui_widget="spinbox",
        ),
    )
    diarization_similarity_threshold: float = field(
        default=_config.DIARIZATION_SIMILARITY_THRESHOLD,
        metadata=_meta(
            "Soglia similarità vocale interlocutori",
            min_value=0.45,
            max_value=0.90,
            help_text=(
                "Soglia di similarità coseno (tra 0.45 e 0.90) per associare un segmento a un interlocutore "
                "già noto anziché crearne uno nuovo. Valori più bassi riducono la frammentazione dei parlanti."
            ),
            ui_widget="spinbox",
        ),
    )


@dataclass(frozen=True, slots=True)
class OllamaSettings:
    base_url: str = field(
        default="http://localhost:11434",
        metadata=_spec(
            "URL Ollama",
            help_text=(
                "Indirizzo del server Ollama. "
                "Modificare se Ollama è in ascolto su un'altra porta o su una macchina remota con GPU dedicata; se non raggiungibile la sintesi fallisce."
            ),
        ),
    )
    model: str = field(
        default="llama3.1:8b",
        metadata=_spec(
            "Modello LLM",
            help_text=(
                "Nome del modello LLM per sintesi e verbale. "
                "Modelli più capaci offrono resoconti migliori ma richiedono più VRAM e tempo; il modello deve essere già scaricato in Ollama."
            ),
        ),
    )
    num_ctx: int = field(
        default=16384,
        metadata=_spec(
            "Contesto (num_ctx)",
            minimum=4096,
            maximum=131072,
            help_text=(
                "Dimensione della finestra di contesto in token. "
                "Aumentare consente di processare trascrizioni più estese senza tagli; ridurre diminuisce l'uso di memoria VRAM della GPU."
            ),
        ),
    )
    num_predict_map: int = field(
        default=3072,
        metadata=_spec(
            "Token massimi per blocco MAP",
            minimum=256,
            maximum=16384,
            help_text=(
                "Token massimi per ogni sintesi parziale (fase MAP). "
                "Aumentare permette note intermedie più dettagliate e ricche per ogni segmento; ridurre velocizza i passaggi parziali."
            ),
        ),
    )
    num_predict_reduce: int = field(
        default=4096,
        metadata=_spec(
            "Token massimi REDUCE",
            minimum=256,
            maximum=16384,
            help_text=(
                "Token massimi per il verbale finale e l'email (fase REDUCE). "
                "Aumentare evita troncamenti in riunioni fitte di decisioni e compiti; ridurre velocizza la stesura e accorcia il testo."
            ),
        ),
    )
    temperature: float = field(
        default=0.2,
        metadata=_spec(
            "Temperature",
            minimum=0.0,
            maximum=2.0,
            help_text=(
                "Creatività del modello (0.0 - 2.0). "
                "Valori bassi (0.0-0.2) garantiscono fedeltà e risposte conformi al trascritto; valori alti aumentano la varietà ma rischiano invenzioni."
            ),
        ),
    )
    read_timeout_sec: int = field(
        default=300,
        metadata=_spec(
            "Timeout tra due chunk di risposta (s)",
            minimum=30,
            maximum=3600,
            help_text=(
                "Secondi massimi di attesa tra frammenti generati da Ollama. "
                "Aumentare previene timeout su GPU/CPU lente o prompt molto lunghi; ridurre rileva prima blocchi del server."
            ),
        ),
    )
    keep_alive: str = field(
        default="10m",
        metadata=_spec(
            "Keep alive modello",
            help_text=(
                "Permanenza del modello in memoria GPU dopo l'uso (es. '10m', '1h', '0' per scaricarlo subito). "
                "Valori alti velocizzano le call successive; '0' libera subito la VRAM."
            ),
        ),
    )
    map_block_duration_sec: int = field(
        default=600,
        metadata=_spec(
            "Durata blocco MAP (s)",
            minimum=120,
            maximum=3600,
            help_text=(
                "Durata in secondi delle porzioni di trascrizione riassunte singolarmente. "
                "Ridurre aumenta il dettaglio cronologico moltiplicando i passaggi; aumentare accelera la sintesi complessiva."
            ),
        ),
    )
    detail_level: str = field(
        default="dettagliato",
        metadata=_spec(
            "Livello di dettaglio del resoconto",
            choices=DETAIL_LEVEL_CHOICES,
            help_text=(
                "Profondità del verbale. "
                "'standard' per recap sintetico e decisioni chiave; 'dettagliato' per discussioni e task operativi; 'massimo' per verbalizzazione completa e analitica."
            ),
        ),
    )


@dataclass(frozen=True, slots=True)
class OutputSettings:
    output_dir: str = field(
        default="",
        metadata=_spec(
            "Cartella output (vuoto = predefinita)",
            restart=True,
            help_text=(
                "Cartella in cui salvare i documenti Word (.docx). "
                "Se vuoto usa la cartella 'output' del programma; impostare per salvare direttamente su OneDrive o cartelle condivise."
            ),
        ),
    )
    organize_by_day: bool = field(
        default=True,
        metadata=_spec(
            "Organizza per giorno",
            help_text=(
                "Raggruppa i documenti in sottocartelle con la data del giorno (YYYY-MM-DD). "
                "Attivo mantiene l'archivio ordinato; disattivato salva tutti i file nella radice di output."
            ),
        ),
    )
    include_transcript: bool = field(
        default=True,
        metadata=_spec(
            "Trascrizione integrale in appendice",
            help_text=(
                "Include la trascrizione integrale con timestamp in appendice al documento Word. "
                "Attivo permette verifiche puntuali; disattivato produce un verbale più compatto e corto."
            ),
        ),
    )
    generate_recap_email: bool = field(
        default=True,
        metadata=_spec(
            "Genera bozza email di recap",
            help_text=(
                "Genera la bozza email di recap pronta per Outlook. "
                "Disattivare se si desidera solo il verbale dettagliato, velocizzando l'elaborazione LLM e risparmiando token."
            ),
        ),
    )
    user_display_name: str = field(
        default="",
        metadata=_spec(
            "Nome utente per la traccia IO",
            help_text=(
                "Nome da associare al proprio microfono. "
                "Sostituisce l'etichetta generica '[IO]' con il proprio nome reale nel verbale Word e nella trascrizione; vuoto = default."
            ),
        ),
    )
    glossary: str = field(
        default="",
        metadata=_spec(
            "Glossario (termini separati da virgola o uno per riga)",
            help_text=(
                "Termini tecnici, acronimi e nomi propri (separati da virgola o a capo). "
                "Guida Whisper a trascriverli senza errori fonetici e istruisce l'LLM a usarli correttamente."
            ),
        ),
    )


@dataclass(frozen=True, slots=True)
class ExecutionSettings:
    parallel_policy: str = field(
        default="auto",
        metadata=_spec(
            "Esecuzione Whisper/LLM",
            choices=PARALLEL_POLICY_CHOICES,
            restart=True,
            help_text=(
                "Politica di concorrenza Whisper/LLM. "
                "'auto' esegue in parallelo solo se l'LLM è su GPU; 'sequential' elabora uno alla volta per non saturare la CPU; 'parallel' forza l'elaborazione simultanea."
            ),
        ),
    )


@dataclass(frozen=True, slots=True)
class AppSettings:
    detection: DetectionSettings = field(default_factory=DetectionSettings)
    audio: AudioSettings = field(default_factory=AudioSettings)
    whisper: WhisperSettings = field(default_factory=WhisperSettings)
    ollama: OllamaSettings = field(default_factory=OllamaSettings)
    output: OutputSettings = field(default_factory=OutputSettings)
    execution: ExecutionSettings = field(default_factory=ExecutionSettings)


SETTINGS_GROUP_LABELS: dict[str, str] = {
    "detection": "Rilevamento",
    "audio": "Audio",
    "whisper": "Whisper",
    "ollama": "Ollama",
    "output": "Output",
    "execution": "Esecuzione",
}


def resolve_settings_path(path: Path | None = None) -> Path:
    """Percorso di settings.json: argomento, variabile d'ambiente o default di config."""
    if path is not None:
        return path
    env_path = os.environ.get(SETTINGS_PATH_ENV_VAR, "").strip()
    if env_path:
        return Path(env_path)
    return _config.SETTINGS_PATH


def default_settings() -> AppSettings:
    """Default letti da config al momento della chiamata (non all'import)."""
    return AppSettings(
        detection=DetectionSettings(
            start_debounce_sec=int(_config.START_DEBOUNCE_SEC),
            stop_grace_period_sec=int(_config.STOP_GRACE_PERIOD_SEC),
            outlook_match_tolerance_min=int(_config.OUTLOOK_MATCH_TOLERANCE_MIN),
            use_mic_signal=bool(_config.WATCHER_USE_MIC_SIGNAL),
            auto_record_teams_calls=True,
            incognito_mode=bool(getattr(_config, "INCOGNITO_MODE", False)),
            pause_hotkey_enabled=bool(getattr(_config, "PAUSE_HOTKEY_ENABLED_DEFAULT", True)),
        ),
        audio=AudioSettings(
            chunk_duration_sec=int(_config.CHUNK_DURATION_SEC),
            max_session_duration_sec=int(_config.MAX_SESSION_DURATION_SEC),
        ),
        whisper=WhisperSettings(
            model=str(_config.WHISPER_CPU_MODEL),
            cpu_threads=int(_config.WHISPER_CPU_THREADS),
            beam_size=int(_config.WHISPER_BEAM_SIZE),
            language=str(_config.WHISPER_LANGUAGE),
            diarization_enabled=bool(getattr(_config, "DIARIZATION_ENABLED_DEFAULT", True)),
            diarization_max_speakers=int(getattr(_config, "DIARIZATION_MAX_SPEAKERS", 8)),
            diarization_similarity_threshold=float(getattr(_config, "DIARIZATION_SIMILARITY_THRESHOLD", 0.68)),
        ),
        ollama=OllamaSettings(
            base_url=str(_config.OLLAMA_BASE_URL),
            model=str(_config.OLLAMA_MODEL),
            num_ctx=int(_config.OLLAMA_NUM_CTX),
            num_predict_map=int(_config.OLLAMA_NUM_PREDICT_MAP),
            num_predict_reduce=int(_config.OLLAMA_NUM_PREDICT_REDUCE),
            temperature=float(_config.OLLAMA_TEMPERATURE),
            read_timeout_sec=int(_config.OLLAMA_READ_TIMEOUT_SEC),
            keep_alive=str(_config.OLLAMA_KEEP_ALIVE),
            map_block_duration_sec=int(_config.MAP_BLOCK_DURATION_SEC),
            detail_level=str(_config.OLLAMA_DETAIL_LEVEL),
        ),
        output=OutputSettings(
            output_dir="",
            organize_by_day=bool(_config.OUTPUT_ORGANIZE_BY_DAY),
            include_transcript=bool(_config.DOCX_INCLUDE_TRANSCRIPT),
            generate_recap_email=True,
            user_display_name=str(_config.USER_DISPLAY_NAME),
            glossary=str(_config.CUSTOM_GLOSSARY),
        ),
        execution=ExecutionSettings(parallel_policy=str(_config.AI_PARALLEL_POLICY)),
    )


def settings_to_dict(settings: AppSettings) -> dict[str, object]:
    """Serializza le impostazioni in un dizionario JSON-compatibile a gruppi."""
    payload: dict[str, object] = {"schema_version": SETTINGS_SCHEMA_VERSION}
    for group_field in fields(settings):
        group = getattr(settings, group_field.name)
        payload[group_field.name] = {f.name: getattr(group, f.name) for f in fields(group)}
    return payload


def _coerce_value(raw: object, default: object, spec: dict[str, Any]) -> object:
    """Converte e valida un singolo valore; solleva ValueError se non accettabile."""
    if isinstance(default, bool):
        if isinstance(raw, bool):
            return raw
        if isinstance(raw, str) and raw.strip().lower() in ("true", "1", "si", "sì", "yes"):
            return True
        if isinstance(raw, str) and raw.strip().lower() in ("false", "0", "no"):
            return False
        raise ValueError("atteso un valore booleano")
    if isinstance(default, int):
        if isinstance(raw, bool):
            raise ValueError("atteso un intero")
        value_int = int(float(str(raw).strip())) if isinstance(raw, str) else int(raw)  # type: ignore[call-overload]
        if spec.get("min") is not None and value_int < spec["min"]:
            raise ValueError(f"minimo {spec['min']}")
        if spec.get("max") is not None and value_int > spec["max"]:
            raise ValueError(f"massimo {spec['max']}")
        return value_int
    if isinstance(default, float):
        if isinstance(raw, bool):
            raise ValueError("atteso un numero")
        value_float = float(str(raw).strip().replace(",", ".")) if isinstance(raw, str) else float(raw)  # type: ignore[arg-type]
        if spec.get("min") is not None and value_float < spec["min"]:
            raise ValueError(f"minimo {spec['min']}")
        if spec.get("max") is not None and value_float > spec["max"]:
            raise ValueError(f"massimo {spec['max']}")
        return value_float
    if not isinstance(raw, str):
        raise ValueError("attesa una stringa")
    value_str = raw if spec.get("label", "").startswith("Glossario") else raw.strip()
    choices = spec.get("choices")
    if choices and value_str not in choices:
        raise ValueError(f"valori ammessi: {', '.join(choices)}")
    return value_str


def settings_from_dict(
    payload: dict[str, object],
    base: AppSettings | None = None,
) -> tuple[AppSettings, list[str]]:
    """Applica un dizionario alle impostazioni base; i valori non validi restano al default.

    Restituisce le impostazioni risultanti e l'elenco dei problemi riscontrati (in italiano).
    """
    result = base if base is not None else default_settings()
    problems: list[str] = []
    for group_field in fields(result):
        group_name = group_field.name
        raw_group = payload.get(group_name)
        if raw_group is None:
            continue
        if not isinstance(raw_group, dict):
            problems.append(f"Gruppo '{group_name}' non valido: ignorato.")
            continue
        group = getattr(result, group_name)
        updates: dict[str, object] = {}
        for item in fields(group):
            if item.name not in raw_group:
                continue
            default_value = getattr(group, item.name)
            try:
                updates[item.name] = _coerce_value(raw_group[item.name], default_value, dict(item.metadata))
            except (TypeError, ValueError) as exc:
                label = item.metadata.get("label", item.name)
                problems.append(f"{SETTINGS_GROUP_LABELS.get(group_name, group_name)} / {label}: {exc}.")
        if updates:
            result = replace(result, **{group_name: replace(group, **updates)})
    return result, problems


def load_settings(path: Path | None = None) -> AppSettings:
    """Carica settings.json sopra i default; file assente o corrotto = default (con warning)."""
    target = resolve_settings_path(path)
    defaults = default_settings()
    if not target.is_file():
        return defaults
    try:
        # utf-8-sig: il Blocco note di Windows salva con BOM
        payload = json.loads(target.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        logger.warning("settings.json illeggibile (%s): uso le impostazioni predefinite.", exc)
        return defaults
    if not isinstance(payload, dict):
        logger.warning("settings.json non contiene un oggetto JSON: uso le impostazioni predefinite.")
        return defaults
    settings, problems = settings_from_dict(payload, defaults)
    for problem in problems:
        logger.warning("Impostazione ignorata: %s", problem)
    return settings


def save_settings(settings: AppSettings, path: Path | None = None) -> Path:
    """Salva atomicamente le impostazioni in settings.json."""
    target = resolve_settings_path(path)
    atomic_write_json(target, settings_to_dict(settings))
    return target


def restart_required_fields(old: AppSettings, new: AppSettings) -> list[str]:
    """Etichette dei campi modificati che richiedono il riavvio dell'applicazione."""
    changed: list[str] = []
    for group_field in fields(old):
        old_group = getattr(old, group_field.name)
        new_group = getattr(new, group_field.name)
        for item in fields(old_group):
            if item.metadata.get("restart") and getattr(old_group, item.name) != getattr(new_group, item.name):
                changed.append(str(item.metadata.get("label", item.name)))
    return changed


def resolve_output_dir(settings: AppSettings) -> Path:
    """Cartella di output effettiva (impostazione utente o default di config)."""
    raw = settings.output.output_dir.strip()
    return Path(raw).expanduser() if raw else _config.OUTPUT_DIR


def to_summarizer_options(settings: AppSettings) -> SummarizerOptions:
    """Converte le impostazioni nelle SummarizerOptions usate da un job."""
    from modules.summarizer import SummarizerOptions, resolve_user_display_name
    from modules.transcript_utils import resolve_glossary

    return SummarizerOptions(
        num_ctx=settings.ollama.num_ctx,
        num_predict_map=settings.ollama.num_predict_map,
        num_predict_reduce=settings.ollama.num_predict_reduce,
        temperature=settings.ollama.temperature,
        seed=int(_config.OLLAMA_SEED),
        keep_alive=settings.ollama.keep_alive,
        connect_timeout_sec=int(_config.OLLAMA_CONNECT_TIMEOUT_SEC),
        read_timeout_sec=settings.ollama.read_timeout_sec,
        max_retries=int(_config.OLLAMA_MAX_RETRIES),
        context_safety_margin=int(_config.OLLAMA_CONTEXT_SAFETY_MARGIN),
        map_block_duration_sec=settings.ollama.map_block_duration_sec,
        detail_level=settings.ollama.detail_level,
        glossary=resolve_glossary(settings.output.glossary),
        user_name=resolve_user_display_name(settings.output.user_display_name),
        streaming=bool(_config.OLLAMA_STREAMING),
    )


def apply_to_config(settings: AppSettings) -> None:
    """Propaga le impostazioni correnti nel modulo config."""
    _config.START_DEBOUNCE_SEC = int(settings.detection.start_debounce_sec)
    _config.STOP_GRACE_PERIOD_SEC = int(settings.detection.stop_grace_period_sec)
    if hasattr(settings.detection, "confidence_threshold"):
        _config.TEAMS_CONFIDENCE_THRESHOLD = float(getattr(settings.detection, "confidence_threshold"))
    _config.OUTLOOK_MATCH_TOLERANCE_MIN = int(settings.detection.outlook_match_tolerance_min)
    _config.WATCHER_USE_MIC_SIGNAL = bool(settings.detection.use_mic_signal)
    _config.INCOGNITO_MODE = bool(getattr(settings.detection, "incognito_mode", False))
    _config.PAUSE_HOTKEY_ENABLED_DEFAULT = bool(getattr(settings.detection, "pause_hotkey_enabled", True))
    _config.DIARIZATION_ENABLED_DEFAULT = bool(getattr(settings.whisper, "diarization_enabled", True))
    _config.DIARIZATION_MAX_SPEAKERS = int(getattr(settings.whisper, "diarization_max_speakers", 8))
    _config.DIARIZATION_SIMILARITY_THRESHOLD = float(getattr(settings.whisper, "diarization_similarity_threshold", 0.68))


def apply_settings(path: Path | None = None) -> AppSettings:
    """Carica settings.json, propaga a config e restituisce le impostazioni."""
    settings = load_settings(path)
    apply_to_config(settings)
    return settings
