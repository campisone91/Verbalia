from __future__ import annotations

import ast
from pathlib import Path

import pytest

import main as main_module
from config import (
    EXIT_APP_ERROR,
    EXIT_INVALID_ARGS,
    EXIT_MISSING_DEPENDENCY,
    EXIT_SUCCESS,
    EXIT_UNRECOVERABLE_SESSION,
    PROJECT_ROOT,
)
from modules.audio_utils import create_silent_wav
from modules.contracts import (
    MeetingMetadata,
    SessionContext,
    Stage,
    atomic_write_text,
    sha256_file,
)
from modules.orchestrator import (
    DRY_RUN_MARKER,
    MeetingScribeApplication,
    verify_or_request_user_consent,
)
from modules.state_manager import StateManager


def test_main_executes_bootstrap_before_third_party_imports() -> None:
    """Verifica AC02: main.py importa ed esegue ensure_environment() prima di ogni altro import."""
    main_source = (PROJECT_ROOT / "main.py").read_text(encoding="utf-8")
    tree = ast.parse(main_source)

    non_future_stmts = [
        stmt
        for stmt in tree.body
        if not (isinstance(stmt, ast.ImportFrom) and stmt.module == "__future__")
    ]
    assert len(non_future_stmts) >= 2
    first_stmt = non_future_stmts[0]
    second_stmt = non_future_stmts[1]

    assert isinstance(first_stmt, ast.ImportFrom)
    assert first_stmt.module == "bootstrap"
    assert isinstance(second_stmt, ast.Expr)
    assert isinstance(second_stmt.value, ast.Call)
    assert isinstance(second_stmt.value.func, ast.Name)
    assert second_stmt.value.func.id == "ensure_environment"


def test_dry_run_end_to_end_creates_valid_docx_and_cleans_temp(tmp_path: Path) -> None:
    """Verifica Sezione 31 (AC11, AC12, AC14): --dry-run completa la pipeline senza componenti reali."""
    temp_dir = tmp_path / "temp"
    output_dir = tmp_path / "output"
    app = MeetingScribeApplication(
        headless=True,
        output_dir=output_dir,
        temp_dir=temp_dir,
    )
    exit_code = app.run_dry_run()
    assert exit_code == EXIT_SUCCESS

    docx_files = list(output_dir.rglob("*.docx"))
    assert len(docx_files) == 1
    assert DRY_RUN_MARKER.replace(" ", "_") in docx_files[0].name or "Dry Run" in docx_files[0].name
    # La cartella di sessione temporanea deve essere stata rimossa dal cleanup
    assert list(temp_dir.glob("session_*")) == []


