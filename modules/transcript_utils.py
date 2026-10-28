from __future__ import annotations

import difflib
import math
import re
from collections.abc import Sequence
from pathlib import Path
from typing import Final

from config import (
    CHUNK_OVERLAP_SEC,
    CUSTOM_GLOSSARY,
    GLOSSARY_PLACEHOLDER_TEXT,
    MAP_BLOCK_DURATION_SEC,
    TRANSCRIPT_DEDUP_SIMILARITY,
    TRANSCRIPT_DEDUP_TIME_TOLERANCE_SEC,
)
from modules.contracts import (
    TrackLabel,
    TranscriptionArtifact,
    TranscriptionError,
    TranscriptSegment,
    atomic_write_text,
    now_tz,
    sha256_file,
)

TRANSCRIPT_LINE_RE: Final[re.Pattern[str]] = re.compile(
    r"^\[(?P<h>\d{2}):(?P<m>\d{2}):(?P<s>\d{2})\] \[(?P<label>IO|ALTRI|INTERLOCUTORE\s+\d+)\]: (?P<text>.+)$"
)

NO_SPEECH_MARKER: str = "[NESSUN PARLATO RILEVATO]"

_HEADER_CHUNK_RE: re.Pattern[str] = re.compile(r"^# Chunk (?P<index>\d{3,})$")
_HEADER_INTERVAL_RE: re.Pattern[str] = re.compile(
    r"^# Intervallo: (?P<start>\d{2,}:\d{2}:\d{2}) - (?P<end>\d{2,}:\d{2}:\d{2})$"
)
_HEADER_SESSION_RE: re.Pattern[str] = re.compile(r"^# Sessione: (?P<session_id>\S.+)$")
_HMS_RE: re.Pattern[str] = re.compile(
    r"^(?P<h>\d{2,}):(?P<m>\d{2}):(?P<s>\d{2})$"
)
_NON_WORD_EXCEPT_SEP_RE: re.Pattern[str] = re.compile(r"[^\w\s./-]", re.UNICODE)
_BOUNDARY_SEP_RE: re.Pattern[str] = re.compile(
    r"(?<!\w)[./_-]+|[./_-]+(?!\w)",
    re.UNICODE,
)
_NUMERIC_TOKEN_RE: re.Pattern[str] = re.compile(r"\b\w*\d[\w./-]*\b", re.UNICODE)


def format_timestamp_hms(seconds: float) -> str:
    """Formatta un valore in secondi nel formato HH:MM:SS."""
    if math.isnan(seconds) or math.isinf(seconds) or seconds < 0.0:
        raise ValueError(f"Timestamp in secondi non valido: {seconds}")
    total_seconds = int(math.floor(seconds + 1e-6))
    hours = total_seconds // 3600
    minutes = (total_seconds % 3600) // 60
    secs = total_seconds % 60
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"


def parse_timestamp_hms(hms: str) -> float:
    """Converte una stringa HH:MM:SS in secondi float."""
    match = _HMS_RE.match(hms.strip())
    if match is None:
        raise ValueError(f"Formato timestamp non valido: {hms!r}")
    hours = int(match.group("h"))
    minutes = int(match.group("m"))
    seconds = int(match.group("s"))
    if minutes >= 60 or seconds >= 60:
        raise ValueError(f"Minuti o secondi fuori range in timestamp: {hms!r}")
    return float(hours * 3600 + minutes * 60 + seconds)


