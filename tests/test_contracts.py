from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest

from modules.contracts import (
    ALLOWED_STAGE_TRANSITIONS,
    InvalidStageTransitionError,
    MeetingMetadata,
    SessionContext,
    SessionMode,
    Stage,
    StateValidationError,
    TrackLabel,
    UiCommandType,
    atomic_write_json,
    atomic_write_text,
    generate_session_id,
    meeting_metadata_from_dict,
    meeting_metadata_to_dict,
    now_tz,
    session_context_from_dict,
    session_context_to_dict,
    sha256_file,
    sha256_text,
    validate_stage_transition,
)


def test_session_context_roundtrip(sample_session_context: SessionContext) -> None:
    """Verifica serializzazione e deserializzazione completa di SessionContext."""
    sample_session_context.recorded_chunks = [0, 1]
    sample_session_context.normalized_chunks = [0, 1]
    sample_session_context.transcribed_chunks = [0]
    sample_session_context.audio_deleted_chunks = [0]
    sample_session_context.mapped_blocks = [0]
    sample_session_context.detailed_reduce_completed = True
    sample_session_context.email_reduce_completed = False
    sample_session_context.final_docx_path = (
        sample_session_context.session_dir.parent / "output" / "Recap.docx"
    ).resolve()
    sample_session_context.errors = [{"stage": "recording", "message": "warning"}]
    sample_session_context.discontinuities = [{"track": "IO", "injected_frames": 320}]

    serialized = session_context_to_dict(sample_session_context)
    restored = session_context_from_dict(serialized)

    assert restored.schema_version == sample_session_context.schema_version
    assert restored.session_id == sample_session_context.session_id
    assert restored.session_dir == sample_session_context.session_dir
    assert restored.mode == SessionMode.AUTO
    assert restored.metadata.title == sample_session_context.metadata.title
    assert restored.recorded_chunks == [0, 1]
    assert restored.transcribed_chunks == [0]
    assert restored.detailed_reduce_completed is True
    assert restored.email_reduce_completed is False
    assert restored.final_docx_path == sample_session_context.final_docx_path


def test_meeting_metadata_naive_datetime_normalization() -> None:
    """Verifica che date naive vengano convertite in timezone-aware durante la serializzazione."""
    naive_dt = datetime(2026, 10, 23, 9, 30, 0)
    meta = MeetingMetadata(
        title="Sync Mattutino",
        scheduled_start=naive_dt,
        scheduled_end=None,
        source="outlook",
    )
    payload = meeting_metadata_to_dict(meta)
    restored = meeting_metadata_from_dict(payload)
    assert restored.scheduled_start is not None
    assert restored.scheduled_start.tzinfo is not None
    assert restored.scheduled_end is None


def test_session_context_rejects_missing_or_invalid_schema(
    sample_session_context: SessionContext,
) -> None:
    """Verifica il rifiuto di schema_version mancante, non intero o incompatibile."""
    payload = session_context_to_dict(sample_session_context)
    del payload["schema_version"]
    with pytest.raises(StateValidationError, match="schema_version"):
        session_context_from_dict(payload)

    payload_wrong = session_context_to_dict(sample_session_context)
    payload_wrong["schema_version"] = 999
    with pytest.raises(StateValidationError, match="incompatibile"):
        session_context_from_dict(payload_wrong)

    payload_bool = session_context_to_dict(sample_session_context)
    payload_bool["schema_version"] = True
    with pytest.raises(StateValidationError, match="intero"):
        session_context_from_dict(payload_bool)


def test_session_context_rejects_missing_required_fields(
    sample_session_context: SessionContext,
) -> None:
    """Verifica che la mancanza di qualunque campo obbligatorio sollevi StateValidationError."""
    required_keys = [
        "session_id",
        "session_dir",
        "mode",
        "metadata",
        "started_at",
        "stage",
        "recorded_chunks",
        "normalized_chunks",
        "transcribed_chunks",
        "audio_deleted_chunks",
        "mapped_blocks",
        "detailed_reduce_completed",
        "email_reduce_completed",
        "final_docx_path",
        "final_docx_verified",
        "errors",
        "discontinuities",
        "last_updated_at",
    ]
    for key in required_keys:
        payload = session_context_to_dict(sample_session_context)
        del payload[key]
        with pytest.raises(StateValidationError):
            session_context_from_dict(payload)


def test_session_context_rejects_relative_paths(
    sample_session_context: SessionContext,
) -> None:
    """Verifica che session_dir e final_docx_path debbano essere percorsi assoluti."""
    payload = session_context_to_dict(sample_session_context)
    payload["session_dir"] = "relative/path/session"
    with pytest.raises(StateValidationError, match="assoluto"):
        session_context_from_dict(payload)

    payload2 = session_context_to_dict(sample_session_context)
    payload2["final_docx_path"] = "output/relative.docx"
    with pytest.raises(StateValidationError, match="assoluto"):
        session_context_from_dict(payload2)


def test_stage_transitions_validation() -> None:
    """Verifica le transizioni di Stage consentite e non consentite (Sezione 13)."""
    for source, targets in ALLOWED_STAGE_TRANSITIONS.items():
        validate_stage_transition(source, source)
        for target in targets:
            validate_stage_transition(source, target)

    with pytest.raises(InvalidStageTransitionError):
        validate_stage_transition(Stage.IDLE, Stage.BUILDING_DOCX)

    with pytest.raises(InvalidStageTransitionError):
        validate_stage_transition(Stage.SHUTTING_DOWN, Stage.IDLE)


def test_atomic_write_and_sha256_utilities(tmp_path: Path) -> None:
    """Verifica atomic_write_text, atomic_write_json, sha256_file e sha256_text."""
    txt_file = tmp_path / "subdir" / "sample.txt"
    atomic_write_text(txt_file, "Contenuto UTF-8: àèìòù\n")
    assert txt_file.read_text(encoding="utf-8") == "Contenuto UTF-8: àèìòù\n"
    assert sha256_file(txt_file) == sha256_text("Contenuto UTF-8: àèìòù\n")

    json_file = tmp_path / "subdir" / "data.json"
    atomic_write_json(json_file, {"key": "valore", "count": 3})
    assert '"count": 3' in json_file.read_text(encoding="utf-8")

    sid = generate_session_id(now_tz())
    assert len(sid.split("_")) == 3
    assert TrackLabel.IO.value == "IO"
    assert UiCommandType.EXIT.value == "exit"