def test_test_pipeline_preserves_original_audio_and_cleans_up(
    tmp_path: Path,
    valid_map_markdown: str,
    valid_detailed_minutes_markdown: str,
    valid_recap_email_markdown: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verifica Sezione 30 e AC23: --test-pipeline preserva intatto il file audio di input."""
    input_wav = create_silent_wav(tmp_path / "external_input.wav", duration_sec=1.5)
    original_hash = sha256_file(input_wav)

    temp_dir = tmp_path / "temp"
    output_dir = tmp_path / "output"
    app = MeetingScribeApplication(
        headless=True,
        output_dir=output_dir,
        temp_dir=temp_dir,
    )

    def _fake_normalize(
        _ffmpeg: Path,
        raw_wav: Path,
        norm_wav: Path,
        *,
        delete_raw_on_success: bool = True,
        timeout_sec: int = 300,
    ) -> Path:
        create_silent_wav(norm_wav, duration_sec=1.0)
        if delete_raw_on_success and raw_wav.exists():
            raw_wav.unlink()
        return norm_wav

    monkeypatch.setattr("modules.orchestrator.normalize_wav_with_ffmpeg", _fake_normalize)
    monkeypatch.setattr("modules.processing.normalize_wav_with_ffmpeg", _fake_normalize)

    class _FakeWhisperModel:
        def transcribe(self, *_args: object, **_kwargs: object) -> tuple[list[object], dict[str, str]]:
            class _Seg:
                start = 0.2
                end = 0.8
                text = "Test pipeline speech."
                avg_logprob = -0.1

            return ([_Seg()], {"language": "it"})

    monkeypatch.setattr(
        "modules.transcriber._default_whisper_model_factory",
        lambda *_a, **_kw: _FakeWhisperModel(),
    )

    class _FakeSummarizer:
        def run_map_phase(
            self,
            transcript_path: Path,
            metadata: MeetingMetadata,
            session_dir: Path,
        ) -> list[Path]:
            _ = (transcript_path, metadata)
            p = session_dir / "maps" / "map_block_000.md"
            atomic_write_text(p, valid_map_markdown)
            return [p]

        def run_reduce_detailed(
            self,
            map_paths: object,
            metadata: MeetingMetadata,
            session_dir: Path,
        ) -> Path:
            _ = (map_paths, metadata)
            p = session_dir / "reduce" / "detailed_minutes.md"
            atomic_write_text(p, valid_detailed_minutes_markdown)
            return p

        def run_reduce_email(
            self,
            detailed_minutes_path: Path,
            metadata: MeetingMetadata,
            session_dir: Path,
        ) -> Path:
            _ = (detailed_minutes_path, metadata)
            p = session_dir / "reduce" / "recap_email.md"
            atomic_write_text(p, valid_recap_email_markdown)
            return p

        def unload_model(self) -> None:
            return None

    app.summarizer = _FakeSummarizer()  # type: ignore[assignment]
    rc = app.run_test_pipeline(input_wav)
    assert rc == EXIT_SUCCESS
    assert input_wav.exists()
    assert sha256_file(input_wav) == original_hash
    assert len(list(output_dir.rglob("*.docx"))) == 1


def test_pipeline_error_prevents_cleanup_and_resume_completes_session(
    tmp_path: Path,
    sample_session_context: SessionContext,
    valid_map_markdown: str,
    valid_detailed_minutes_markdown: str,
    valid_recap_email_markdown: str,
) -> None:
    """Verifica AC06 e AC13: un errore blocca il cleanup, e --resume completa senza ricalcolare i chunk validi."""
    temp_dir = tmp_path / "temp"
    output_dir = tmp_path / "output"
    sm = StateManager(
        sample_session_context.session_dir,
        temp_root=temp_dir,
        output_root=output_dir,
    )
    sm.initialize(sample_session_context)

    # Prepariamo un transcript già valido su disco per il chunk 0
    chunk0_txt = sample_session_context.session_dir / "transcripts" / "chunk_000.txt"
    atomic_write_text(
        chunk0_txt,
        "# Chunk 000\n"
        "# Intervallo: 00:00:00 - 00:15:00\n"
        f"# Sessione: {sample_session_context.session_id}\n\n"
        "[00:00:10] [IO]: Abbiamo completato la trascrizione prima del crash.\n",
    )

    def _mark_transcribed(c: SessionContext) -> None:
        c.recorded_chunks = [0]
        c.normalized_chunks = [0]
        c.transcribed_chunks = [0]
        c.audio_deleted_chunks = [0]
        c.stage = Stage.SUMMARIZING_MAP

    sm.mutate(_mark_transcribed)

    app = MeetingScribeApplication(
        headless=True,
        output_dir=output_dir,
        temp_dir=temp_dir,
    )

    # 1. Simuliamo un fallimento di Ollama durante il resume -> la sessione NON deve essere cancellata
    class _FailingSummarizer:
        def run_map_phase(self, *_args: object, **_kwargs: object) -> list[Path]:
            raise RuntimeError("Ollama crash simulato")

    app.summarizer = _FailingSummarizer()  # type: ignore[assignment]
    rc_fail = app.run_resume(sample_session_context.session_dir)
    assert rc_fail == EXIT_APP_ERROR
    assert sample_session_context.session_dir.exists()
    assert sm.load().stage == Stage.ERROR

    # 2. Riprendiamo la sessione con Summarizer funzionante -> completa e pulisce
    class _WorkingSummarizer:
        def run_map_phase(
            self,
            transcript_path: Path,
            metadata: MeetingMetadata,
            session_dir: Path,
        ) -> list[Path]:
            _ = (transcript_path, metadata)
            p = session_dir / "maps" / "map_block_000.md"
            atomic_write_text(p, valid_map_markdown)
            return [p]

        def run_reduce_detailed(
            self,
            map_paths: object,
            metadata: MeetingMetadata,
            session_dir: Path,
        ) -> Path:
            _ = (map_paths, metadata)
            p = session_dir / "reduce" / "detailed_minutes.md"
            atomic_write_text(p, valid_detailed_minutes_markdown)
            return p

        def run_reduce_email(
            self,
            detailed_minutes_path: Path,
            metadata: MeetingMetadata,
            session_dir: Path,
        ) -> Path:
            _ = (detailed_minutes_path, metadata)
            p = session_dir / "reduce" / "recap_email.md"
            atomic_write_text(p, valid_recap_email_markdown)
            return p

        def unload_model(self) -> None:
            return None

    app.summarizer = _WorkingSummarizer()  # type: ignore[assignment]
    rc_ok = app.run_resume(sample_session_context.session_dir)
    assert rc_ok == EXIT_SUCCESS
    assert not sample_session_context.session_dir.exists()
    assert len(list(output_dir.rglob("*.docx"))) == 1


def test_diagnostics_report_and_resume_from_raw_audio(
    tmp_path: Path,
    sample_session_context: SessionContext,
    valid_map_markdown: str,
    valid_detailed_minutes_markdown: str,
    valid_recap_email_markdown: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verifica --diagnostics e --resume a partire da chunk audio raw non ancora normalizzati."""
    temp_dir = tmp_path / "temp"
    output_dir = tmp_path / "output"
    app = MeetingScribeApplication(
        headless=True,
        output_dir=output_dir,
        temp_dir=temp_dir,
    )

    report = app.collect_diagnostics_report()
    assert "python_version" in report
    assert "free_disk_gb" in report
    rc_diag = app.run_diagnostics()
    assert rc_diag in (EXIT_SUCCESS, EXIT_MISSING_DEPENDENCY, EXIT_APP_ERROR)

    # Prepariamo una sessione interrotta in FINALIZING_AUDIO con soli file raw
    sm = StateManager(
        sample_session_context.session_dir,
        temp_root=temp_dir,
        output_root=output_dir,
    )
    sm.initialize(sample_session_context)
    audio_dir = sample_session_context.session_dir / "audio"
    create_silent_wav(audio_dir / "chunk_000_io_raw.wav", duration_sec=0.5)
    create_silent_wav(audio_dir / "chunk_000_altri_raw.wav", duration_sec=0.5)

    def _mark_raw(c: SessionContext) -> None:
        c.recorded_chunks = [0]
        c.stage = Stage.FINALIZING_AUDIO

    sm.mutate(_mark_raw)

    def _fake_normalize(
        _ffmpeg: Path,
        raw_wav: Path,
        norm_wav: Path,
        *,
        delete_raw_on_success: bool = True,
        timeout_sec: int = 300,
    ) -> Path:
        _ = timeout_sec
        create_silent_wav(norm_wav, duration_sec=0.5)
        if delete_raw_on_success and raw_wav.exists():
            raw_wav.unlink()
        return norm_wav

    monkeypatch.setattr("modules.orchestrator.normalize_wav_with_ffmpeg", _fake_normalize)
    monkeypatch.setattr("modules.processing.normalize_wav_with_ffmpeg", _fake_normalize)

    class _FakeWhisperModel:
        def transcribe(self, *_args: object, **_kwargs: object) -> tuple[list[object], dict[str, str]]:
            class _Seg:
                start = 0.1
                end = 0.4
                text = "Ripristino da audio raw completato."
                avg_logprob = -0.1

            return ([_Seg()], {"language": "it"})

    monkeypatch.setattr(
        "modules.transcriber._default_whisper_model_factory",
        lambda *_a, **_kw: _FakeWhisperModel(),
    )

    class _WorkingSummarizer:
        def run_map_phase(
            self,
            transcript_path: Path,
            metadata: MeetingMetadata,
            session_dir: Path,
        ) -> list[Path]:
            _ = (transcript_path, metadata)
            p = session_dir / "maps" / "map_block_000.md"
            atomic_write_text(p, valid_map_markdown)
            return [p]

        def run_reduce_detailed(
            self,
            map_paths: object,
            metadata: MeetingMetadata,
            session_dir: Path,
        ) -> Path:
            _ = (map_paths, metadata)
            p = session_dir / "reduce" / "detailed_minutes.md"
            atomic_write_text(p, valid_detailed_minutes_markdown)
            return p

        def run_reduce_email(
            self,
            detailed_minutes_path: Path,
            metadata: MeetingMetadata,
            session_dir: Path,
        ) -> Path:
            _ = (detailed_minutes_path, metadata)
            p = session_dir / "reduce" / "recap_email.md"
            atomic_write_text(p, valid_recap_email_markdown)
            return p

        def unload_model(self) -> None:
            return None

    app.summarizer = _WorkingSummarizer()  # type: ignore[assignment]
    rc_resume_raw = app.run_resume(sample_session_context.session_dir)
    assert rc_resume_raw == EXIT_SUCCESS
    assert len(list(output_dir.rglob("*.docx"))) == 1




def test_cli_validation_and_user_consent(tmp_path: Path) -> None:
    """Verifica gli exit code della CLI e la persistenza del consenso privacy locale."""
    assert main_module.main(["--test-pipeline", str(tmp_path / "non_esiste.wav")]) == EXIT_INVALID_ARGS
    assert main_module.main(["--test-pipeline", str(tmp_path)]) == EXIT_INVALID_ARGS
    assert main_module.main(["--resume", str(tmp_path / "non_esiste_dir")]) == EXIT_INVALID_ARGS

    empty_session = tmp_path / "temp" / "session_empty"
    empty_session.mkdir(parents=True, exist_ok=True)
    app = MeetingScribeApplication(
        headless=True,
        output_dir=tmp_path / "output",
        temp_dir=tmp_path / "temp",
    )
    assert app.run_resume(empty_session) == EXIT_UNRECOVERABLE_SESSION

    consent_file = tmp_path / ".user_consent.json"
    assert verify_or_request_user_consent(
        consent_file,
        require_confirmation=True,
        prompt_callback=lambda _msg: False,
    ) is False
    assert not consent_file.exists()

    assert verify_or_request_user_consent(
        consent_file,
        require_confirmation=True,
        prompt_callback=lambda _msg: True,
    ) is True
    assert consent_file.is_file()


def test_consecutive_sessions_isolation_without_state_leak(
    tmp_path: Path,
    valid_map_markdown: str,
    valid_detailed_minutes_markdown: str,
    valid_recap_email_markdown: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verifica che sessioni consecutive sulla stessa istanza app non condividano StateManager obsoleto."""
    temp_dir = tmp_path / "temp"
    output_dir = tmp_path / "output"
    app = MeetingScribeApplication(
        headless=True,
        output_dir=output_dir,
        temp_dir=temp_dir,
    )

    def _fake_normalize(
        _ffmpeg: Path,
        raw_wav: Path,
        norm_wav: Path,
        *,
        delete_raw_on_success: bool = True,
        timeout_sec: int = 300,
    ) -> Path:
        create_silent_wav(norm_wav, duration_sec=1.0)
        if delete_raw_on_success and raw_wav.exists():
            raw_wav.unlink()
        return norm_wav

    monkeypatch.setattr("modules.orchestrator.normalize_wav_with_ffmpeg", _fake_normalize)
    monkeypatch.setattr("modules.processing.normalize_wav_with_ffmpeg", _fake_normalize)

    class _FakeWhisperModel:
        def transcribe(self, *_args: object, **_kwargs: object) -> tuple[list[object], dict[str, str]]:
            class _Seg:
                start = 0.2
                end = 0.8
                text = "Test speech segment."
                avg_logprob = -0.1

            return ([_Seg()], {"language": "it"})

    monkeypatch.setattr(
        "modules.transcriber._default_whisper_model_factory",
        lambda *_a, **_kw: _FakeWhisperModel(),
    )

    def _fake_invoke_ollama(self_summarizer: object, prompt: str, **_kw: object) -> str:
        prompt_lower = prompt.lower()
        if "assistente esecutivo" in prompt_lower or "bozza di mail" in prompt_lower or "outlook" in prompt_lower:
            return valid_recap_email_markdown
        if "executive summary" in prompt_lower:
            return valid_detailed_minutes_markdown
        return valid_map_markdown

    monkeypatch.setattr(
        "modules.summarizer.OllamaMapReduceSummarizer._invoke_ollama",
        _fake_invoke_ollama,
    )

    # Sessione 1
    input_wav_1 = create_silent_wav(tmp_path / "input1.wav", duration_sec=1.5)
    rc1 = app.run_test_pipeline(input_wav_1)
    assert rc1 == EXIT_SUCCESS
    assert len(list(output_dir.rglob("*.docx"))) == 1
    assert list(temp_dir.glob("session_*")) == []

    # Ripristiniamo l'evento di shutdown per simulare la permanenza del daemon come in run_auto
    app.shutdown_event.clear()

    # Sessione 2 sulla STESSA istanza applicativa: non deve fallire con StateValidationError
    input_wav_2 = create_silent_wav(tmp_path / "input2.wav", duration_sec=1.5)
    rc2 = app.run_test_pipeline(input_wav_2)
    assert rc2 == EXIT_SUCCESS
    assert len(list(output_dir.rglob("*.docx"))) == 2
    assert list(temp_dir.glob("session_*")) == []