def parse_transcript_line(line: str, chunk_index: int = 0) -> TranscriptSegment | None:
    """Analizza una riga di transcript e restituisce TranscriptSegment o None."""
    _ = chunk_index
    stripped = line.strip()
    if not stripped or stripped.startswith("#") or stripped == NO_SPEECH_MARKER:
        return None
    match = TRANSCRIPT_LINE_RE.match(stripped)
    if match is None:
        return None
    hours = int(match.group("h"))
    minutes = int(match.group("m"))
    seconds = int(match.group("s"))
    if minutes >= 60 or seconds >= 60:
        return None
    label_raw = match.group("label").strip()
    text = match.group("text").strip()
    if not text:
        return None
    if label_raw == "IO":
        track = TrackLabel.IO
        speaker = None
    elif label_raw == "ALTRI":
        track = TrackLabel.OTHERS
        speaker = None
    elif label_raw.startswith("INTERLOCUTORE"):
        track = TrackLabel.OTHERS
        speaker = " ".join(label_raw.split())
    else:
        return None
    timestamp_sec = float(hours * 3600 + minutes * 60 + seconds)
    return TranscriptSegment(
        track=track,
        start_sec=timestamp_sec,
        end_sec=timestamp_sec,
        text=text,
        speaker=speaker,
    )


def format_transcript_segment(segment: TranscriptSegment) -> str:
    """Formatta un TranscriptSegment nel formato [HH:MM:SS] [IO|ALTRI|INTERLOCUTORE X]: <text>."""
    clean_text = " ".join(segment.text.splitlines()).strip()
    if not clean_text:
        raise ValueError("Il testo del segmento non può essere vuoto.")
    hms = format_timestamp_hms(max(0.0, segment.start_sec))
    return f"[{hms}] [{segment.display_label}]: {clean_text}"


def format_chunk_transcript(
    chunk_index: int,
    session_id: str,
    start_offset_sec: float,
    end_offset_sec: float,
    segments: Sequence[TranscriptSegment],
) -> str:
    """Formatta il contenuto completo di un file chunk_XXX.txt."""
    if chunk_index < 0:
        raise ValueError("chunk_index non può essere negativo.")
    clean_session_id = session_id.strip()
    if not clean_session_id:
        raise ValueError("session_id non può essere vuoto.")

    header_lines = [
        f"# Chunk {chunk_index:03d}",
        (
            f"# Intervallo: {format_timestamp_hms(max(0.0, start_offset_sec))} - "
            f"{format_timestamp_hms(max(0.0, end_offset_sec))}"
        ),
        f"# Sessione: {clean_session_id}",
        "",
    ]

    valid_segments = [
        seg for seg in segments if " ".join(seg.text.splitlines()).strip()
    ]
    sorted_segments = sorted(
        valid_segments,
        key=lambda seg: (seg.start_sec, seg.end_sec),
    )

    if sorted_segments:
        body_lines = [format_transcript_segment(seg) for seg in sorted_segments]
    else:
        body_lines = [NO_SPEECH_MARKER]

    return "\n".join(header_lines + body_lines) + "\n"


