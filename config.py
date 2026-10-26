from __future__ import annotations

import math
import subprocess
from pathlib import Path

# 8.1 Metadati
APP_NAME: str = "Verbalia"
APP_VERSION: str = "0.1.0"
STATE_SCHEMA_VERSION: int = 1
MIN_PYTHON_VERSION: tuple[int, int] = (3, 10)

# Exit codes (Sezione 29.1)
EXIT_SUCCESS: int = 0
EXIT_APP_ERROR: int = 1
EXIT_INVALID_ARGS: int = 2
EXIT_BOOTSTRAP_FAILED: int = 3
EXIT_MISSING_DEPENDENCY: int = 4
EXIT_UNRECOVERABLE_SESSION: int = 5
EXIT_CLEANUP_FAILED: int = 6

# 8.2 Path
PROJECT_ROOT: Path = Path(__file__).resolve().parent
PROMPTS_DIR: Path = PROJECT_ROOT / "prompts"
ASSETS_DIR: Path = PROJECT_ROOT / "assets"
APP_ICON_PATH: Path = ASSETS_DIR / "icon.ico"
APP_LOGO_PATH: Path = ASSETS_DIR / "logo.png"
BIN_DIR: Path = PROJECT_ROOT / "bin"
LOG_DIR: Path = PROJECT_ROOT / "logs"
TEMP_DIR: Path = PROJECT_ROOT / "temp"
OUTPUT_DIR: Path = PROJECT_ROOT / "output"
DATA_DIR: Path = PROJECT_ROOT / "data"
HISTORY_DB_PATH: Path = DATA_DIR / "history.db"
SETTINGS_PATH: Path = PROJECT_ROOT / "settings.json"

ENV_MARKER_PATH: Path = PROJECT_ROOT / ".env_ready.json"
USER_CONSENT_PATH: Path = PROJECT_ROOT / ".user_consent.json"
FFMPEG_LOCAL_PATH: Path = BIN_DIR / "ffmpeg.exe"

FFMPEG_DOWNLOAD_URL: str = (
    "https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/"
    "ffmpeg-master-latest-win64-gpl.zip"
)
FFMPEG_DOWNLOAD_SHA256: str = ""
FFMPEG_DOWNLOAD_TIMEOUT_SEC: int = 120


def ensure_runtime_directories() -> None:
    """Crea le directory runtime necessarie se non esistono."""
    for directory in (
        ASSETS_DIR,
        BIN_DIR,
        LOG_DIR,
        TEMP_DIR,
        OUTPUT_DIR,
        DATA_DIR,
    ):
        directory.mkdir(parents=True, exist_ok=True)


def windows_creation_flags() -> int:
    """Restituisce il flag CREATE_NO_WINDOW su Windows per subprocess."""
    return int(getattr(subprocess, "CREATE_NO_WINDOW", 0))


# 8.3 Audio
CHUNK_DURATION_SEC: int = 900
CHUNK_OVERLAP_SEC: int = 3
MAX_SESSION_DURATION_SEC: int = 14400

AUDIO_BLOCK_MS: int = 20
LOOPBACK_GAP_TOLERANCE_MS: int = 60
MAX_SILENCE_INJECTION_MS: int = 2000
MAX_TRACK_DRIFT_MS: int = 100

TARGET_SAMPLE_RATE: int = 16000
TARGET_CHANNELS: int = 1
TARGET_SAMPLE_WIDTH_BYTES: int = 2

AUDIO_QUEUE_SECONDS: int = 10
AUDIO_FINALIZATION_WORKERS: int = 1

DEVICE_RECONNECT_DELAY_SEC: int = 2
DEVICE_RECONNECT_MAX_ATTEMPTS: int = 30

# 8.4 Watcher
START_DEBOUNCE_SEC: int = 20
STOP_GRACE_PERIOD_SEC: int = 90
WATCHER_POLL_INTERVAL_SEC: float = 1.0
TEAMS_CONFIDENCE_THRESHOLD: float = 0.70
# Considera la call attiva anche quando Teams tiene aperto il microfono (registry CapabilityAccessManager)
WATCHER_USE_MIC_SIGNAL: bool = True
TEAMS_MIC_CONSENT_KEYS: tuple[str, ...] = ("MSTeams_8wekyb3d8bbwe",)

TEAMS_PROCESS_NAMES: set[str] = {
    "ms-teams.exe",
    "ms-teams_modulehost.exe",
    "teams.exe",
}

TEAMS_SIGNAL_WEIGHTS: dict[str, float] = {
    "process_present": 0.15,
    "audio_active": 0.55,
    "debounce_completed": 0.15,
    "outlook_event": 0.10,
    "window_title": 0.05,
}


def validate_signal_weights(
    weights: dict[str, float] | None = None,
) -> None:
    """Verifica che la somma dei pesi del watcher sia pari a 1.0."""
    target = weights if weights is not None else TEAMS_SIGNAL_WEIGHTS
    total = sum(target.values())
    if not math.isclose(total, 1.0, rel_tol=1e-6, abs_tol=1e-6):
        raise ValueError(
            f"La somma di TEAMS_SIGNAL_WEIGHTS deve essere 1.0, trovato {total:.6f}"
        )


validate_signal_weights()

