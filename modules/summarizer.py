from __future__ import annotations

import json
import re
import threading
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

import requests

import config as _config
from config import (
    APP_NAME,
    APP_VERSION,
    PROMPTS_DIR,
)
from modules.contracts import (
    MeetingMetadata,
    OllamaError,
    SessionContext,
    ShutdownRequested,
    Stage,
    StageReporter,
    TranscriptSegment,
    atomic_write_json,
    atomic_write_text,
    now_tz,
    sha256_file,
    sha256_text,
)
from modules.logging_setup import get_logger
from modules.transcript_utils import (
    NO_SPEECH_MARKER,
    estimate_tokens,
    format_timestamp_hms,
    format_transcript_segment,
    parse_transcript_line,
    resolve_glossary,
    split_segments_by_time_window,
)

if TYPE_CHECKING:
    from modules.state_manager import StateManager

logger = get_logger("summarizer")

UNRESOLVED_PLACEHOLDER_RE: re.Pattern[str] = re.compile(r"\{\{[A-Z0-9_]+\}\}")
_ANY_PLACEHOLDER_RE: re.Pattern[str] = re.compile(r"\{\{[^{}]+\}\}")
_PROMPT_VERSION_RE: re.Pattern[str] = re.compile(r"^PROMPT_VERSION:\s*(?P<version>\d+)$")

_RETRYABLE_STATUS_CODES: frozenset[int] = frozenset({408, 429, 500, 502, 503, 504})
_STREAM_PROGRESS_EVERY_TOKENS: int = 32

_SYSTEM_PROMPT_ITALIAN: str = (
    "Sei un assistente aziendale specializzato nella redazione di verbali di riunione in "
    "lingua italiana. Hai due obiettivi di pari importanza: fedeltà, cioè riportare solo "
    "fatti, decisioni, nomi e scadenze presenti nel testo fornito senza mai inventarli; e "
    "completezza, cioè non tralasciare argomenti, argomentazioni, numeri, esempi, domande "
    "e risposte rilevanti. Scrivi in modo chiaro e ordinato, rispettando il formato richiesto."
)

# Parole di resoconto per minuto di blocco, per livello di dettaglio
_DETAIL_WORDS_PER_MINUTE: dict[str, tuple[int, int]] = {
    "standard": (20, 35),
    "dettagliato": (30, 50),
    "massimo": (45, 70),
}
_NARRATIVE_SECTION = "resoconto dettagliato"
_MAP_LIST_SECTIONS: tuple[str, ...] = (
    "decisioni",
    "action items",
    "questioni aperte",
    "rischi",
)


@dataclass(frozen=True, slots=True)
class SummarizerOptions:
    """Parametri di generazione fotografati all'avvio di ogni job (impostazioni utente)."""

    num_ctx: int
    num_predict_map: int
    num_predict_reduce: int
    temperature: float
    seed: int
    keep_alive: str
    connect_timeout_sec: int
    read_timeout_sec: int
    max_retries: int
    context_safety_margin: int
    map_block_duration_sec: int
    detail_level: str
    glossary: str
    user_name: str
    streaming: bool


def resolve_user_display_name(name: str | None = None) -> str:
    """Nome con cui riferirsi alla traccia [IO] nei prompt e nel DOCX."""
    raw = name if name is not None else _config.USER_DISPLAY_NAME
    clean = (raw or "").strip()
    return clean or "Utente locale"


def default_summarizer_options() -> SummarizerOptions:
    """Costruisce le opzioni leggendo i default di config al momento della chiamata."""
    return SummarizerOptions(
        num_ctx=int(_config.OLLAMA_NUM_CTX),
        num_predict_map=int(_config.OLLAMA_NUM_PREDICT_MAP),
        num_predict_reduce=int(_config.OLLAMA_NUM_PREDICT_REDUCE),
        temperature=float(_config.OLLAMA_TEMPERATURE),
        seed=int(_config.OLLAMA_SEED),
        keep_alive=str(_config.OLLAMA_KEEP_ALIVE),
        connect_timeout_sec=int(_config.OLLAMA_CONNECT_TIMEOUT_SEC),
        read_timeout_sec=int(_config.OLLAMA_READ_TIMEOUT_SEC),
        max_retries=int(_config.OLLAMA_MAX_RETRIES),
        context_safety_margin=int(_config.OLLAMA_CONTEXT_SAFETY_MARGIN),
        map_block_duration_sec=int(_config.MAP_BLOCK_DURATION_SEC),
        detail_level=str(_config.OLLAMA_DETAIL_LEVEL),
        glossary=resolve_glossary(_config.CUSTOM_GLOSSARY),
        user_name=resolve_user_display_name(),
        streaming=bool(_config.OLLAMA_STREAMING),
    )


# Esposto a livello di modulo per consentire ai test di azzerare il backoff
OLLAMA_RETRY_BASE_DELAY_SEC: float = float(_config.OLLAMA_RETRY_BASE_DELAY_SEC)


def load_and_render_prompt(
    prompt_path: Path,
    replacements: Mapping[str, str],
) -> str:
    """Carica un template prompt versionato, applica i placeholder e verifica assenza di {{...}}."""
    if not prompt_path.exists() or not prompt_path.is_file():
        raise OllamaError(f"File prompt non trovato: {prompt_path}")

    try:
        raw_content = prompt_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise OllamaError(
            f"Impossibile leggere il file prompt {prompt_path}: {exc}"
        ) from exc

    lines = raw_content.splitlines()
    if len(lines) < 2:
        raise OllamaError(
            f"Intestazione di versione mancante nel prompt: {prompt_path.name}"
        )

    version_line = lines[0].strip()
    name_line = lines[1].strip()
    version_match = _PROMPT_VERSION_RE.match(version_line)
    if version_match is None or int(version_match.group("version")) < 1:
        raise OllamaError(
            f"Versione prompt non valida in {prompt_path.name}: {version_line!r}"
        )
    if not name_line.startswith("PROMPT_NAME:") or not name_line.split(":", 1)[1].strip():
        raise OllamaError(
            f"Nome prompt mancante o non valido in {prompt_path.name}: {name_line!r}"
        )

    rendered = "\n".join(lines[2:]).lstrip("\n")
    for raw_key, value in replacements.items():
        placeholder = (
            raw_key
            if raw_key.startswith("{{") and raw_key.endswith("}}")
            else f"{{{{{raw_key}}}}}"
        )
        rendered = rendered.replace(placeholder, str(value))

    unresolved = UNRESOLVED_PLACEHOLDER_RE.findall(rendered)
    if not unresolved:
        unresolved = _ANY_PLACEHOLDER_RE.findall(rendered)
    if unresolved:
        unique_unresolved = sorted(set(unresolved))
        raise OllamaError(
            f"Placeholder non risolti nel prompt {prompt_path.name}: {', '.join(unique_unresolved)}"
        )

    return rendered


def _format_participants(metadata: MeetingMetadata) -> str:
    """Formatta l'elenco dei partecipanti dai metadati della riunione."""
    participants: list[str] = []
    for attendee in metadata.required_attendees:
        clean = attendee.strip()
        if clean and clean not in participants:
            participants.append(clean)
    for attendee in metadata.optional_attendees:
        clean = attendee.strip()
        if clean and clean not in participants:
            participants.append(f"{clean} (opzionale)")
    if not participants:
        return "Non disponibile (partecipanti non specificati)"
    return ", ".join(participants)


def _clean_agenda(metadata: MeetingMetadata) -> str:
    """Agenda senza il blocco di accesso Teams (le sessioni create prima della pulizia lo contengono)."""
    from modules.watcher import clean_outlook_agenda

    return clean_outlook_agenda(metadata.agenda or "")


