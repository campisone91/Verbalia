from __future__ import annotations

from pathlib import Path

import pytest

from modules.contracts import TrackLabel, TranscriptionError, TranscriptSegment
from modules.transcript_utils import (
    NO_SPEECH_MARKER,
    apply_custom_glossary_normalization,
    deduplicate_overlap_segments,
    estimate_tokens,
    format_chunk_transcript,
    format_timestamp_hms,
    merge_chunk_transcripts,
    normalize_text_for_comparison,
    parse_timestamp_hms,
    parse_transcript_line,
    split_segments_by_time_window,
    validate_chunk_transcript_file,
)


def test_timestamp_and_line_parsing() -> None:
    """Verifica formattazione e parsing di timestamp e righe transcript."""
    assert format_timestamp_hms(3661.2) == "01:01:01"
    assert parse_timestamp_hms("01:01:01") == 3661.0

    line = "[00:14:58] [ALTRI]: Confermiamo il rilascio della versione 2.4.1."
    seg = parse_transcript_line(line)
    assert seg is not None
    assert seg.track == TrackLabel.OTHERS
    assert seg.start_sec == 14 * 60 + 58
    assert seg.text == "Confermiamo il rilascio della versione 2.4.1."

    assert parse_transcript_line("# Header comment") is None
    assert parse_transcript_line(NO_SPEECH_MARKER) is None
    assert parse_transcript_line("Riga non valida") is None


def test_deduplicate_overlap_preserves_different_labels_and_numeric_codes() -> None:
    """Verifica AC04: l'overlap deduplica la stessa label ma non fonde label o codici numerici diversi."""
    prev_segments = [
        TranscriptSegment(
            track=TrackLabel.IO,
            start_sec=898.0,
            end_sec=900.0,
            text="Procediamo con il ticket PROJ-101.",
        ),
        TranscriptSegment(
            track=TrackLabel.OTHERS,
            start_sec=899.0,
            end_sec=900.0,
            text="Perfetto, d'accordo",
        ),
    ]
    new_segments = [
        # Stessa frase nella finestra di overlap con testo leggermente più completo -> deduplicato
        TranscriptSegment(
            track=TrackLabel.OTHERS,
            start_sec=899.0,
            end_sec=901.0,
            text="Perfetto, d'accordo su tutto.",
        ),
        # Stesso testo ma label opposta (IO vs ALTRI) -> NON deve essere fuso
        TranscriptSegment(
            track=TrackLabel.IO,
            start_sec=899.0,
            end_sec=901.0,
            text="Perfetto, d'accordo su tutto.",
        ),
        # Codice numerico diverso (PROJ-102 vs PROJ-101) -> NON deve essere considerato duplicato
        TranscriptSegment(
            track=TrackLabel.IO,
            start_sec=900.0,
            end_sec=902.0,
            text="Procediamo con il ticket PROJ-102.",
        ),
    ]

    deduped = deduplicate_overlap_segments(
        prev_segments,
        new_segments,
        boundary_sec=900.0,
    )
    texts_and_tracks = [(s.track, s.text) for s in deduped]
    assert (TrackLabel.OTHERS, "Perfetto, d'accordo su tutto.") in texts_and_tracks
    assert (TrackLabel.OTHERS, "Perfetto, d'accordo") not in texts_and_tracks
    assert (TrackLabel.IO, "Perfetto, d'accordo su tutto.") in texts_and_tracks
    assert (TrackLabel.IO, "Procediamo con il ticket PROJ-101.") in texts_and_tracks
    assert (TrackLabel.IO, "Procediamo con il ticket PROJ-102.") in texts_and_tracks