def validate_chunk_transcript_file(transcript_path: Path) -> TranscriptionArtifact:
    """Verifica integrità, formato e hash di un file transcript di chunk."""
    if not transcript_path.exists() or not transcript_path.is_file():
        raise TranscriptionError(
            f"File transcript mancante o non valido: {transcript_path}"
        )

    try:
        raw_text = transcript_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise TranscriptionError(
            f"Impossibile leggere il transcript in UTF-8: {transcript_path}"
        ) from exc

    lines = [line.rstrip("\r") for line in raw_text.splitlines()]
    if len(lines) < 4:
        raise TranscriptionError(
            f"Transcript incompleto o privo di header: {transcript_path}"
        )

    chunk_match = _HEADER_CHUNK_RE.match(lines[0].strip())
    interval_match = _HEADER_INTERVAL_RE.match(lines[1].strip())
    session_match = _HEADER_SESSION_RE.match(lines[2].strip())

    if chunk_match is None or interval_match is None or session_match is None:
        raise TranscriptionError(
            f"Header del transcript malformato in: {transcript_path}"
        )

    try:
        start_interval = parse_timestamp_hms(interval_match.group("start"))
        end_interval = parse_timestamp_hms(interval_match.group("end"))
    except ValueError as exc:
        raise TranscriptionError(
            f"Intervallo temporale non valido nell'header di: {transcript_path}"
        ) from exc

    if end_interval < start_interval:
        raise TranscriptionError(
            f"Intervallo temporale invertito nell'header di: {transcript_path}"
        )

    chunk_index = int(chunk_match.group("index"))
    body_non_empty = [line.strip() for line in lines[3:] if line.strip()]
    if not body_non_empty:
        raise TranscriptionError(
            f"Il corpo del transcript è vuoto: {transcript_path}"
        )

    segments: list[TranscriptSegment] = []
    if len(body_non_empty) == 1 and body_non_empty[0] == NO_SPEECH_MARKER:
        segments = []
    else:
        for body_line in body_non_empty:
            if body_line == NO_SPEECH_MARKER:
                raise TranscriptionError(
                    f"Marker nessun parlato mescolato a segmenti in: {transcript_path}"
                )
            parsed = parse_transcript_line(body_line)
            if parsed is None:
                raise TranscriptionError(
                    f"Riga di transcript non conforme al formato in {transcript_path}: {body_line!r}"
                )
            segments.append(parsed)

    try:
        file_sha256 = sha256_file(transcript_path)
    except OSError as exc:
        raise TranscriptionError(
            f"Impossibile calcolare SHA-256 per: {transcript_path}"
        ) from exc

    if not file_sha256:
        raise TranscriptionError(
            f"Hash SHA-256 vuoto per il transcript: {transcript_path}"
        )

    first_ts = segments[0].start_sec if segments else None
    last_ts = segments[-1].start_sec if segments else None

    return TranscriptionArtifact(
        chunk_index=chunk_index,
        transcript_path=transcript_path.resolve(),
        segment_count=len(segments),
        first_timestamp_sec=first_ts,
        last_timestamp_sec=last_ts,
        sha256=file_sha256,
        verified=True,
        completed_at=now_tz(),
    )


def normalize_text_for_comparison(text: str) -> str:
    """Normalizza il testo per il confronto di deduplicazione preservando numeri, codici e acronimi."""
    lowered = text.lower()
    without_punct = _NON_WORD_EXCEPT_SEP_RE.sub(" ", lowered)
    cleaned_seps = _BOUNDARY_SEP_RE.sub(" ", without_punct)
    return " ".join(cleaned_seps.split())


def is_duplicate_segment(
    prev_seg: TranscriptSegment,
    curr_seg: TranscriptSegment,
    similarity_threshold: float = TRANSCRIPT_DEDUP_SIMILARITY,
    time_tolerance_sec: float = TRANSCRIPT_DEDUP_TIME_TOLERANCE_SEC,
) -> bool:
    """Determina se due segmenti nella finestra di overlap sono duplicati della stessa traccia."""
    if prev_seg.track != curr_seg.track:
        return False

    if abs(curr_seg.start_sec - prev_seg.start_sec) > float(time_tolerance_sec):
        return False

    norm_prev = normalize_text_for_comparison(prev_seg.text)
    norm_curr = normalize_text_for_comparison(curr_seg.text)
    if not norm_prev or not norm_curr:
        return False

    if norm_prev == norm_curr:
        return True

    nums_prev = set(_NUMERIC_TOKEN_RE.findall(norm_prev))
    nums_curr = set(_NUMERIC_TOKEN_RE.findall(norm_curr))
    if nums_prev != nums_curr:
        if not nums_prev or not nums_curr:
            return False
        if not (nums_prev <= nums_curr or nums_curr <= nums_prev):
            return False
        shorter_for_num = (
            norm_prev if len(norm_prev) <= len(norm_curr) else norm_curr
        )
        longer_for_num = (
            norm_curr if len(norm_prev) <= len(norm_curr) else norm_prev
        )
        if shorter_for_num not in longer_for_num:
            return False

    matcher = difflib.SequenceMatcher(None, norm_prev, norm_curr)
    if matcher.ratio() >= similarity_threshold:
        return True

    shorter, longer = (
        (norm_prev, norm_curr)
        if len(norm_prev) <= len(norm_curr)
        else (norm_curr, norm_prev)
    )
    if len(shorter) >= 4:
        if shorter in longer:
            return True
        longest_block = difflib.SequenceMatcher(
            None, shorter, longer
        ).find_longest_match(0, len(shorter), 0, len(longer))
        if (longest_block.size / len(shorter)) >= similarity_threshold:
            return True

    return False