def _format_meeting_metadata_block(
    metadata: MeetingMetadata,
    agenda_override: str | None = None,
) -> str:
    """Costruisce il blocco testuale dei metadati della riunione per la fase REDUCE."""
    agenda_text = (
        agenda_override if agenda_override is not None else _clean_agenda(metadata)
    ).strip()
    if not agenda_text:
        agenda_text = "Nessuna agenda disponibile."
    start_str = (
        metadata.scheduled_start.isoformat()
        if metadata.scheduled_start is not None
        else "Non specificato"
    )
    end_str = (
        metadata.scheduled_end.isoformat()
        if metadata.scheduled_end is not None
        else "Non specificato"
    )
    return (
        f"- Organizzatore: {metadata.organizer or 'Non disponibile'}\n"
        f"- Partecipanti da calendario: {_format_participants(metadata)}\n"
        f"- Inizio pianificato: {start_str}\n"
        f"- Fine pianificata: {end_str}\n"
        f"- Agenda:\n{agenda_text}"
    )


def _glossary_section(glossary: str) -> str:
    """Sezione glossario per i prompt; vuota (e quindi omessa) se il glossario non è configurato."""
    clean = resolve_glossary(glossary)
    if not clean:
        return ""
    return (
        "\n=== GLOSSARIO DI DOMINIO ===\n"
        "Usa il glossario solo per riconoscere o normalizzare termini effettivamente pronunciati:\n"
        f"{clean}\n"
    )


def _format_duration_it(total_sec: float) -> str:
    minutes = max(0, int(round(total_sec / 60.0)))
    if minutes < 1:
        return "meno di un minuto"
    if minutes < 60:
        return f"circa {minutes} minuti"
    hours, rem = divmod(minutes, 60)
    hours_label = "ora" if hours == 1 else "ore"
    return f"circa {hours} {hours_label} e {rem} minuti" if rem else f"circa {hours} {hours_label}"


def _target_words(block_minutes: float, detail_level: str) -> str:
    low, high = _DETAIL_WORDS_PER_MINUTE.get(
        detail_level.strip().lower(), _DETAIL_WORDS_PER_MINUTE["dettagliato"]
    )
    minutes = max(1.0, block_minutes)
    min_words = max(120, int(round(low * minutes / 10.0) * 10))
    max_words = max(min_words + 80, int(round(high * minutes / 10.0) * 10))
    return f"circa {min_words}-{max_words} parole"


def _split_markdown_sections(text: str) -> list[tuple[str, str]]:
    """Divide un testo Markdown nelle sezioni di secondo livello (## Titolo) -> [(titolo, corpo)]."""
    sections: list[tuple[str, str]] = []
    current_title = ""
    current_lines: list[str] = []
    for line in text.splitlines():
        match = re.match(r"^\s{0,3}#{1,2}\s+(?P<title>.+?)\s*#*\s*$", line)
        if match is not None:
            if current_title or any(item.strip() for item in current_lines):
                sections.append((current_title, "\n".join(current_lines).strip()))
            current_title = match.group("title").strip()
            current_lines = []
        else:
            current_lines.append(line)
    if current_title or any(item.strip() for item in current_lines):
        sections.append((current_title, "\n".join(current_lines).strip()))
    return sections


def _normalize_heading(title: str) -> str:
    clean = re.sub(r"^\d+[.)]\s*", "", title.replace("*", "")).strip().lower()
    return " ".join(clean.split())


def _find_section(sections: Sequence[tuple[str, str]], *keywords: str) -> str | None:
    for title, body in sections:
        normalized = _normalize_heading(title)
        if any(keyword in normalized for keyword in keywords):
            return body
    return None


def _extract_rolling_context(map_output: str) -> str:
    """Estrae la sezione Rolling Context dall'output Markdown di un blocco MAP."""
    pattern = re.compile(
        r"^#{1,3}\s*Rolling Context\b[^\n]*\n(?P<body>.+?)(?=^#{1,3}\s|\Z)",
        re.IGNORECASE | re.MULTILINE | re.DOTALL,
    )
    match = pattern.search(map_output)
    if match is not None:
        extracted = match.group("body").strip()
        if extracted:
            return extracted

    paragraphs = [p.strip() for p in map_output.strip().split("\n\n") if p.strip()]
    if paragraphs:
        return paragraphs[-1][:1800]
    return "Nessun contesto precedente disponibile."


def _compress_rolling_context_preserving_key_facts(
    rolling_context: str,
    max_chars: int,
) -> str:
    """Comprime il rolling context preservando prioritariamente decisioni e Action Items."""
    if len(rolling_context) <= max_chars:
        return rolling_context
    if max_chars <= 80:
        return rolling_context[:max_chars]

    lines = [line.strip() for line in rolling_context.splitlines() if line.strip()]
    priority_keywords = (
        "decision",
        "confermat",
        "action",
        "azion",
        "da assegnare",
        "responsabile",
        "scadenza",
        "rischi",
        "questioni",
    )
    priority_lines = [
        line
        for line in lines
        if any(kw in line.lower() for kw in priority_keywords)
    ]
    other_lines = [line for line in lines if line not in priority_lines]

    ordered_lines = priority_lines + other_lines
    selected: list[str] = []
    current_len = 0
    for line in ordered_lines:
        addition = len(line) + 1
        if current_len + addition > max_chars - 45:
            break
        selected.append(line)
        current_len += addition

    if not selected:
        return rolling_context[: max_chars - 40].rstrip() + "\n[Rolling context compresso]"
    return "\n".join(selected) + "\n[Rolling context compresso]"


def _validate_map_output(text: str) -> tuple[bool, list[str]]:
    """Verifica che l'output MAP v2 contenga resoconto, liste e Rolling Context."""
    lower = text.lower()
    checks: list[tuple[str, tuple[str, ...]]] = [
        ("Resoconto dettagliato", ("resoconto",)),
        ("Decisioni", ("decision",)),
        ("Action items", ("action item", "azion")),
        ("Questioni aperte", ("question",)),
        ("Rischi", ("risch", "criticit")),
        ("Rolling Context", ("rolling context",)),
    ]
    missing: list[str] = []
    for section_name, keywords in checks:
        if not any(kw in lower for kw in keywords):
            missing.append(section_name)
    return len(missing) == 0, missing


def _validate_reduce_detailed_output(text: str) -> tuple[bool, list[str]]:
    """Verifica che la sintesi REDUCE contenga Executive Summary, liste e struttura Action Items."""
    lower = text.lower()
    checks: list[tuple[str, tuple[str, ...]]] = [
        ("Executive Summary", ("executive summary",)),
        ("Decisioni", ("decisioni",)),
        ("Questioni aperte", ("questioni aperte",)),
        ("Rischi", ("rischi",)),
        ("Dipendenze", ("dipendenze",)),
        ("Action Items", ("action item",)),
    ]
    missing: list[str] = []
    for section_name, keywords in checks:
        if not any(kw in lower for kw in keywords):
            missing.append(section_name)

    has_pipe_table = (
        "|" in text
        and "responsabile" in lower
        and ("task" in lower or "azione" in lower)
    )
    has_no_actions_marker = "non sono emersi action items espliciti" in lower
    if not (has_pipe_table or has_no_actions_marker):
        missing.append("Tabella Action Items (o dicitura assenza Action Items)")

    return len(missing) == 0, missing


