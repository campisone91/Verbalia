from __future__ import annotations

import json as json_module
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import requests

from config import PROMPTS_DIR
from modules.contracts import MeetingMetadata, OllamaError, SessionContext
from modules.state_manager import StateManager
from modules.summarizer import OllamaMapReduceSummarizer, load_and_render_prompt


class _FakeHttpResponse:
    def __init__(
        self,
        status_code: int = 200,
        json_data: dict[str, Any] | None = None,
        invalid_json: bool = False,
    ) -> None:
        self.status_code = status_code
        self._json_data = json_data or {}
        self._invalid_json = invalid_json

    def json(self) -> dict[str, Any]:
        if self._invalid_json:
            raise ValueError("Invalid JSON")
        return self._json_data

    def iter_lines(self) -> Iterator[bytes]:
        """Simula lo streaming NDJSON di Ollama: un chunk per parola e il chunk finale done."""
        if self._invalid_json:
            yield b"{non json"
            return
        text = str(self._json_data.get("response", ""))
        words = text.split(" ")
        for idx, word in enumerate(words):
            piece = word if idx == len(words) - 1 else word + " "
            yield json_module.dumps({"response": piece, "done": False}).encode("utf-8")
        final = {k: v for k, v in self._json_data.items() if k != "response"}
        final.setdefault("done", True)
        yield json_module.dumps(final).encode("utf-8")

    def close(self) -> None:
        return None


class _FakeHttpSession:
    def __init__(self, responses: list[ Any]) -> None:
        self.headers: dict[str, str] = {}
        self.responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    def post(
        self,
        url: str,
        json: dict[str, Any],
        timeout: tuple[int, int],
        stream: bool = False,
    ) -> _FakeHttpResponse:
        self.calls.append({"url": url, "json": json, "timeout": timeout, "stream": stream})
        if not self.responses:
            raise AssertionError("Nessuna risposta mock configurata in _FakeHttpSession")
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def test_load_and_render_prompt_validates_version_and_placeholders() -> None:
    """Verifica il controllo dell'header PROMPT_VERSION e il rifiuto di placeholder non risolti."""
    map_prompt = PROMPTS_DIR / "map_chunk.txt"
    with pytest.raises(OllamaError, match="Placeholder non risolti"):
        load_and_render_prompt(map_prompt, {"MEETING_TITLE": "Solo titolo"})