def deduplicate_overlap_segments(
    prev_segments: Sequence[TranscriptSegment],
    new_segments: Sequence[TranscriptSegment],
    boundary_sec: float | None = None,
    overlap_sec: float = CHUNK_OVERLAP_SEC,
    time_tolerance_sec: float = TRANSCRIPT_DEDUP_TIME_TOLERANCE_SEC,
    similarity_threshold: float = TRANSCRIPT_DEDUP_SIMILARITY,
) -> list[TranscriptSegment]:
    """Deduplica i segmenti tra la coda del chunk precedente e la testa del nuovo chunk."""
    if not prev_segments:
        return list(new_segments)
    if not new_segments:
        return list(prev_segments)

    result: list[TranscriptSegment] = list(prev_segments)
    window_span = float(overlap_sec) + float(time_tolerance_sec)

    if boundary_sec is not None:
        tail_min_sec = boundary_sec - window_span
        head_max_sec = boundary_sec + window_span
    else:
        max_prev_sec = max(seg.start_sec for seg in prev_segments)
        min_new_sec = min(seg.start_sec for seg in new_segments)
        tail_min_sec = min(max_prev_sec, min_new_sec) - window_span
        head_max_sec = max(max_prev_sec, min_new_sec) + window_span

    tail_indices = [
        idx
        for idx, seg in enumerate(result)
        if seg.start_sec >= tail_min_sec or seg.end_sec >= tail_min_sec
    ]

    matched_prev_indices: set[int] = set()

    for new_seg in new_segments:
        if new_seg.start_sec <= head_max_sec:
            duplicate_idx: int | None = None
            for prev_idx in tail_indices:
                if prev_idx in matched_prev_indices:
                    continue
                candidate_prev = result[prev_idx]
                if is_duplicate_segment(
                    candidate_prev,
                    new_seg,
                    similarity_threshold=similarity_threshold,
                    time_tolerance_sec=time_tolerance_sec,
                ):
                    duplicate_idx = prev_idx
                    break

            if duplicate_idx is not None:
                matched_prev_indices.add(duplicate_idx)
                existing_seg = result[duplicate_idx]
                norm_existing = normalize_text_for_comparison(existing_seg.text)
                norm_new = normalize_text_for_comparison(new_seg.text)
                if len(norm_new) > len(norm_existing) or (
                    len(norm_new) == len(norm_existing)
                    and len(new_seg.text.strip()) > len(existing_seg.text.strip())
                ):
                    result[duplicate_idx] = new_seg
                continue

        result.append(new_seg)

    result.sort(key=lambda seg: (seg.start_sec, seg.end_sec))
    return result


def _parse_chunk_file_with_interval(
    transcript_path: Path,
) -> tuple[float, float, list[TranscriptSegment]]:
    """Valida e legge un file transcript di chunk restituendo intervallo e segmenti."""
    validate_chunk_transcript_file(transcript_path)
    lines = transcript_path.read_text(encoding="utf-8").splitlines()
    interval_match = _HEADER_INTERVAL_RE.match(lines[1].strip())
    assert interval_match is not None
    start_sec = parse_timestamp_hms(interval_match.group("start"))
    end_sec = parse_timestamp_hms(interval_match.group("end"))

    segments: list[TranscriptSegment] = []
    for raw_line in lines[3:]:
        parsed = parse_transcript_line(raw_line)
        if parsed is not None:
            segments.append(parsed)
    return start_sec, end_sec, segments