def test_format_validate_and_merge_chunk_transcripts_including_no_speech(
    tmp_path: Path,
) -> None:
    """Verifica validazione file chunk, gestione NESSUN PARLATO e merge multi-chunk."""
    c0_path = tmp_path / "chunk_000.txt"
    c1_path = tmp_path / "chunk_001.txt"

    c0_text = format_chunk_transcript(
        chunk_index=0,
        session_id="20261023_140000_a1b2c3d4",
        start_offset_sec=0.0,
        end_offset_sec=900.0,
        segments=[
            TranscriptSegment(TrackLabel.IO, 10.0, 12.0, "Inizio riunione."),
            TranscriptSegment(TrackLabel.OTHERS, 899.0, 900.0, "Frase sul confine overlap"),
        ],
    )
    c0_path.write_text(c0_text, encoding="utf-8")
    art0 = validate_chunk_transcript_file(c0_path)
    assert art0.verified is True
    assert art0.segment_count == 2

    # Chunk 1 senza parlato
    c1_empty = format_chunk_transcript(
        chunk_index=1,
        session_id="20261023_140000_a1b2c3d4",
        start_offset_sec=897.0,
        end_offset_sec=1800.0,
        segments=[],
    )
    c1_path.write_text(c1_empty, encoding="utf-8")
    art1 = validate_chunk_transcript_file(c1_path)
    assert art1.segment_count == 0

    merged_path = tmp_path / "merged_transcript.txt"
    merged_segments = merge_chunk_transcripts([c0_path, c1_path], merged_path)
    assert len(merged_segments) == 2
    assert merged_path.is_file()

    # Verifica che un file corrotto sollevi TranscriptionError
    bad_path = tmp_path / "bad_chunk.txt"
    bad_path.write_text("File senza header", encoding="utf-8")
    with pytest.raises(TranscriptionError):
        validate_chunk_transcript_file(bad_path)


def test_split_segments_by_time_window_and_glossary() -> None:
    """Verifica la suddivisione in finestre MAP da 30 minuti [0, 1800), [1800, 3600) e il glossario."""
    segments = [
        TranscriptSegment(TrackLabel.IO, 120.0, 125.0, "Primo blocco."),
        TranscriptSegment(TrackLabel.OTHERS, 1799.0, 1800.0, "Fine primo blocco."),
        TranscriptSegment(TrackLabel.IO, 1805.0, 1810.0, "Secondo blocco."),
        TranscriptSegment(TrackLabel.OTHERS, 3700.0, 3710.0, "Terzo blocco."),
    ]
    windows = split_segments_by_time_window(segments, window_duration_sec=1800)
    assert len(windows) == 3
    assert windows[0][0] == 0 and len(windows[0][3]) == 2
    assert windows[1][0] == 1 and len(windows[1][3]) == 1
    assert windows[2][0] == 2 and len(windows[2][3]) == 1

    assert estimate_tokens("Testo di prova") > 0
    assert normalize_text_for_comparison("Codice: AB-123/4!") == "codice ab-123/4"
    normalized = apply_custom_glossary_normalization(
        "Usiamo kubernetes e fastapi.",
        "kubernetes -> Kubernetes\nFastAPI",
    )
    assert "Kubernetes" in normalized
    assert "FastAPI" in normalized


def test_transcript_utils_edge_cases(tmp_path: Path) -> None:
    """Verifica casi limite: liste vuote, checksum sidecar e deduplicazione senza boundary."""
    assert split_segments_by_time_window([]) == []
    assert deduplicate_overlap_segments([], []) == []

    segs = [TranscriptSegment(TrackLabel.IO, 10.0, 15.0, "Ciao")]
    assert deduplicate_overlap_segments(segs, []) == segs
    assert deduplicate_overlap_segments([], segs) == segs

    # Deduplicazione senza boundary esplicito
    tail = [TranscriptSegment(TrackLabel.IO, 898.0, 900.0, "Frase in coda")]
    head = [TranscriptSegment(TrackLabel.IO, 899.0, 901.0, "Frase in coda estesa")]
    deduped = deduplicate_overlap_segments(tail, head, boundary_sec=None)
    assert len(deduped) == 1
    assert "estesa" in deduped[0].text

    # Verifica chunk transcript con sidecar sha256
    chunk_file = tmp_path / "chunk_002.txt"
    chunk_txt = format_chunk_transcript(
        chunk_index=2,
        session_id="s1",
        start_offset_sec=0.0,
        end_offset_sec=10.0,
        segments=[TranscriptSegment(TrackLabel.IO, 1.0, 2.0, "Segmento")],
    )
    chunk_file.write_text(chunk_txt, encoding="utf-8")
    sha_sidecar = chunk_file.with_suffix(".txt.sha256")
    from modules.contracts import sha256_file
    sha_sidecar.write_text(sha256_file(chunk_file), encoding="utf-8")

    art = validate_chunk_transcript_file(chunk_file)
    assert art.verified is True

