from __future__ import annotations

import threading
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from modules.contracts import (
    MeetingMetadata,
    ScribeError,
    SessionContext,
    Stage,
    StateValidationError,
    atomic_write_json,
    atomic_write_text,
    sha256_text,
)
from modules.docx_builder import MeetingDocxBuilder
from modules.state_manager import StateManager, ensure_safe_child_path


def test_state_manager_initialize_load_and_concurrent_mutate(
    tmp_path: Path,
    sample_session_context: SessionContext,
) -> None:
    """Verifica inizializzazione atomica, load e mutazioni concorrenti thread-safe."""
    temp_root = tmp_path / "temp"
    output_root = tmp_path / "output"
    sm = StateManager(
        sample_session_context.session_dir,
        temp_root=temp_root,
        output_root=output_root,
    )
    sm.initialize(sample_session_context)

    assert (sample_session_context.session_dir / "state.json").is_file()
    assert (sample_session_context.session_dir / "metadata" / "meeting.json").is_file()

    def _worker(chunk_idx: int) -> None:
        def _cb(ctx: SessionContext) -> None:
            ctx.recorded_chunks.append(chunk_idx)
            ctx.recorded_chunks.sort()

        sm.mutate(_cb)

    threads = [threading.Thread(target=_worker, args=(i,)) for i in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    loaded = sm.load()
    assert loaded.recorded_chunks == list(range(10))


def test_state_manager_corrupt_json_and_record_error(
    tmp_path: Path,
    sample_session_context: SessionContext,
) -> None:
    """Verifica rilevamento JSON corrotto e registrazione strutturata degli errori."""
    temp_root = tmp_path / "temp"
    sm = StateManager(sample_session_context.session_dir, temp_root=temp_root)
    sm.initialize(sample_session_context)

    sm.record_error(Stage.RECORDING, RuntimeError("Dispositivo disconnesso"))
    ctx = sm.load()
    assert ctx.stage == Stage.ERROR
    assert len(ctx.errors) == 1
    assert ctx.errors[0]["type"] == "RuntimeError"

    sm.state_path.write_text("{json_corrotto", encoding="utf-8")
    with pytest.raises(StateValidationError, match="corrotto"):
        sm.load()


def test_ensure_safe_child_path_rejects_traversal_and_external(tmp_path: Path) -> None:
    """Verifica la protezione contro path traversal ('..') e percorsi esterni a TEMP_DIR."""
    base = tmp_path / "temp"
    base.mkdir(parents=True, exist_ok=True)
    outside = tmp_path / "outside"
    outside.mkdir(parents=True, exist_ok=True)

    with pytest.raises(StateValidationError, match="traversal"):
        ensure_safe_child_path(base, Path("../outside"))

    with pytest.raises(StateValidationError, match="esterno"):
        ensure_safe_child_path(base, outside)


def test_validate_resume_artifacts_reconciles_missing_files_and_hash_mismatch(
    tmp_path: Path,
    sample_session_context: SessionContext,
) -> None:
    """Verifica che validate_resume_artifacts retroceda lo stage se mancano file o gli hash non coincidono."""
    temp_root = tmp_path / "temp"
    sm = StateManager(sample_session_context.session_dir, temp_root=temp_root)
    sm.initialize(sample_session_context)

    # Caso 1: lo state dichiara chunk 0 trascritto ma il file non esiste -> retrocede
    def _declare_fake_progress(ctx: SessionContext) -> None:
        ctx.recorded_chunks = [0]
        ctx.normalized_chunks = [0]
        ctx.transcribed_chunks = [0]
        ctx.mapped_blocks = [0]
        ctx.stage = Stage.SUMMARIZING_REDUCE

    sm.mutate(_declare_fake_progress)
    reconciled = sm.validate_resume_artifacts()
    assert reconciled.transcribed_chunks == []
    assert reconciled.mapped_blocks == []
    assert reconciled.stage == Stage.FINALIZING_AUDIO

    # Caso 2: creiamo un transcript valido per chunk 0 e un blocco MAP con hash errato
    transcript_file = (
        sample_session_context.session_dir / "transcripts" / "chunk_000.txt"
    )
    atomic_write_text(
        transcript_file,
        "# Chunk 000\n# Intervallo: 00:00:00 - 00:15:00\n# Sessione: s1\n\n[00:00:05] [IO]: Test.\n",
    )
    map_md = sample_session_context.session_dir / "maps" / "map_block_000.md"
    map_meta = sample_session_context.session_dir / "maps" / "map_block_000.meta.json"
    atomic_write_text(map_md, "## Contenuto MAP modificato\n")
    atomic_write_json(
        map_meta,
        {
            "block_index": 0,
            "input_sha256": sha256_text("in"),
            "prompt_sha256": sha256_text("pr"),
            "model": "llama3.1:8b",
            "output_sha256": "0" * 64,  # hash non coincidente
            "completed_at": "2026-10-23T14:30:00+02:00",
        },
    )

    sm.mutate(_declare_fake_progress)
    reconciled2 = sm.validate_resume_artifacts()
    assert reconciled2.transcribed_chunks == [0]
    assert reconciled2.mapped_blocks == []
    assert reconciled2.stage == Stage.SUMMARIZING_MAP
    assert map_md.exists()  # I file invalidi non vengono cancellati durante la riconciliazione


def test_cleanup_session_files_requires_valid_docx_and_handles_rmtree_error(
    tmp_path: Path,
    fixed_now: datetime,
    sample_metadata: MeetingMetadata,
    sample_session_context: SessionContext,
    valid_recap_email_markdown: str,
    valid_detailed_minutes_markdown: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verifica che il cleanup rifiuti DOCX invalidi/interni e gestisca errori rmtree con exit code 6."""
    temp_root = tmp_path / "temp"
    output_root = tmp_path / "output"
    output_root.mkdir(parents=True, exist_ok=True)

    sm = StateManager(
        sample_session_context.session_dir,
        temp_root=temp_root,
        output_root=output_root,
    )
    sm.initialize(sample_session_context)

    # Cleanup con DOCX inesistente -> fallisce e preserva session_dir
    with pytest.raises(StateValidationError):
        sm.cleanup_session_files(output_root / "missing.docx")
    assert sample_session_context.session_dir.exists()

    # Generiamo un vero DOCX valido in output_root
    recap_p = sample_session_context.session_dir / "reduce" / "recap_email.md"
    minutes_p = sample_session_context.session_dir / "reduce" / "detailed_minutes.md"
    recap_p.write_text(valid_recap_email_markdown, encoding="utf-8")
    minutes_p.write_text(valid_detailed_minutes_markdown, encoding="utf-8")

    docx_path = MeetingDocxBuilder().build(
        metadata=sample_metadata,
        started_at=fixed_now,
        ended_at=fixed_now + timedelta(minutes=30),
        recap_email_path=recap_p,
        detailed_minutes_path=minutes_p,
        output_dir=output_root,
    )

    # Simuliamo fallimento di shutil.rmtree -> solleva ScribeError con exit_code == 6
    def _failing_rmtree(_path: object) -> None:
        raise OSError("File bloccato da altro processo")

    monkeypatch.setattr("modules.state_manager.shutil.rmtree", _failing_rmtree)
    with pytest.raises(ScribeError) as exc_info:
        sm.cleanup_session_files(docx_path)
    assert getattr(exc_info.value, "exit_code", None) == 6
    assert sample_session_context.session_dir.exists()

    # Ripristiniamo rmtree e verifichiamo la rimozione completa della sessione
    monkeypatch.undo()
    sm.cleanup_session_files(docx_path)
    assert not sample_session_context.session_dir.exists()
    assert docx_path.exists()


def test_state_manager_helper_methods_and_reduce_validation(
    tmp_path: Path,
    fixed_now: datetime,
    sample_metadata: MeetingMetadata,
    sample_session_context: SessionContext,
    valid_map_markdown: str,
    valid_recap_email_markdown: str,
    valid_detailed_minutes_markdown: str,
) -> None:
    """Verifica i metodi helper di registrazione chunk/blocchi e la riconciliazione di REDUCE/DOCX."""
    temp_root = tmp_path / "temp"
    output_root = tmp_path / "output"
    output_root.mkdir(parents=True, exist_ok=True)
    sm = StateManager(
        sample_session_context.session_dir,
        temp_root=temp_root,
        output_root=output_root,
    )
    sm.initialize(sample_session_context)

    sm.update_stage(Stage.RECORDING)

    def _set_all_progress(c: SessionContext) -> None:
        c.recorded_chunks = [0]
        c.normalized_chunks = [0]
        c.transcribed_chunks = [0]
        c.audio_deleted_chunks = [0]
        c.mapped_blocks = [0]
        c.detailed_reduce_completed = True
        c.email_reduce_completed = True

    sm.mutate(_set_all_progress)

    # Scriviamo gli artefatti validi fino a REDUCE e DOCX
    transcript_file = sample_session_context.session_dir / "transcripts" / "chunk_000.txt"
    atomic_write_text(
        transcript_file,
        "# Chunk 000\n# Intervallo: 00:00:00 - 00:15:00\n# Sessione: s1\n\n[00:00:05] [IO]: Test.\n",
    )
    map_md = sample_session_context.session_dir / "maps" / "map_block_000.md"
    map_meta = sample_session_context.session_dir / "maps" / "map_block_000.meta.json"
    atomic_write_text(map_md, valid_map_markdown)
    atomic_write_json(
        map_meta,
        {
            "block_index": 0,
            "input_sha256": sha256_text("in"),
            "prompt_sha256": sha256_text("pr"),
            "model": "llama3.1:8b",
            "output_sha256": sha256_text(valid_map_markdown),
            "completed_at": "2026-10-23T14:30:00+02:00",
        },
    )

    recap_p = sample_session_context.session_dir / "reduce" / "recap_email.md"
    minutes_p = sample_session_context.session_dir / "reduce" / "detailed_minutes.md"
    recap_p.write_text(valid_recap_email_markdown, encoding="utf-8")
    minutes_p.write_text(valid_detailed_minutes_markdown, encoding="utf-8")

    docx_path = MeetingDocxBuilder().build(
        metadata=sample_metadata,
        started_at=fixed_now,
        ended_at=fixed_now + timedelta(minutes=30),
        recap_email_path=recap_p,
        detailed_minutes_path=minutes_p,
        output_dir=output_root,
    )

    def _set_docx(c: SessionContext) -> None:
        c.final_docx_path = docx_path.resolve()
        c.final_docx_verified = True

    sm.mutate(_set_docx)

    reconciled = sm.validate_resume_artifacts()
    assert reconciled.detailed_reduce_completed is True
    assert reconciled.email_reduce_completed is True
    assert reconciled.final_docx_path == docx_path.resolve()
    assert reconciled.stage == Stage.CLEANING_UP