def _validate_reduce_email_output(text: str) -> tuple[bool, list[str]]:
    """Verifica che la mail di recap contenga oggetto, introduzione, TL;DR, decisioni, Next Steps e chiusura."""
    lower = text.lower()
    checks: list[tuple[str, tuple[str, ...]]] = [
        ("Oggetto", ("oggetto:", "oggetto")),
        ("Introduzione", ("introduzione", "buongiorno", "gentili", "ciao")),
        ("TL;DR", ("tl;dr", "tldr", "sintesi")),
        ("Decisioni", ("decisioni",)),
        ("Next Steps", ("next steps", "next step", "prossimi passi", "azioni")),
        ("Chiusura", ("chiusura", "saluto", "saluti", "disposizione")),
    ]
    missing: list[str] = []
    for section_name, keywords in checks:
        if not any(kw in lower for kw in keywords):
            missing.append(section_name)
    return len(missing) == 0, missing


def _demote_headings(text: str, levels: int = 1) -> str:
    """Abbassa di `levels` i titoli Markdown (### -> ####), con un massimo di 6 cancelletti."""
    def _repl(match: re.Match[str]) -> str:
        hashes = min(6, len(match.group("hashes")) + levels)
        return f"{'#' * hashes} {match.group('title')}"

    return re.sub(r"^(?P<hashes>#{1,6})\s+(?P<title>.+)$", _repl, text, flags=re.MULTILINE)


def _read_block_interval(map_path: Path, block_index: int, block_duration_sec: int) -> tuple[float, float]:
    meta_path = map_path.with_name(f"map_block_{block_index:03d}.meta.json")
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        return float(meta["block_start_sec"]), float(meta["block_end_sec"])
    except (OSError, ValueError, KeyError, TypeError):
        start = float(block_index * block_duration_sec)
        return start, start + float(block_duration_sec)


def _block_index_from_path(path: Path, fallback: int) -> int:
    match = re.search(r"map_block_(\d+)\.md$", path.name)
    return int(match.group(1)) if match else fallback


def assemble_detailed_narrative(
    map_paths: Sequence[Path],
    *,
    block_duration_sec: int | None = None,
) -> str:
    """Concatena i resoconti MAP in ordine, ciascuno con la propria intestazione temporale.

    La narrativa non passa da una seconda sintesi LLM: è il testo dei blocchi MAP,
    con i sotto-argomenti abbassati di un livello sotto l'intestazione del blocco.
    """
    duration = int(block_duration_sec or _config.MAP_BLOCK_DURATION_SEC)
    parts: list[str] = []
    for position, path in enumerate(map_paths):
        if not path.is_file():
            continue
        block_index = _block_index_from_path(path, position)
        start_sec, end_sec = _read_block_interval(path, block_index, duration)
        text = path.read_text(encoding="utf-8")
        narrative = _find_section(_split_markdown_sections(text), _NARRATIVE_SECTION)
        if narrative is None:
            # Blocchi MAP di versione precedente: si usa tutto tranne liste e rolling context
            narrative = "\n\n".join(
                body
                for title, body in _split_markdown_sections(text)
                if body and "rolling context" not in _normalize_heading(title)
            )
        narrative = narrative.strip()
        if not narrative:
            continue
        header = f"### {format_timestamp_hms(start_sec)} – {format_timestamp_hms(end_sec)}"
        parts.append(f"{header}\n\n{_demote_headings(narrative, 1)}")
    if not parts:
        return "Non è stato possibile ricostruire un resoconto dettagliato dalla trascrizione."
    return "\n\n".join(parts)


def _extract_map_lists(map_text: str) -> str:
    """Estrae da un blocco MAP le liste da consolidare (decisioni, azioni, questioni, rischi) e il contesto."""
    sections = _split_markdown_sections(map_text)
    chunks: list[str] = []
    for key in _MAP_LIST_SECTIONS:
        body = _find_section(sections, key.split()[0] if key != "action items" else "action")
        if body:
            chunks.append(f"#### {key.capitalize()}\n{body.strip()}")
    rolling = _find_section(sections, "rolling context")
    if rolling:
        chunks.append(f"#### Contesto del blocco\n{rolling.strip()}")
    if not chunks:
        return map_text.strip()
    return "\n\n".join(chunks)


def strip_narrative_section(detailed_minutes: str) -> str:
    """Rimuove dal verbale la sezione narrativa lunga (per la mail basta la sintesi)."""
    kept: list[str] = []
    for title, body in _split_markdown_sections(detailed_minutes):
        normalized = _normalize_heading(title)
        if "resoconto" in normalized or "dati della riunione" in normalized:
            continue
        kept.append(f"## {title}\n{body}" if title else body)
    return "\n\n".join(kept).strip() or detailed_minutes