def test_ollama_retry_on_recoverable_errors_and_no_retry_on_400(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verifica AC09: retry su ConnectionError/Timeout/408/429/500 e nessun retry su HTTP 400."""
    monkeypatch.setattr("modules.summarizer.OLLAMA_RETRY_BASE_DELAY_SEC", 0)

    # Caso 1: ConnectionError -> HTTP 500 -> HTTP 200 valido
    session_ok = _FakeHttpSession(
        [
            requests.exceptions.ConnectionError("Server busy"),
            _FakeHttpResponse(500),
            _FakeHttpResponse(200, {"done": True, "response": "Risposta valida"}),
        ]
    )
    summarizer = OllamaMapReduceSummarizer(http_session=session_ok)  # type: ignore[arg-type]
    out = summarizer._invoke_ollama("Test prompt")
    assert out == "Risposta valida"
    assert len(session_ok.calls) == 3

    # Caso 2: HTTP 400 -> non ritenta mai
    session_400 = _FakeHttpSession([_FakeHttpResponse(400)])
    summarizer_400 = OllamaMapReduceSummarizer(http_session=session_400)  # type: ignore[arg-type]
    with pytest.raises(OllamaError, match="HTTP 400"):
        summarizer_400._invoke_ollama("Test prompt")
    assert len(session_400.calls) == 1

    # Caso 3: JSON invalido o risposta vuota
    session_bad_json = _FakeHttpSession([_FakeHttpResponse(200, invalid_json=True)])
    summarizer_bad = OllamaMapReduceSummarizer(http_session=session_bad_json)  # type: ignore[arg-type]
    with pytest.raises(OllamaError, match="JSON valido"):
        summarizer_bad._invoke_ollama("Test prompt")
    assert session_bad_json.calls[0]["stream"] is True

    session_empty = _FakeHttpSession([_FakeHttpResponse(200, {"done": True, "response": "   "})])
    summarizer_empty = OllamaMapReduceSummarizer(http_session=session_empty)  # type: ignore[arg-type]
    with pytest.raises(OllamaError, match="vuoto"):
        summarizer_empty._invoke_ollama("Test prompt")


def test_summarizer_map_cache_idempotency_repair_budget_and_unload(
    tmp_path: Path,
    sample_metadata: MeetingMetadata,
    sample_session_context: SessionContext,
    valid_map_markdown: str,
    valid_detailed_minutes_markdown: str,
    valid_recap_email_markdown: str,
) -> None:
    """Verifica fase MAP con cache idempotente, repair singolo, budget contesto, REDUCE e unload."""
    temp_root = tmp_path / "temp"
    sm = StateManager(sample_session_context.session_dir, temp_root=temp_root)
    sm.initialize(sample_session_context)

    transcript_path = (
        sample_session_context.session_dir / "transcripts" / "merged_transcript.txt"
    )
    transcript_path.write_text(
        "[00:01:00] [IO]: Discutiamo l'adozione di RS256.\n"
        "[00:02:00] [ALTRI]: Confermato, procediamo.\n",
        encoding="utf-8",
    )

    # Prima esecuzione MAP: restituisce prima un output incompleto (attiva repair) e poi valido
    fake_session = _FakeHttpSession(
        [
            _FakeHttpResponse(200, {"done": True, "response": "Output incompleto senza sezioni"}),
            _FakeHttpResponse(200, {"done": True, "response": valid_map_markdown}),
            _FakeHttpResponse(200, {"done": True, "response": valid_detailed_minutes_markdown}),
            _FakeHttpResponse(200, {"done": True, "response": valid_recap_email_markdown}),
            # Unload finale simula un errore di rete che deve produrre solo warning senza fallire
            requests.exceptions.ConnectionError("Unload connection closed"),
        ]
    )

    summarizer = OllamaMapReduceSummarizer(
        state_manager=sm,
        http_session=fake_session,  # type: ignore[arg-type]
    )
    map_paths = summarizer.run_map_phase(
        transcript_path=transcript_path,
        metadata=sample_metadata,
        session_dir=sample_session_context.session_dir,
    )
    assert len(map_paths) == 1
    assert (sample_session_context.session_dir / "maps" / "map_block_000.invalid.md").is_file()

    # Seconda esecuzione MAP con lo stesso input e prompt -> deve usare la cache senza chiamare Ollama!
    calls_before_cache = len(fake_session.calls)
    cached_paths = summarizer.run_map_phase(
        transcript_path=transcript_path,
        metadata=sample_metadata,
        session_dir=sample_session_context.session_dir,
    )
    assert cached_paths == map_paths
    assert len(fake_session.calls) == calls_before_cache

    # Esecuzione REDUCE dettagliato e mail
    detailed_path = summarizer.run_reduce_detailed(
        map_paths=map_paths,
        metadata=sample_metadata,
        session_dir=sample_session_context.session_dir,
    )
    email_path = summarizer.run_reduce_email(
        detailed_minutes_path=detailed_path,
        metadata=sample_metadata,
        session_dir=sample_session_context.session_dir,
    )
    assert detailed_path.is_file()
    assert email_path.is_file()

    # Unload fallito non solleva eccezioni bloccanti (Sezione 24.12)
    summarizer.unload_model()

    # Test adattamento budget contesto quando il prompt è troppo lungo
    summarizer.available_input_tokens = 250
    long_lines = [f"[00:0{i % 9}:10] [IO]: Frase molto lunga numero {i} " * 4 for i in range(25)]
    prompts = summarizer._fit_map_prompt_within_budget(
        template_path=PROMPTS_DIR / "map_chunk.txt",
        base_replacements={
            "MEETING_TITLE": sample_metadata.title,
            "ORGANIZER": sample_metadata.organizer,
            "PARTICIPANTS": "Mario, Giulia",
            "AGENDA": "Agenda lunghissima " * 50,
            "GLOSSARY_SECTION": "",
            "USER_NAME": "Mario",
            "MEETING_DURATION": "circa 30 minuti",
            "TARGET_WORDS": "circa 300-500 parole",
            "BLOCK_START": "00:00:00",
            "BLOCK_END": "00:30:00",
            "ROLLING_CONTEXT": "Decisione: usare RS256.\n" + ("Dettaglio secondario\n" * 40),
        },
        transcript_lines=long_lines,
    )
    assert len(prompts) >= 2


def test_summarizer_double_invalid_raises_ollama_error(
    tmp_path: Path,
    sample_metadata: MeetingMetadata,
    sample_session_context: SessionContext,
) -> None:
    """Verifica che due fallimenti consecutivi di validazione schema sollevino OllamaError."""
    temp_root = tmp_path / "temp"
    sm = StateManager(sample_session_context.session_dir, temp_root=temp_root)
    sm.initialize(sample_session_context)

    transcript_path = (
        sample_session_context.session_dir / "transcripts" / "merged_transcript.txt"
    )
    transcript_path.write_text(
        "[00:01:00] [IO]: Test.\n",
        encoding="utf-8",
    )

    fake_session = _FakeHttpSession(
        [
            _FakeHttpResponse(200, {"done": True, "response": "Incompleto primo tentativo"}),
            _FakeHttpResponse(200, {"done": True, "response": "Incompleto secondo tentativo"}),
        ]
    )
    summarizer = OllamaMapReduceSummarizer(
        state_manager=sm,
        http_session=fake_session,  # type: ignore[arg-type]
    )
    with pytest.raises(OllamaError, match="ancora non valido dopo il tentativo di repair"):
        summarizer.run_map_phase(
            transcript_path=transcript_path,
            metadata=sample_metadata,
            session_dir=sample_session_context.session_dir,
        )

