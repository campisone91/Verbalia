from __future__ import annotations

import logging
import os
import re
import sys
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from logging.handlers import RotatingFileHandler
from pathlib import Path

from config import (
    APP_NAME,
    LOG_BACKUP_COUNT,
    LOG_DIR,
    LOG_LEVEL,
    LOG_MAX_BYTES,
)

LOGGER_NAME: str = APP_NAME
LOG_FORMAT: str = (
    "%(asctime)s | %(levelname)s | %(threadName)s | "
    "%(name)s | session=%(session_id)s | %(message)s"
)
DEFAULT_SESSION_ID: str = "-"
# Permette ai test (e a eventuali processi ausiliari) di dirottare app.log fuori dalla cartella di produzione
LOG_DIR_ENV_VAR: str = "TEAMS_SCRIBE_LOG_DIR"

_LOGGING_LOCK = threading.RLock()
_CONTROL_CHARS_RE = re.compile(r"[\x00-\x1f\x7f]+")


class SessionContextFilter(logging.Filter):
    """Filtro di logging che inietta l'attributo session_id in ogni LogRecord.

    Il session_id del thread corrente (impostato dai worker di sessione) ha la precedenza
    sul valore globale, così sessioni concorrenti non si contaminano a vicenda nei log.
    """

    def __init__(self, session_id: str | None = None) -> None:
        super().__init__()
        self._lock = threading.RLock()
        self._thread_local = threading.local()
        self.session_id: str = _clean_session_id(session_id)

    def set_session_id(self, session_id: str | None) -> None:
        """Aggiorna in modo thread-safe il session_id globale associato ai log."""
        with self._lock:
            self.session_id = _clean_session_id(session_id)

    def set_thread_session_id(self, session_id: str | None) -> None:
        """Imposta (o rimuove con None) il session_id del solo thread corrente."""
        if isinstance(session_id, str) and session_id.strip():
            self._thread_local.session_id = session_id.strip()
        else:
            self._thread_local.session_id = None

    def current_session_id(self) -> str:
        """Restituisce il session_id effettivo per il thread corrente."""
        thread_value = getattr(self._thread_local, "session_id", None)
        if thread_value:
            return str(thread_value)
        with self._lock:
            return self.session_id

    def filter(self, record: logging.LogRecord) -> bool:
        setattr(record, "session_id", self.current_session_id())
        return True


def _clean_session_id(session_id: str | None) -> str:
    if isinstance(session_id, str) and session_id.strip():
        return session_id.strip()
    return DEFAULT_SESSION_ID


_SESSION_FILTER = SessionContextFilter()


def sanitize_for_log(value: str, max_len: int = 120) -> str:
    """Sanitizza una stringa per il log rimuovendo caratteri di controllo e limitando la lunghezza."""
    cleaned = _CONTROL_CHARS_RE.sub(" ", str(value))
    collapsed = " ".join(cleaned.split())
    if max_len <= 0:
        return ""
    if len(collapsed) <= max_len:
        return collapsed
    if max_len <= 3:
        return collapsed[:max_len]
    return f"{collapsed[: max_len - 3]}..."


def set_log_session_id(session_id: str | None) -> None:
    """Imposta globalmente il session_id corrente per tutti i messaggi di log."""
    _SESSION_FILTER.set_session_id(session_id)


def set_thread_log_session_id(session_id: str | None) -> None:
    """Imposta il session_id dei log per il solo thread corrente (None per rimuoverlo)."""
    _SESSION_FILTER.set_thread_session_id(session_id)


@contextmanager
def bind_log_session(session_id: str | None) -> Iterator[None]:
    """Context manager che associa temporaneamente un session_id ai log del thread corrente."""
    previous = getattr(_SESSION_FILTER._thread_local, "session_id", None)
    _SESSION_FILTER.set_thread_session_id(session_id)
    try:
        yield
    finally:
        _SESSION_FILTER.set_thread_session_id(previous)


def resolve_log_dir(log_dir: Path | None = None) -> Path:
    """Restituisce la directory dei log: argomento esplicito, variabile d'ambiente o LOG_DIR."""
    if log_dir is not None:
        return log_dir
    env_dir = os.environ.get(LOG_DIR_ENV_VAR, "").strip()
    if env_dir:
        return Path(env_dir)
    return LOG_DIR


def close_logging_handlers() -> None:
    """Chiude gli handler del logger applicativo (rilascia app.log, utile su Windows)."""
    with _LOGGING_LOCK:
        logger = logging.getLogger(LOGGER_NAME)
        for existing_handler in list(logger.handlers):
            logger.removeHandler(existing_handler)
            try:
                existing_handler.close()
            except OSError:
                pass


def _resolve_log_level(level: str | None) -> int:
    """Converte il nome del livello di log nel corrispondente valore intero di logging."""
    level_name = (level if level is not None else LOG_LEVEL).strip().upper()
    resolved = getattr(logging, level_name, None)
    if isinstance(resolved, int):
        return resolved
    return logging.INFO


def configure_logging(
    session_id: str | None = None,
    log_dir: Path | None = None,
    level: str | None = None,
) -> logging.Logger:
    """Configura il logger applicativo con RotatingFileHandler UTF-8 e StreamHandler."""
    with _LOGGING_LOCK:
        target_dir = resolve_log_dir(log_dir).resolve()
        target_dir.mkdir(parents=True, exist_ok=True)
        log_file_path = target_dir / "app.log"

        if session_id is not None:
            _SESSION_FILTER.set_session_id(session_id)

        numeric_level = _resolve_log_level(level)
        logger = logging.getLogger(LOGGER_NAME)
        logger.setLevel(numeric_level)
        logger.propagate = False

        # Rimuove e chiude gli handler esistenti per evitare duplicati o file lock residui
        for existing_handler in list(logger.handlers):
            logger.removeHandler(existing_handler)
            try:
                existing_handler.close()
            except OSError as exc:
                sys.stderr.write(
                    f"Errore durante la chiusura dell'handler di log: {exc}\n"
                )

        if _SESSION_FILTER not in logger.filters:
            logger.addFilter(_SESSION_FILTER)

        formatter = logging.Formatter(LOG_FORMAT)

        file_handler = RotatingFileHandler(
            filename=str(log_file_path),
            mode="a",
            maxBytes=LOG_MAX_BYTES,
            backupCount=LOG_BACKUP_COUNT,
            encoding="utf-8",
        )
        file_handler.setLevel(numeric_level)
        file_handler.setFormatter(formatter)
        file_handler.addFilter(_SESSION_FILTER)

        stream_handler = logging.StreamHandler(stream=sys.stderr)
        stream_handler.setLevel(numeric_level)
        stream_handler.setFormatter(formatter)
        stream_handler.addFilter(_SESSION_FILTER)

        logger.addHandler(file_handler)
        logger.addHandler(stream_handler)

        return logger


def get_logger(name: str | None = None) -> logging.Logger:
    """Restituisce il logger principale o un child logger sotto 'Verbalia'."""
    base_logger = logging.getLogger(LOGGER_NAME)
    if _SESSION_FILTER not in base_logger.filters:
        base_logger.addFilter(_SESSION_FILTER)

    if name is None or not name.strip() or name.strip() == LOGGER_NAME:
        return base_logger

    clean_name = name.strip()
    if clean_name.startswith(f"{LOGGER_NAME}."):
        child_logger = logging.getLogger(clean_name)
    else:
        child_logger = base_logger.getChild(clean_name)

    if _SESSION_FILTER not in child_logger.filters:
        child_logger.addFilter(_SESSION_FILTER)
    return child_logger