class OllamaMapReduceSummarizer:
    """Pipeline Map-Reduce su Ollama locale con retry, budget contesto, cache MAP e repair."""

    def __init__(
        self,
        *,
        base_url: str | None = None,
        model: str | None = None,
        prompts_dir: Path = PROMPTS_DIR,
        state_manager: StateManager | None = None,
        stage_reporter: StageReporter | None = None,
        shutdown_event: threading.Event | None = None,
        ai_lock: threading.RLock | None = None,
        http_session: requests.Session | None = None,
        options: SummarizerOptions | None = None,
        on_progress: Callable[[int], None] | None = None,
    ) -> None:
        self._base_url: str = (base_url or _config.OLLAMA_BASE_URL).rstrip("/")
        self._model: str = model or _config.OLLAMA_MODEL
        self._prompts_dir: Path = prompts_dir.resolve()
        self._state_manager: StateManager | None = state_manager
        self._stage_reporter: StageReporter | None = stage_reporter
        self._shutdown_event: threading.Event | None = shutdown_event
        # Lock dedicato all'LLM: con la policy parallela non è condiviso con Whisper
        self._ai_lock: threading.RLock = (
            ai_lock if ai_lock is not None else threading.RLock()
        )
        self._options_override: SummarizerOptions | None = options
        self._on_progress: Callable[[int], None] | None = on_progress
        self._active_response: object | None = None
        self._cancel_requested: threading.Event = threading.Event()
        self.last_inference_stats: dict[str, float] = {}

        self._session: requests.Session = (
            http_session if http_session is not None else requests.Session()
        )
        self._session.headers.update(
            {
                "Content-Type": "application/json",
                "User-Agent": f"{APP_NAME}/{APP_VERSION}",
            }
        )
        opts = self.options
        self.available_input_tokens: int = max(
            128,
            opts.num_ctx - opts.num_predict_map - opts.context_safety_margin,
        )
        self.available_reduce_tokens: int = max(
            128,
            opts.num_ctx - opts.num_predict_reduce - opts.context_safety_margin,
        )

    @property
    def options(self) -> SummarizerOptions:
        """Opzioni effettive: quelle iniettate oppure i default letti adesso da config."""
        if self._options_override is not None:
            return self._options_override
        return default_summarizer_options()

    @property
    def model(self) -> str:
        """Restituisce il nome del modello Ollama configurato."""
        return self._model

    @property
    def state_manager(self) -> StateManager | None:
        """Restituisce lo StateManager attualmente associato alla sessione."""
        return self._state_manager

    @state_manager.setter
    def state_manager(self, value: StateManager | None) -> None:
        """Aggiorna lo StateManager associato alla sessione."""
        self._state_manager = value

    def set_state_manager(self, state_manager: StateManager | None) -> None:
        """Aggiorna lo StateManager associato per la sessione corrente."""
        self._state_manager = state_manager

    @property
    def stage_reporter(self) -> StageReporter | None:
        """Restituisce lo StageReporter attualmente associato."""
        return self._stage_reporter

    @stage_reporter.setter
    def stage_reporter(self, value: StageReporter | None) -> None:
        """Aggiorna lo StageReporter associato."""
        self._stage_reporter = value

    def set_stage_reporter(self, stage_reporter: StageReporter | None) -> None:
        """Aggiorna lo StageReporter associato."""
        self._stage_reporter = stage_reporter

    def set_progress_callback(self, callback: Callable[[int], None] | None) -> None:
        """Imposta la callback che riceve il numero di token generati durante lo streaming."""
        self._on_progress = callback

    def reset_cancel(self) -> None:
        """Ripristina il flag di cancellazione prima di iniziare un nuovo job."""
        self._cancel_requested.clear()

    def cancel_current(self) -> None:
        """Interrompe la richiesta HTTP Ollama in corso chiudendo lo stream."""
        self._cancel_requested.set()
        self.cancel_active_request()

    def cancel_active_request(self) -> None:
        """Chiude la risposta in streaming in corso (usato allo shutdown)."""
        response = self._active_response
        if response is not None and hasattr(response, "close"):
            try:
                response.close()  # type: ignore[union-attr]
            except Exception as exc:
                logger.debug("Chiusura della risposta Ollama in corso non riuscita: %s", exc)

    def _check_shutdown(self) -> None:
        if self._cancel_requested.is_set():
            raise OllamaError("Elaborazione Ollama annullata dall'utente.")
        if self._shutdown_event is not None and self._shutdown_event.is_set():
            raise ShutdownRequested("Esecuzione Ollama interrotta: arresto richiesto.")

    def _sleep_with_shutdown(self, delay_sec: float) -> None:
        if self._cancel_requested.is_set():
            raise OllamaError("Elaborazione Ollama annullata dall'utente.")
        if self._shutdown_event is not None:
            if self._shutdown_event.wait(timeout=max(0.0, delay_sec)):
                raise ShutdownRequested(
                    "Attesa retry Ollama interrotta: arresto richiesto."
                )
        else:
            time.sleep(max(0.0, delay_sec))

    def _report_progress(self, tokens: int) -> None:
        if self._on_progress is None:
            return
        try:
            self._on_progress(tokens)
        except Exception as exc:
            logger.debug("Callback di avanzamento Ollama fallita: %s", exc)

    def _consume_stream(self, response: object) -> dict[str, object]:
        """Legge una risposta NDJSON in streaming e restituisce il payload finale con il testo completo."""
        pieces: list[str] = []
        final: dict[str, object] = {}
        tokens = 0
        iter_lines = getattr(response, "iter_lines")
        lines: Iterator[bytes] = iter_lines()
        for raw_line in lines:
            self._check_shutdown()
            if not raw_line:
                continue
            try:
                chunk = json.loads(raw_line)
            except ValueError as exc:
                raise OllamaError("Chunk di streaming Ollama non è JSON valido.") from exc
            if not isinstance(chunk, Mapping):
                raise OllamaError("Chunk di streaming Ollama non è un oggetto JSON.")
            if chunk.get("error"):
                raise OllamaError(f"Errore restituito da Ollama: {chunk.get('error')}")
            piece = chunk.get("response")
            if isinstance(piece, str) and piece:
                pieces.append(piece)
                tokens += 1
                if tokens % _STREAM_PROGRESS_EVERY_TOKENS == 0:
                    self._report_progress(tokens)
            if chunk.get("done") is True:
                final = dict(chunk)
                break
        final["response"] = "".join(pieces)
        if "eval_count" not in final:
            final["eval_count"] = tokens
        eval_count = final.get("eval_count")
        self._report_progress(eval_count if isinstance(eval_count, int) and eval_count > 0 else tokens)
        return final

    def _log_inference_stats(self, prompt: str, response_text: str, body: Mapping[str, object], elapsed: float) -> None:
        def _num(key: str) -> float:
            value = body.get(key, 0)
            return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else 0.0

        prompt_tokens = _num("prompt_eval_count")
        eval_tokens = _num("eval_count")
        prompt_sec = _num("prompt_eval_duration") / 1e9
        eval_sec = _num("eval_duration") / 1e9
        prompt_tps = prompt_tokens / prompt_sec if prompt_sec > 0 else 0.0
        eval_tps = eval_tokens / eval_sec if eval_sec > 0 else 0.0
        self.last_inference_stats = {
            "prompt_tokens": prompt_tokens,
            "eval_tokens": eval_tokens,
            "prompt_tokens_per_sec": round(prompt_tps, 2),
            "eval_tokens_per_sec": round(eval_tps, 2),
            "elapsed_sec": round(elapsed, 2),
        }
        logger.info(
            "Inferenza Ollama completata: model=%s prompt_chars=%d response_chars=%d "
            "prompt_tokens=%d eval_tokens=%d prompt_tok_s=%.1f eval_tok_s=%.1f "
            "done_reason=%s durata_sec=%.2f",
            self._model,
            len(prompt),
            len(response_text),
            int(prompt_tokens),
            int(eval_tokens),
            prompt_tps,
            eval_tps,
            str(body.get("done_reason", "stop")),
            elapsed,
        )

    def _invoke_ollama(
        self,
        prompt: str,
        *,
        system_prompt: str = _SYSTEM_PROMPT_ITALIAN,
        num_predict: int | None = None,
        validation_attempt: int = 0,
        stage: Stage = Stage.SUMMARIZING_MAP,
        progress_detail: str = "",
    ) -> str:
        """Invia una richiesta a /api/generate con retry esponenziale e validazione risposta."""
        del stage, progress_detail
        opts = self.options
        url = f"{self._base_url}/api/generate"
        streaming = bool(opts.streaming)
        effective_temp = (
            min(0.4, round(opts.temperature + 0.15 * validation_attempt, 2))
            if validation_attempt > 0
            else opts.temperature
        )
        payload: dict[str, object] = {
            "model": self._model,
            "system": system_prompt,
            "prompt": prompt,
            "stream": streaming,
            "keep_alive": opts.keep_alive,
            "options": {
                "num_ctx": opts.num_ctx,
                "temperature": effective_temp,
                "repeat_penalty": 1.15,
                "repeat_last_n": 256,
                "seed": opts.seed,
                "num_predict": int(num_predict if num_predict is not None else opts.num_predict_map),
            },
        }

        max_attempts = max(1, opts.max_retries)
        last_error: Exception | None = None
        base_delay = float(OLLAMA_RETRY_BASE_DELAY_SEC)

        with self._ai_lock:
            for attempt in range(max_attempts):
                self._check_shutdown()
                started_at = time.monotonic()
                response: object | None = None
                try:
                    post_kwargs: dict[str, object] = {
                        "json": payload,
                        "timeout": (opts.connect_timeout_sec, opts.read_timeout_sec),
                    }
                    if streaming:
                        post_kwargs["stream"] = True
                    response = self._session.post(url, **post_kwargs)  # type: ignore[arg-type]
                    self._active_response = response
                    status_code = int(getattr(response, "status_code", 0))
                    if status_code != 200:
                        if status_code in _RETRYABLE_STATUS_CODES:
                            last_error = OllamaError(f"HTTP {status_code} ricevuto da Ollama.")
                            if attempt + 1 < max_attempts:
                                delay = base_delay * (2**attempt)
                                logger.warning(
                                    "Ollama ha restituito HTTP %d (tentativo %d/%d). Retry tra %.1fs.",
                                    status_code,
                                    attempt + 1,
                                    max_attempts,
                                    delay,
                                )
                                self._sleep_with_shutdown(delay)
                                continue
                            raise OllamaError(
                                f"Ollama ha restituito HTTP {status_code} dopo {max_attempts} tentativi."
                            )
                        raise OllamaError(
                            f"Errore client HTTP {status_code} da Ollama (nessun retry effettuato)."
                        )

                    if streaming and hasattr(response, "iter_lines"):
                        body: object = self._consume_stream(response)
                    else:
                        try:
                            body = getattr(response, "json")()
                        except ValueError as exc:
                            raise OllamaError(
                                "La risposta di Ollama non contiene JSON valido."
                            ) from exc
                except (
                    requests.exceptions.ConnectTimeout,
                    requests.exceptions.ReadTimeout,
                    requests.exceptions.Timeout,
                    requests.exceptions.ConnectionError,
                    requests.exceptions.ChunkedEncodingError,
                    requests.exceptions.RequestException,
                    AttributeError,
                    ValueError,
                ) as exc:
                    self._check_shutdown()
                    if isinstance(exc, AttributeError):
                        raise OllamaError("Richiesta Ollama interrotta.") from exc
                    if self._shutdown_event is not None and self._shutdown_event.is_set():
                        raise ShutdownRequested("Richiesta Ollama annullata per arresto.") from exc
                    last_error = exc
                    if attempt + 1 < max_attempts:
                        delay = base_delay * (2**attempt)
                        logger.warning(
                            "Errore di rete Ollama (tentativo %d/%d): %s. Retry tra %.1fs.",
                            attempt + 1,
                            max_attempts,
                            type(exc).__name__,
                            delay,
                        )
                        self._sleep_with_shutdown(delay)
                        continue
                    raise OllamaError(
                        f"Connessione a Ollama fallita dopo {max_attempts} tentativi: {exc}"
                    ) from exc
                finally:
                    self._active_response = None
                    if streaming and response is not None and hasattr(response, "close"):
                        try:
                            response.close()  # type: ignore[union-attr]
                        except Exception as close_exc:
                            logger.debug("Chiusura risposta Ollama: %s", close_exc)

                if not isinstance(body, Mapping):
                    raise OllamaError(
                        "Il payload JSON di Ollama non è un oggetto valido."
                    )

                if body.get("done") is not True:
                    raise OllamaError(
                        "Risposta Ollama incompleta: il campo 'done' non è True."
                    )

                response_text = body.get("response")
                if not isinstance(response_text, str) or not response_text.strip():
                    raise OllamaError(
                        "Risposta Ollama non valida: campo 'response' vuoto o non stringa."
                    )

                self._log_inference_stats(prompt, response_text, body, time.monotonic() - started_at)
                return response_text.strip()

        raise OllamaError(
            f"Impossibile completare la richiesta Ollama: {last_error}"
        )

    def _generate_with_validation_and_repair(
        self,
        prompt: str,
        target_artifact_path: Path,
        validator: Callable[[str], tuple[bool, list[str]]],
        artifact_label: str,
        *,
        num_predict: int | None = None,
    ) -> str:
        """Esegue la generazione, valida la struttura e tenta un singolo repair se necessario."""
        initial_output = self._invoke_ollama(prompt, num_predict=num_predict, validation_attempt=0)
        is_valid, missing_sections = validator(initial_output)
        if is_valid:
            return initial_output

        invalid_backup_path = target_artifact_path.with_suffix(".invalid.md")
        atomic_write_text(invalid_backup_path, initial_output + "\n")
        logger.warning(
            "Output %s non valido (sezioni mancanti: %s). Risposta salvata in %s. Avvio singolo tentativo di repair.",
            artifact_label,
            ", ".join(missing_sections),
            invalid_backup_path.name,
        )

        repair_prompt = (
            "La tua precedente risposta non rispetta la struttura obbligatoria richiesta.\n"
            f"Elementi o sezioni mancanti: {', '.join(missing_sections)}.\n\n"
            "Riformula e completa la seguente risposta includendo TUTTE le sezioni obbligatorie, "
            "mantenendo tutto il contenuto già presente e senza inventare alcun dato:\n\n"
            "=== RISPOSTA DA CORREGGERE ===\n"
            f"{initial_output}\n"
        )
        repaired_output = self._invoke_ollama(repair_prompt, num_predict=num_predict, validation_attempt=1)
        repaired_valid, still_missing = validator(repaired_output)
        if not repaired_valid:
            raise OllamaError(
                f"Output {artifact_label} ancora non valido dopo il tentativo di repair. "
                f"Sezioni mancanti: {', '.join(still_missing)}"
            )
        return repaired_output

    _generate_validated_markdown = _generate_with_validation_and_repair

    def _fit_map_prompt_within_budget(
        self,
        template_path: Path,
        base_replacements: dict[str, str],
        transcript_lines: list[str],
    ) -> list[str]:
        """Adatta agenda, rolling context e transcript per rispettare available_input_tokens."""
        budget = self.available_input_tokens
        replacements = dict(base_replacements)
        full_transcript = (
            "\n".join(transcript_lines)
            if transcript_lines
            else NO_SPEECH_MARKER
        )
        replacements["TRANSCRIPT"] = full_transcript

        rendered = load_and_render_prompt(template_path, replacements)
        if estimate_tokens(rendered) <= budget:
            return [rendered]

        # Passo 1: ridurre l'agenda
        original_agenda = replacements.get("AGENDA", "")
        if len(original_agenda) > 200:
            replacements["AGENDA"] = (
                original_agenda[:200].rstrip()
                + "\n[Agenda ridotta per rispetto del budget di contesto]"
            )
            logger.warning(
                "Budget contesto MAP superato: agenda ridotta da %d a %d caratteri.",
                len(original_agenda),
                len(replacements["AGENDA"]),
            )
            rendered = load_and_render_prompt(template_path, replacements)
            if estimate_tokens(rendered) <= budget:
                return [rendered]

        # Passo 2: comprimere il rolling context preservando decisioni e Action Items
        original_rc = replacements.get("ROLLING_CONTEXT", "")
        if len(original_rc) > 240:
            replacements["ROLLING_CONTEXT"] = (
                _compress_rolling_context_preserving_key_facts(original_rc, 240)
            )
            logger.warning(
                "Budget contesto MAP superato: rolling context compresso da %d a %d caratteri.",
                len(original_rc),
                len(replacements["ROLLING_CONTEXT"]),
            )
            rendered = load_and_render_prompt(template_path, replacements)
            if estimate_tokens(rendered) <= budget:
                return [rendered]

        # Passo 3: dividere il transcript in sotto-blocchi
        empty_replacements = dict(replacements)
        empty_replacements["TRANSCRIPT"] = ""
        base_overhead_tokens = estimate_tokens(
            load_and_render_prompt(template_path, empty_replacements)
        )
        available_for_transcript_tokens = max(64, budget - base_overhead_tokens)
        max_transcript_chars = max(180, int(available_for_transcript_tokens * 3.0))

        sub_transcripts: list[str] = []
        current_chunk_lines: list[str] = []
        current_chars = 0

        source_lines = transcript_lines if transcript_lines else [full_transcript]
        for line in source_lines:
            if len(line) > max_transcript_chars:
                if current_chunk_lines:
                    sub_transcripts.append("\n".join(current_chunk_lines))
                    current_chunk_lines = []
                    current_chars = 0
                for start_idx in range(0, len(line), max_transcript_chars):
                    sub_transcripts.append(
                        line[start_idx : start_idx + max_transcript_chars]
                    )
                continue

            line_len = len(line) + 1
            if current_chunk_lines and current_chars + line_len > max_transcript_chars:
                sub_transcripts.append("\n".join(current_chunk_lines))
                current_chunk_lines = [line]
                current_chars = len(line)
            else:
                current_chunk_lines.append(line)
                current_chars += line_len

        if current_chunk_lines:
            sub_transcripts.append("\n".join(current_chunk_lines))

        logger.warning(
            "Budget contesto MAP superato: transcript suddiviso in %d sotto-blocchi.",
            len(sub_transcripts),
        )

        prompts: list[str] = []
        for sub_text in sub_transcripts:
            sub_rep = dict(replacements)
            sub_rep["TRANSCRIPT"] = sub_text
            prompts.append(load_and_render_prompt(template_path, sub_rep))
        return prompts

    def _is_map_cache_valid(
        self,
        block_index: int,
        map_path: Path,
        meta_path: Path,
        expected_input_sha256: str,
        expected_prompt_sha256: str,
    ) -> bool:
        """Verifica se il blocco MAP esistente su disco è valido e riutilizzabile."""
        if not map_path.exists() or not meta_path.exists():
            return False
        try:
            meta_raw = json.loads(meta_path.read_text(encoding="utf-8"))
            if not isinstance(meta_raw, Mapping):
                return False
            if meta_raw.get("block_index") != block_index:
                return False
            if meta_raw.get("input_sha256") != expected_input_sha256:
                return False
            if meta_raw.get("prompt_sha256") != expected_prompt_sha256:
                return False
            if meta_raw.get("model") != self._model:
                return False
            actual_output_sha256 = sha256_file(map_path)
            if meta_raw.get("output_sha256") != actual_output_sha256:
                return False
            content = map_path.read_text(encoding="utf-8").strip()
            return bool(content)
        except (OSError, UnicodeDecodeError, ValueError):
            return False

    def _record_mapped_block_in_state(self, block_index: int) -> None:
        if self._state_manager is None:
            return

        def _apply(ctx: SessionContext) -> None:
            if block_index not in ctx.mapped_blocks:
                ctx.mapped_blocks.append(block_index)
                ctx.mapped_blocks.sort()

        mutate_fn = getattr(self._state_manager, "mutate", None)
        if callable(mutate_fn):
            mutate_fn(_apply)
        else:
            ctx = self._state_manager.load()
            _apply(ctx)
            self._state_manager.save(ctx)

    def _record_reduce_flag_in_state(self, *, detailed: bool = False, email: bool = False) -> None:
        if self._state_manager is None:
            return

        def _apply(ctx: SessionContext) -> None:
            if detailed:
                ctx.detailed_reduce_completed = True
            if email:
                ctx.email_reduce_completed = True

        mutate_fn = getattr(self._state_manager, "mutate", None)
        if callable(mutate_fn):
            mutate_fn(_apply)
        else:
            ctx = self._state_manager.load()
            _apply(ctx)
            self._state_manager.save(ctx)

    def run_map_phase(
        self,
        transcript_path: Path,
        metadata: MeetingMetadata,
        session_dir: Path,
    ) -> list[Path]:
        """Esegue la fase MAP su finestre temporali con idempotenza e rolling context."""
        if not transcript_path.exists() or not transcript_path.is_file():
            raise OllamaError(f"Transcript per fase MAP non trovato: {transcript_path}")

        opts = self.options
        block_duration = max(60, int(opts.map_block_duration_sec))
        raw_lines = transcript_path.read_text(encoding="utf-8").splitlines()
        segments: list[TranscriptSegment] = []
        for line in raw_lines:
            parsed = parse_transcript_line(line)
            if parsed is not None:
                segments.append(parsed)

        windows = split_segments_by_time_window(
            segments,
            window_duration_sec=block_duration,
        )
        if not windows:
            windows = [(0, 0.0, float(block_duration), [])]
        meeting_end_sec = max((seg.end_sec for seg in segments), default=0.0)

        maps_dir = session_dir.resolve() / "maps"
        maps_dir.mkdir(parents=True, exist_ok=True)

        map_prompt_path = self._prompts_dir / "map_chunk.txt"
        if not map_prompt_path.exists():
            raise OllamaError(f"Prompt MAP mancante: {map_prompt_path}")
        # Cambiare template, livello di dettaglio o nome utente invalida la cache MAP
        prompt_template_sha256 = sha256_text(
            map_prompt_path.read_text(encoding="utf-8")
            + f"|{opts.detail_level}|{opts.user_name}|{block_duration}|{opts.glossary}"
        )

        rolling_context = "Nessun contesto precedente (inizio riunione)."
        generated_paths: list[Path] = []
        total_blocks = len(windows)

        for block_index, start_sec, end_sec, block_segments in windows:
            self._check_shutdown()
            if self._stage_reporter is not None:
                self._stage_reporter.set_stage(
                    Stage.SUMMARIZING_MAP,
                    f"Sintesi blocco MAP {block_index + 1}/{total_blocks}",
                    current=block_index + 1,
                    total=total_blocks,
                )

            block_end_effective = min(end_sec, max(start_sec + 60.0, meeting_end_sec))
            block_start_hms = format_timestamp_hms(start_sec)
            block_end_hms = format_timestamp_hms(block_end_effective)
            transcript_lines = [
                format_transcript_segment(seg) for seg in block_segments
            ]
            block_transcript_str = (
                "\n".join(transcript_lines)
                if transcript_lines
                else NO_SPEECH_MARKER
            )

            map_path = maps_dir / f"map_block_{block_index:03d}.md"
            meta_path = maps_dir / f"map_block_{block_index:03d}.meta.json"
            rolling_path = maps_dir / f"rolling_context_{block_index:03d}.md"

            input_signature = (
                f"{block_start_hms}|{block_end_hms}|{rolling_context}|{block_transcript_str}"
            )
            input_sha256 = sha256_text(input_signature)

            if self._is_map_cache_valid(
                block_index=block_index,
                map_path=map_path,
                meta_path=meta_path,
                expected_input_sha256=input_sha256,
                expected_prompt_sha256=prompt_template_sha256,
            ):
                logger.info(
                    "Blocco MAP %03d valido in cache (sha256 verificato), riutilizzo artefatto.",
                    block_index,
                )
                cached_map = map_path.read_text(encoding="utf-8")
                if rolling_path.exists():
                    rolling_context = rolling_path.read_text(encoding="utf-8").strip()
                else:
                    rolling_context = _extract_rolling_context(cached_map)
                    atomic_write_text(rolling_path, rolling_context + "\n")
                self._record_mapped_block_in_state(block_index)
                generated_paths.append(map_path)
                continue

            spoken_minutes = max(1.0, (block_end_effective - start_sec) / 60.0)
            base_replacements = {
                "MEETING_TITLE": metadata.title or "Riunione Teams",
                "ORGANIZER": metadata.organizer or "Non disponibile",
                "PARTICIPANTS": _format_participants(metadata),
                "AGENDA": _clean_agenda(metadata) or "Nessuna agenda disponibile.",
                "GLOSSARY_SECTION": _glossary_section(opts.glossary),
                "USER_NAME": opts.user_name,
                "MEETING_DURATION": _format_duration_it(meeting_end_sec),
                "TARGET_WORDS": _target_words(spoken_minutes, opts.detail_level),
                "BLOCK_START": block_start_hms,
                "BLOCK_END": block_end_hms,
                "ROLLING_CONTEXT": rolling_context,
            }

            prompts_to_run = self._fit_map_prompt_within_budget(
                template_path=map_prompt_path,
                base_replacements=base_replacements,
                transcript_lines=transcript_lines,
            )

            if len(prompts_to_run) == 1:
                final_map_md = self._generate_with_validation_and_repair(
                    prompt=prompts_to_run[0],
                    target_artifact_path=map_path,
                    validator=_validate_map_output,
                    artifact_label=f"MAP blocco {block_index:03d}",
                    num_predict=opts.num_predict_map,
                )
            else:
                sub_outputs: list[str] = []
                for sub_idx, sub_prompt in enumerate(prompts_to_run):
                    sub_md = self._generate_with_validation_and_repair(
                        prompt=sub_prompt,
                        target_artifact_path=map_path,
                        validator=_validate_map_output,
                        artifact_label=f"MAP blocco {block_index:03d} parte {sub_idx + 1}",
                        num_predict=opts.num_predict_map,
                    )
                    sub_outputs.append(sub_md)
                final_map_md = _merge_map_sub_outputs(sub_outputs)

            atomic_write_text(map_path, final_map_md.rstrip() + "\n")
            rolling_context = _extract_rolling_context(final_map_md)
            atomic_write_text(rolling_path, rolling_context.rstrip() + "\n")

            output_sha256 = sha256_file(map_path)
            meta_payload: dict[str, object] = {
                "block_index": block_index,
                "block_start_sec": float(start_sec),
                "block_end_sec": float(block_end_effective),
                "input_sha256": input_sha256,
                "prompt_sha256": prompt_template_sha256,
                "model": self._model,
                "output_sha256": output_sha256,
                "inference": dict(self.last_inference_stats),
                "completed_at": now_tz().isoformat(),
            }
            atomic_write_json(meta_path, meta_payload)
            self._record_mapped_block_in_state(block_index)
            generated_paths.append(map_path)

        return generated_paths

    def _render_reduce_prompt(
        self,
        prompt_path: Path,
        metadata: MeetingMetadata,
        items: Sequence[str],
        meeting_duration: str,
        *,
        short_agenda: bool = False,
    ) -> str:
        opts = self.options
        agenda_override = _clean_agenda(metadata)[:200] if short_agenda else None
        return load_and_render_prompt(
            prompt_path,
            {
                "MEETING_TITLE": metadata.title or "Riunione Teams",
                "MEETING_METADATA": _format_meeting_metadata_block(metadata, agenda_override),
                "MEETING_DURATION": meeting_duration,
                "USER_NAME": opts.user_name,
                "GLOSSARY_SECTION": _glossary_section(opts.glossary),
                "MAP_BLOCKS": "\n\n".join(items),
            },
        )

    def _reduce_items(
        self,
        items: list[str],
        metadata: MeetingMetadata,
        meeting_duration: str,
        target_path: Path,
        depth: int = 0,
    ) -> str:
        """REDUCE delle liste; se il materiale supera il budget procede per gruppi (REDUCE gerarchico)."""
        self._check_shutdown()
        prompt_path = self._prompts_dir / "reduce_detailed.txt"
        budget = self.available_reduce_tokens
        rendered = self._render_reduce_prompt(prompt_path, metadata, items, meeting_duration)
        if estimate_tokens(rendered) > budget:
            rendered = self._render_reduce_prompt(
                prompt_path, metadata, items, meeting_duration, short_agenda=True
            )

        if estimate_tokens(rendered) <= budget or len(items) <= 1 or depth >= 3:
            if estimate_tokens(rendered) > budget:
                overhead = estimate_tokens(
                    self._render_reduce_prompt(prompt_path, metadata, [], meeting_duration, short_agenda=True)
                )
                per_item_chars = max(300, int((budget - overhead) * 3.0 // max(1, len(items))))
                compressed = [
                    _compress_rolling_context_preserving_key_facts(item, per_item_chars)
                    for item in items
                ]
                logger.warning(
                    "Budget REDUCE superato anche dopo il raggruppamento: materiale compresso "
                    "preservando decisioni e Action Items."
                )
                rendered = self._render_reduce_prompt(
                    prompt_path, metadata, compressed, meeting_duration, short_agenda=True
                )
            return self._generate_with_validation_and_repair(
                prompt=rendered,
                target_artifact_path=target_path,
                validator=_validate_reduce_detailed_output,
                artifact_label="REDUCE detailed_minutes" if depth == 0 else f"REDUCE gruppo (livello {depth})",
                num_predict=self.options.num_predict_reduce,
            )

        overhead = estimate_tokens(
            self._render_reduce_prompt(prompt_path, metadata, [], meeting_duration, short_agenda=True)
        )
        group_budget = max(256, budget - overhead)
        groups: list[list[str]] = []
        current: list[str] = []
        current_tokens = 0
        for item in items:
            item_tokens = estimate_tokens(item) + 2
            if current and current_tokens + item_tokens > group_budget:
                groups.append(current)
                current, current_tokens = [], 0
            current.append(item)
            current_tokens += item_tokens
        if current:
            groups.append(current)
        if len(groups) <= 1:
            groups = [items[: len(items) // 2], items[len(items) // 2 :]]

        logger.info(
            "REDUCE gerarchico: %d blocchi divisi in %d gruppi (livello %d).",
            len(items),
            len(groups),
            depth + 1,
        )
        partials: list[str] = []
        for group_index, group in enumerate(groups):
            partial = self._reduce_items(group, metadata, meeting_duration, target_path, depth + 1)
            partials.append(f"### Sintesi del gruppo {group_index + 1}\n{partial}")
        return self._reduce_items(partials, metadata, meeting_duration, target_path, depth + 1)

    def run_reduce_detailed(
        self,
        map_paths: Sequence[Path],
        metadata: MeetingMetadata,
        session_dir: Path,
        *,
        started_at: datetime | None = None,
        ended_at: datetime | None = None,
    ) -> Path:
        """Genera reduce/detailed_minutes.md: sintesi e liste via LLM, resoconto assemblato dai MAP."""
        self._check_shutdown()
        if not map_paths:
            raise OllamaError("Nessun file MAP fornito per la fase REDUCE dettagliata.")

        if self._stage_reporter is not None:
            self._stage_reporter.set_stage(
                Stage.SUMMARIZING_REDUCE,
                "Generazione verbale dettagliato (REDUCE)",
            )

        opts = self.options
        items: list[str] = []
        last_end_sec = 0.0
        for position, path in enumerate(map_paths):
            if not path.exists() or not path.is_file():
                raise OllamaError(f"File blocco MAP mancante: {path}")
            block_index = _block_index_from_path(path, position)
            start_sec, end_sec = _read_block_interval(path, block_index, opts.map_block_duration_sec)
            last_end_sec = max(last_end_sec, end_sec)
            block_text = path.read_text(encoding="utf-8").strip()
            items.append(
                f"### Blocco {block_index + 1} ({format_timestamp_hms(start_sec)} - "
                f"{format_timestamp_hms(end_sec)})\n{_extract_map_lists(block_text)}"
            )

        if started_at is not None and ended_at is not None:
            duration_sec = max(0.0, (ended_at - started_at).total_seconds())
        else:
            duration_sec = last_end_sec
        meeting_duration = _format_duration_it(duration_sec)

        reduce_dir = session_dir.resolve() / "reduce"
        reduce_dir.mkdir(parents=True, exist_ok=True)
        detailed_path = reduce_dir / "detailed_minutes.md"
        llm_path = reduce_dir / "reduce_llm.md"

        llm_output = self._reduce_items(items, metadata, meeting_duration, detailed_path)
        atomic_write_text(llm_path, llm_output.rstrip() + "\n")

        narrative = assemble_detailed_narrative(
            map_paths, block_duration_sec=opts.map_block_duration_sec
        )
        detailed_md = _assemble_detailed_minutes(
            llm_output=llm_output,
            narrative=narrative,
            metadata=metadata,
            meeting_duration=meeting_duration,
            started_at=started_at,
            ended_at=ended_at,
        )

        atomic_write_text(detailed_path, detailed_md.rstrip() + "\n")
        self._record_reduce_flag_in_state(detailed=True)
        logger.info(
            "Verbale dettagliato generato: path=%s parole=%d sha256=%s",
            detailed_path.name,
            len(detailed_md.split()),
            sha256_file(detailed_path)[:12],
        )
        return detailed_path

    def run_reduce_email(
        self,
        detailed_minutes_path: Path,
        metadata: MeetingMetadata,
        session_dir: Path,
    ) -> Path:
        """Esegue la fase REDUCE generando la bozza mail reduce/recap_email.md."""
        self._check_shutdown()
        if not detailed_minutes_path.exists() or not detailed_minutes_path.is_file():
            raise OllamaError(
                f"File verbale dettagliato mancante per generazione mail: {detailed_minutes_path}"
            )

        if self._stage_reporter is not None:
            self._stage_reporter.set_stage(
                Stage.SUMMARIZING_REDUCE,
                "Generazione bozza mail di recap (REDUCE)",
            )

        detailed_text = detailed_minutes_path.read_text(encoding="utf-8").strip()
        if not detailed_text:
            raise OllamaError("Il file detailed_minutes.md è vuoto.")
        # La mail si basa sulla sintesi: il resoconto narrativo completo resta solo nel verbale
        summary_text = strip_narrative_section(detailed_text)

        prompt_path = self._prompts_dir / "reduce_email.txt"
        replacements = {
            "MEETING_TITLE": metadata.title or "Riunione Teams",
            "DETAILED_MINUTES": summary_text,
        }
        rendered_prompt = load_and_render_prompt(prompt_path, replacements)

        budget = self.available_reduce_tokens
        if estimate_tokens(rendered_prompt) > budget:
            max_chars = max(400, int(budget * 2.5))
            replacements["DETAILED_MINUTES"] = (
                _compress_rolling_context_preserving_key_facts(
                    summary_text,
                    max_chars,
                )
            )
            logger.warning(
                "Budget contesto superato in REDUCE email: verbale compresso preservando decisioni e Action Items."
            )
            rendered_prompt = load_and_render_prompt(prompt_path, replacements)

        reduce_dir = session_dir.resolve() / "reduce"
        reduce_dir.mkdir(parents=True, exist_ok=True)
        email_path = reduce_dir / "recap_email.md"

        email_md = self._generate_with_validation_and_repair(
            prompt=rendered_prompt,
            target_artifact_path=email_path,
            validator=_validate_reduce_email_output,
            artifact_label="REDUCE recap_email",
            num_predict=self.options.num_predict_reduce,
        )

        atomic_write_text(email_path, email_md.rstrip() + "\n")
        self._record_reduce_flag_in_state(email=True)
        logger.info(
            "Bozza mail di recap generata: path=%s sha256=%s",
            email_path.name,
            sha256_file(email_path)[:12],
        )
        return email_path

    def unload_model(self) -> None:
        """Richiede a Ollama lo scaricamento del modello dalla memoria (keep_alive=0)."""
        url = f"{self._base_url}/api/generate"
        payload: dict[str, object] = {
            "model": self._model,
            "stream": False,
            "keep_alive": 0,
        }
        with self._ai_lock:
            try:
                response = self._session.post(
                    url,
                    json=payload,
                    timeout=(self.options.connect_timeout_sec, 30),
                )
                if response.status_code != 200:
                    logger.warning(
                        "Unload del modello Ollama %s ha restituito HTTP %d.",
                        self._model,
                        response.status_code,
                    )
                else:
                    logger.info(
                        "Modello Ollama %s scaricato dalla memoria (keep_alive=0).",
                        self._model,
                    )
            except Exception as exc:
                logger.warning(
                    "Impossibile scaricare il modello Ollama %s dalla memoria: %s",
                    self._model,
                    exc,
                )


def _merge_map_sub_outputs(sub_outputs: Sequence[str]) -> str:
    """Unisce i MAP di sotto-blocchi dello stesso intervallo in un unico documento v2."""
    narratives: list[str] = []
    lists: dict[str, list[str]] = {key: [] for key in _MAP_LIST_SECTIONS}
    rolling = ""
    for output in sub_outputs:
        sections = _split_markdown_sections(output)
        narrative = _find_section(sections, _NARRATIVE_SECTION)
        if narrative:
            narratives.append(narrative.strip())
        for key in _MAP_LIST_SECTIONS:
            body = _find_section(sections, key.split()[0] if key != "action items" else "action")
            if body:
                lists[key].append(body.strip())
        rolling = _find_section(sections, "rolling context") or rolling
    if not narratives:
        return "\n\n---\n\n".join(sub_outputs)
    parts = ["## Resoconto dettagliato", "\n\n".join(narratives)]
    titles = {
        "decisioni": "Decisioni",
        "action items": "Action items",
        "questioni aperte": "Questioni aperte",
        "rischi": "Rischi",
    }
    for key in _MAP_LIST_SECTIONS:
        parts.append(f"## {titles[key]}")
        parts.append("\n".join(lists[key]) if lists[key] else "- Nessun elemento.")
    parts.append("## Rolling Context")
    parts.append(rolling.strip() or "Nessun contesto disponibile.")
    return "\n\n".join(parts)


def _assemble_detailed_minutes(
    *,
    llm_output: str,
    narrative: str,
    metadata: MeetingMetadata,
    meeting_duration: str,
    started_at: datetime | None,
    ended_at: datetime | None,
) -> str:
    """Compone il verbale finale: sintesi e liste dall'LLM, dati e resoconto in modo deterministico."""
    sections = _split_markdown_sections(llm_output)

    def _section(*keywords: str, fallback: str) -> str:
        body = _find_section(sections, *keywords)
        return body.strip() if body and body.strip() else fallback

    executive = _section("executive", "sintesi", fallback="Sintesi non disponibile.")
    decisions = _section(
        "decision", fallback="Non sono state verbalizzate decisioni definitive."
    )
    open_points = _section("questioni", "punti aperti", fallback="Nessuna questione aperta segnalata.")
    risks = _section("rischi", fallback="Nessun rischio segnalato.")
    dependencies = _section("dipendenze", fallback="Nessuna dipendenza segnalata.")
    actions = _section("action", fallback="Non sono emersi Action Items espliciti.")

    data_lines = [f"- Titolo: {metadata.title or 'Riunione Teams'}"]
    if started_at is not None:
        end_part = f" - {ended_at.strftime('%H:%M')}" if ended_at is not None else ""
        data_lines.append(f"- Data e ora: {started_at.strftime('%d/%m/%Y %H:%M')}{end_part}")
    data_lines.append(f"- Durata: {meeting_duration}")
    data_lines.append(f"- Organizzatore: {metadata.organizer or 'Non disponibile'}")
    data_lines.append(f"- Partecipanti da calendario: {_format_participants(metadata)}")
    agenda = _clean_agenda(metadata)
    if agenda:
        agenda_inline = "; ".join(line.strip() for line in agenda.splitlines() if line.strip())
        data_lines.append(f"- Agenda: {agenda_inline[:1500]}")

    return "\n\n".join(
        [
            "## 1. Executive Summary",
            executive,
            "## 2. Dati della Riunione",
            "\n".join(data_lines),
            "## 3. Resoconto dettagliato per argomento",
            narrative.strip(),
            "## 4. Decisioni Confermate",
            decisions,
            "## 5. Questioni Aperte",
            open_points,
            "## 6. Rischi",
            risks,
            "## 7. Dipendenze",
            dependencies,
            "## 8. Action Items",
            actions,
        ]
    )