def merge_chunk_transcripts(
    transcript_paths: Sequence[Path],
    output_path: Path,
) -> list[TranscriptSegment]:
    """Unisce i transcript dei chunk in ordine, deduplicando gli overlap e scrivendo output_path."""
    merged_segments: list[TranscriptSegment] = []

    for path in transcript_paths:
        start_sec, _end_sec, chunk_segments = _parse_chunk_file_with_interval(path)
        if not merged_segments:
            merged_segments = list(chunk_segments)
        else:
            merged_segments = deduplicate_overlap_segments(
                merged_segments,
                chunk_segments,
                boundary_sec=start_sec,
            )

    merged_segments.sort(key=lambda seg: (seg.start_sec, seg.end_sec))

    if merged_segments:
        body = "\n".join(format_transcript_segment(seg) for seg in merged_segments) + "\n"
    else:
        body = f"{NO_SPEECH_MARKER}\n"

    atomic_write_text(output_path, body)
    return merged_segments


def split_segments_by_time_window(
    segments: Sequence[TranscriptSegment],
    window_duration_sec: int = MAP_BLOCK_DURATION_SEC,
) -> list[tuple[int, float, float, list[TranscriptSegment]]]:
    """Suddivide i segmenti in finestre temporali fisse [0, W), [W, 2W), ... basate sui timestamp."""
    if window_duration_sec <= 0:
        raise ValueError("window_duration_sec deve essere maggiore di zero.")
    if not segments:
        return []

    sorted_segments = sorted(segments, key=lambda seg: (seg.start_sec, seg.end_sec))
    max_start_sec = max(0.0, max(seg.start_sec for seg in sorted_segments))
    total_windows = int(math.floor(max_start_sec / float(window_duration_sec))) + 1

    windows: list[tuple[int, float, float, list[TranscriptSegment]]] = []
    for block_idx in range(total_windows):
        win_start = float(block_idx * window_duration_sec)
        win_end = float((block_idx + 1) * window_duration_sec)
        win_segments = [
            seg
            for seg in sorted_segments
            if win_start <= max(0.0, seg.start_sec) < win_end
        ]
        windows.append((block_idx, win_start, win_end, win_segments))

    return windows


def estimate_tokens(text: str) -> int:
    """Stima conservativa del numero di token per testo italiano/inglese."""
    if not text:
        return 0
    return math.ceil(len(text) / 3.2)


def resolve_glossary(raw: str | None = None) -> str:
    """Restituisce il glossario effettivo: stringa vuota se assente o se è ancora il testo segnaposto."""
    text = raw if raw is not None else CUSTOM_GLOSSARY
    stripped = (text or "").strip()
    if not stripped:
        return ""
    if " ".join(stripped.split()) == " ".join(GLOSSARY_PLACEHOLDER_TEXT.split()):
        return ""
    return stripped


def apply_custom_glossary_normalization(
    text: str,
    glossary: str | None = None,
) -> str:
    """Normalizza nel testo i termini presenti nel glossario senza forzare parole assenti."""
    stripped_glossary = resolve_glossary(glossary)
    if not text or not stripped_glossary:
        return text

    result = text
    raw_entries: list[str] = []
    for raw_line in stripped_glossary.splitlines():
        clean_line = raw_line.strip().lstrip("-*•").strip()
        if not clean_line or clean_line.startswith("#"):
            continue
        if any(sep in clean_line for sep in ("->", "=>", "=")):
            raw_entries.append(clean_line)
        else:
            for part in re.split(r"[,;]", clean_line):
                token = part.strip()
                if token:
                    raw_entries.append(token)

    for entry in raw_entries:
        mapping_match = re.match(
            r"^(?P<src>.+?)\s*(?:->|=>|=)\s*(?P<dst>.+)$",
            entry,
        )
        if mapping_match is not None:
            src = mapping_match.group("src").strip()
            dst = mapping_match.group("dst").strip()
            if src and dst:
                pattern = re.compile(rf"\b{re.escape(src)}\b", re.IGNORECASE)
                result = pattern.sub(dst, result)
        else:
            term = entry.strip()
            if len(term) >= 2:
                pattern = re.compile(rf"\b{re.escape(term)}\b", re.IGNORECASE)
                result = pattern.sub(term, result)

    return result