# 8.5 Outlook
OUTLOOK_CALENDAR_FOLDER_ID: int = 9
OUTLOOK_LOOKBACK_MINUTES: int = 30
OUTLOOK_LOOKAHEAD_MINUTES: int = 30
# Tolleranza per l'abbinamento: un meeting futuro è accettato solo se inizia entro N minuti
OUTLOOK_MATCH_TOLERANCE_MIN: int = 10
OUTLOOK_AGENDA_MAX_CHARS: int = 8000
OUTLOOK_METADATA_TIMEOUT_SEC: float = 30.0
OUTLOOK_INCLUDE_ALL_DAY_EVENTS: bool = False

# 8.6 Whisper
WHISPER_DEVICE: str = "auto"

WHISPER_GPU_MODEL: str = "large-v3-turbo"
WHISPER_GPU_COMPUTE_TYPE: str = "float16"

WHISPER_CPU_MODEL: str = "medium"
WHISPER_CPU_COMPUTE_TYPE: str = "int8"

WHISPER_LANGUAGE: str = "it"
# Il Core Ultra 7 265H ha 16 core: 6 thread lasciano margine a cattura audio e LLM
WHISPER_CPU_THREADS: int = 6
WHISPER_BEAM_SIZE: int = 5

WHISPER_MIN_SILENCE_DURATION_MS: int = 500
WHISPER_SPEECH_PAD_MS: int = 200
WHISPER_CONDITION_ON_PREVIOUS_TEXT: bool = False
WHISPER_WORD_TIMESTAMPS: bool = False
WHISPER_ENGINE_IDLE_UNLOAD_SEC: int = 600

# 8.7 Deduplicazione
TRANSCRIPT_DEDUP_SIMILARITY: float = 0.88
TRANSCRIPT_DEDUP_TIME_TOLERANCE_SEC: int = 5
MAP_BLOCK_DURATION_SEC: int = 600

# 8.8 Ollama
OLLAMA_BASE_URL: str = "http://localhost:11434"
OLLAMA_MODEL: str = "llama3.1:8b"

OLLAMA_NUM_CTX: int = 16384
OLLAMA_TEMPERATURE: float = 0.2
OLLAMA_SEED: int = 42
OLLAMA_NUM_PREDICT: int = 4096
OLLAMA_NUM_PREDICT_MAP: int = 3072
OLLAMA_NUM_PREDICT_REDUCE: int = 4096
OLLAMA_CONTEXT_SAFETY_MARGIN: int = 2048
# Livello di dettaglio del resoconto: "standard" | "dettagliato" | "massimo"
OLLAMA_DETAIL_LEVEL: str = "dettagliato"

OLLAMA_CONNECT_TIMEOUT_SEC: int = 10
# Con lo streaming è il tempo massimo di silenzio tra due chunk della risposta
OLLAMA_READ_TIMEOUT_SEC: int = 300
OLLAMA_MAX_RETRIES: int = 3
OLLAMA_RETRY_BASE_DELAY_SEC: int = 2
OLLAMA_KEEP_ALIVE: str = "10m"
OLLAMA_UNLOAD_AFTER_PIPELINE: bool = True

OLLAMA_STREAMING: bool = True

# 8.8b Esecuzione: "auto" (parallelo Whisper/LLM se il modello è su GPU) | "sequential" | "parallel"
AI_PARALLEL_POLICY: str = "auto"
PROCESSING_MAX_ATTEMPTS: int = 3
PROCESSING_RETRY_BASE_DELAY_SEC: int = 60
HISTORY_RETENTION_DAYS: int = 365
UI_COMMAND_POLL_INTERVAL_SEC: float = 1.0

# 8.9 DOCX
DOCX_MIN_SIZE_BYTES: int = 4096
DOCX_TITLE_MAX_LENGTH: int = 80
DOCX_FONT_NAME: str = "Calibri"
DOCX_FONT_SIZE_PT: int = 11
DOCX_MARGIN_CM: float = 2.0
DOCX_TRANSCRIPT_FONT_SIZE_PT: float = 8.5
DOCX_INCLUDE_TRANSCRIPT: bool = True
OUTPUT_ORGANIZE_BY_DAY: bool = True
# Nome mostrato per la traccia [IO]; vuoto = "Io"
USER_DISPLAY_NAME: str = ""
OTHERS_DISPLAY_NAME: str = "Interlocutori"

# 8.10 Logging
LOG_LEVEL: str = "INFO"
LOG_MAX_BYTES: int = 5 * 1024 * 1024
LOG_BACKUP_COUNT: int = 3
LOG_TRANSCRIPT_CONTENT: bool = False

# 8.11 Privacy
REQUIRE_MANUAL_CONFIRMATION_FOR_FIRST_RECORDING: bool = True
SHOW_RECORDING_NOTIFICATION: bool = True
PRIVACY_NOTICE_TEXT: str = (
    "Questa applicazione registra e trascrive audio locale.\n"
    "Verificare di disporre delle autorizzazioni necessarie\n"
    "e di rispettare policy aziendali e normativa applicabile."
)

# 8.12 Glossario
# Vuoto per default: un testo segnaposto finirebbe nell'initial_prompt di Whisper e nei prompt LLM.
CUSTOM_GLOSSARY: str = ""
GLOSSARY_PLACEHOLDER_TEXT: str = (
    "Inserire termini tecnici, acronimi aziendali, nomi applicativi,\n"
    "piattaforme, prodotti, inglesismi e nomenclature ricorrenti."
)
