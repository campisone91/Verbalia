from __future__ import annotations

import os
import subprocess
import wave
from collections.abc import Mapping
from pathlib import Path

from config import (
    CHUNK_DURATION_SEC,
    CHUNK_OVERLAP_SEC,
    LOOPBACK_GAP_TOLERANCE_MS,
    MAX_SILENCE_INJECTION_MS,
    TARGET_CHANNELS,
    TARGET_SAMPLE_RATE,
    TARGET_SAMPLE_WIDTH_BYTES,
    windows_creation_flags,
)
from modules.contracts import (
    AudioDeviceError,
    AudioProcessingError,
    DeviceDescriptor,
    replace_with_retry,
)
from modules.logging_setup import get_logger

try:
    import pyaudiowpatch
except ImportError:  # pragma: no cover - gestito in ambienti senza PyAudioWPatch
    pyaudiowpatch = None  # type: ignore[assignment]

logger = get_logger("audio_utils")

# FFmpeg gira sotto la priorità normale per non sottrarre CPU alla cattura audio
BELOW_NORMAL_PRIORITY_CLASS: int = 0x00004000

_WAV_HEADER_MIN_BYTES: int = 44
_SILENCE_WRITE_BLOCK_FRAMES: int = 16000


def _to_int(value: object, default: int = 0) -> int:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(round(value))
    if isinstance(value, str):
        try:
            return int(float(value.strip()))
        except ValueError:
            return default
    return default


def _to_float(value: object, default: float = 0.0) -> float:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip())
        except ValueError:
            return default
    return default


def _resolve_host_api_name(
    pa: object,
    host_api_index: object,
    default_name: str = "WASAPI",
) -> str:
    """Recupera il nome dell'Host API da un'istanza PyAudio."""
    if isinstance(host_api_index, int) and hasattr(pa, "get_host_api_info_by_index"):
        try:
            getter = getattr(pa, "get_host_api_info_by_index")
            info = getter(host_api_index)
            if isinstance(info, Mapping) and isinstance(info.get("name"), str):
                return str(info["name"])
        except Exception as exc:
            logger.debug("Impossibile leggere host API %s: %s", host_api_index, exc)
    return default_name


def _find_microphone_device_info(pa: object) -> Mapping[str, object]:
    """Individua il dizionario descrittivo del microfono predefinito."""
    if hasattr(pa, "get_default_input_device_info"):
        try:
            info = getattr(pa, "get_default_input_device_info")()
            if isinstance(info, Mapping) and int(info.get("maxInputChannels", 0)) > 0:
                return info
        except Exception as exc:
            logger.debug("get_default_input_device_info fallita, tento scansione: %s", exc)

    if hasattr(pa, "get_device_count") and hasattr(pa, "get_device_info_by_index"):
        count = int(getattr(pa, "get_device_count")())
        by_index = getattr(pa, "get_device_info_by_index")
        for idx in range(count):
            dev = by_index(idx)
            if (
                isinstance(dev, Mapping)
                and int(dev.get("maxInputChannels", 0)) > 0
                and not bool(dev.get("isLoopbackDevice", False))
            ):
                return dev

    raise AudioDeviceError("Nessun dispositivo microfono di input trovato.")


def _find_wasapi_loopback_device_info(pa: object) -> Mapping[str, object]:
    """Individua il dizionario descrittivo del dispositivo WASAPI loopback predefinito."""
    if hasattr(pa, "get_default_wasapi_loopback"):
        try:
            info = getattr(pa, "get_default_wasapi_loopback")()
            if isinstance(info, Mapping):
                return info
        except Exception as exc:
            logger.debug("get_default_wasapi_loopback fallita, tento fallback: %s", exc)

    default_output: Mapping[str, object] | None = None
    if hasattr(pa, "get_host_api_info_by_type") and pyaudiowpatch is not None:
        wasapi_const = getattr(pyaudiowpatch, "paWASAPI", 13)
        try:
            wasapi_info = getattr(pa, "get_host_api_info_by_type")(wasapi_const)
            if isinstance(wasapi_info, Mapping):
                out_idx = int(wasapi_info.get("defaultOutputDevice", -1))
                if out_idx >= 0 and hasattr(pa, "get_device_info_by_index"):
                    candidate = getattr(pa, "get_device_info_by_index")(out_idx)
                    if isinstance(candidate, Mapping):
                        default_output = candidate
        except Exception as exc:
            logger.debug("Ricerca host API WASAPI fallita: %s", exc)

    if default_output is None and hasattr(pa, "get_default_output_device_info"):
        try:
            candidate = getattr(pa, "get_default_output_device_info")()
            if isinstance(candidate, Mapping):
                default_output = candidate
        except Exception as exc:
            logger.debug("get_default_output_device_info fallita: %s", exc)

    if default_output is not None:
        if bool(default_output.get("isLoopbackDevice", False)):
            return default_output
        if hasattr(pa, "get_wasapi_loopback_analogue_by_dict"):
            try:
                analogue = getattr(pa, "get_wasapi_loopback_analogue_by_dict")(
                    dict(default_output)
                )
                if isinstance(analogue, Mapping):
                    return analogue
            except Exception as exc:
                logger.debug("get_wasapi_loopback_analogue_by_dict fallita: %s", exc)
        if hasattr(pa, "get_wasapi_loopback_analogue_by_index") and "index" in default_output:
            try:
                analogue = getattr(pa, "get_wasapi_loopback_analogue_by_index")(
                    _to_int(default_output["index"])
                )
                if isinstance(analogue, Mapping):
                    return analogue
            except Exception as exc:
                logger.debug("get_wasapi_loopback_analogue_by_index fallita: %s", exc)

    if hasattr(pa, "get_loopback_device_info_generator"):
        try:
            for dev in getattr(pa, "get_loopback_device_info_generator")():
                if isinstance(dev, Mapping):
                    if default_output is not None:
                        out_name = str(default_output.get("name", ""))
                        if out_name and out_name in str(dev.get("name", "")):
                            return dev
                    else:
                        return dev
        except Exception as exc:
            logger.debug("get_loopback_device_info_generator fallita: %s", exc)

    if hasattr(pa, "get_device_count") and hasattr(pa, "get_device_info_by_index"):
        count = int(getattr(pa, "get_device_count")())
        by_index = getattr(pa, "get_device_info_by_index")
        first_loopback: Mapping[str, object] | None = None
        for idx in range(count):
            dev = by_index(idx)
            if isinstance(dev, Mapping) and bool(dev.get("isLoopbackDevice", False)):
                if first_loopback is None:
                    first_loopback = dev
                if default_output is not None:
                    out_name = str(default_output.get("name", ""))
                    if out_name and out_name in str(dev.get("name", "")):
                        return dev
        if first_loopback is not None:
            return first_loopback

    raise AudioDeviceError("Nessun dispositivo WASAPI loopback trovato.")


def discover_audio_devices(
    pyaudio_instance: object | None = None,
) -> tuple[DeviceDescriptor, DeviceDescriptor]:
    """Individua il microfono predefinito e il loopback WASAPI associato (Sezione 20.3).

    Restituisce una tupla `(mic_descriptor, loopback_descriptor)`.
    Solleva `AudioDeviceError` se uno dei dispositivi non è disponibile o non valido.
    """
    owns_instance = False
    pa = pyaudio_instance
    if pa is None:
        if pyaudiowpatch is None or not hasattr(pyaudiowpatch, "PyAudio"):
            raise AudioDeviceError(
                "Modulo PyAudioWPatch non disponibile per l'enumerazione dei dispositivi."
            )
        try:
            pa = pyaudiowpatch.PyAudio()
            owns_instance = True
        except Exception as exc:
            raise AudioDeviceError(
                f"Impossibile inizializzare PyAudioWPatch: {exc}"
            ) from exc

    try:
        mic_info = _find_microphone_device_info(pa)
        loopback_info = _find_wasapi_loopback_device_info(pa)

        mic_index = _to_int(mic_info.get("index", 0), 0)
        mic_name = str(mic_info.get("name", "Default Microphone")).strip() or "Default Microphone"
        mic_host = _resolve_host_api_name(pa, mic_info.get("hostApi"), "MME")
        mic_rate = int(round(_to_float(mic_info.get("defaultSampleRate", 48000), 48000.0)))
        mic_channels = max(1, _to_int(mic_info.get("maxInputChannels", 1), 1))

        loop_index = _to_int(loopback_info.get("index", 0), 0)
        loop_name = (
            str(loopback_info.get("name", "WASAPI Loopback")).strip()
            or "WASAPI Loopback"
        )
        loop_host = _resolve_host_api_name(pa, loopback_info.get("hostApi"), "Windows WASAPI")
        loop_rate = int(round(_to_float(loopback_info.get("defaultSampleRate", 48000), 48000.0)))
        raw_loop_ch = _to_int(
            loopback_info.get(
                "maxInputChannels",
                loopback_info.get("maxOutputChannels", 2),
            ),
            2,
        )
        loop_channels = max(1, raw_loop_ch)

        if mic_rate <= 0 or loop_rate <= 0:
            raise AudioDeviceError(
                f"Sample rate non valido: mic={mic_rate}, loopback={loop_rate}"
            )

        mic_descriptor = DeviceDescriptor(
            index=mic_index,
            name=mic_name,
            host_api=mic_host,
            sample_rate=mic_rate,
            channels=mic_channels,
            sample_width=TARGET_SAMPLE_WIDTH_BYTES,
            is_loopback=False,
        )
        loopback_descriptor = DeviceDescriptor(
            index=loop_index,
            name=loop_name,
            host_api=loop_host,
            sample_rate=loop_rate,
            channels=loop_channels,
            sample_width=TARGET_SAMPLE_WIDTH_BYTES,
            is_loopback=True,
        )
        return mic_descriptor, loopback_descriptor
    except AudioDeviceError:
        raise
    except Exception as exc:
        raise AudioDeviceError(
            f"Errore durante la scoperta dei dispositivi audio: {exc}"
        ) from exc
    finally:
        if owns_instance and pa is not None and hasattr(pa, "terminate"):
            try:
                getattr(pa, "terminate")()
            except Exception as exc:
                logger.debug("Errore terminando istanza temporanea PyAudio: %s", exc)


def calculate_missing_frames(
    elapsed_seconds: float,
    sample_rate: int,
    written_frames: int,
) -> int:
    """Calcola il numero di frame mancanti rispetto alla timeline monotona (Sezione 20.5)."""
    if elapsed_seconds <= 0.0 or sample_rate <= 0:
        return 0
    expected_frames = round(elapsed_seconds * sample_rate)
    missing_frames = expected_frames - written_frames
    return max(0, int(missing_frames))


def generate_silence_bytes(
    frame_count: int,
    channels: int,
    sample_width: int = TARGET_SAMPLE_WIDTH_BYTES,
) -> bytes:
    """Genera un buffer PCM di silenzio digitale (zeri) per il numero di frame indicato."""
    if frame_count <= 0:
        return b""
    if channels <= 0 or sample_width <= 0:
        raise ValueError("I canali e il sample_width devono essere maggiori di zero.")
    total_bytes = int(frame_count) * int(channels) * int(sample_width)
    return b"\x00" * total_bytes


def compute_silence_injection_frames(
    missing_frames: int,
    sample_rate: int,
    gap_tolerance_ms: int = LOOPBACK_GAP_TOLERANCE_MS,
    max_injection_ms: int = MAX_SILENCE_INJECTION_MS,
) -> int:
    """Restituisce i frame di silenzio da iniettare oltre la tolleranza, limitati al massimo per step."""
    if missing_frames <= 0 or sample_rate <= 0:
        return 0
    tolerance_frames = round((gap_tolerance_ms / 1000.0) * sample_rate)
    if missing_frames <= tolerance_frames:
        return 0
    max_injection_frames = max(1, round((max_injection_ms / 1000.0) * sample_rate))
    return min(int(missing_frames), int(max_injection_frames))


def compute_track_drift_ms(
    io_frames: int,
    io_rate: int,
    others_frames: int,
    others_rate: int,
) -> float:
    """Calcola lo scostamento temporale assoluto in millisecondi tra traccia IO e ALTRI."""
    if io_rate <= 0 or others_rate <= 0:
        raise ValueError("I sample rate delle tracce devono essere positivi.")
    io_duration_sec = max(0, io_frames) / float(io_rate)
    others_duration_sec = max(0, others_frames) / float(others_rate)
    return round(abs(io_duration_sec - others_duration_sec) * 1000.0, 3)


def build_ffmpeg_normalize_command(
    ffmpeg_path: Path,
    input_wav: Path,
    output_wav: Path,
) -> list[str]:
    """Costruisce la lista di argomenti FFmpeg per la normalizzazione a 16 kHz mono (Sezione 20.7)."""
    return [
        str(ffmpeg_path),
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-i",
        str(input_wav),
        "-af",
        "loudnorm=I=-16:LRA=11:TP=-1.5,aresample=16000:async=1:first_pts=0",
        "-ac",
        str(TARGET_CHANNELS),
        "-ar",
        str(TARGET_SAMPLE_RATE),
        "-c:a",
        "pcm_s16le",
        str(output_wav),
    ]


def validate_normalized_wav(wav_path: Path) -> bool:
    """Valida che un file WAV sia apribile, mono, 16 kHz, 16-bit PCM e con frame coerenti (Sezione 20.7).

    Restituisce `True` se valido, altrimenti solleva `AudioProcessingError`.
    """
    if not wav_path.exists() or not wav_path.is_file():
        raise AudioProcessingError(f"File WAV normalizzato inesistente: {wav_path}")

    try:
        file_size = wav_path.stat().st_size
    except OSError as exc:
        raise AudioProcessingError(
            f"Impossibile leggere dimensione WAV {wav_path}: {exc}"
        ) from exc

    if file_size <= _WAV_HEADER_MIN_BYTES:
        raise AudioProcessingError(
            f"File WAV troppo piccolo o privo di dati ({file_size} byte): {wav_path}"
        )

    try:
        with wave.open(str(wav_path), "rb") as wav_file:
            channels = wav_file.getnchannels()
            sample_width = wav_file.getsampwidth()
            sample_rate = wav_file.getframerate()
            nframes = wav_file.getnframes()

            if channels != TARGET_CHANNELS:
                raise AudioProcessingError(
                    f"Canali WAV non validi per {wav_path}: attesi {TARGET_CHANNELS}, trovati {channels}"
                )
            if sample_rate != TARGET_SAMPLE_RATE:
                raise AudioProcessingError(
                    f"Sample rate WAV non valido per {wav_path}: atteso {TARGET_SAMPLE_RATE}, trovato {sample_rate}"
                )
            if sample_width != TARGET_SAMPLE_WIDTH_BYTES:
                raise AudioProcessingError(
                    f"Sample width WAV non valido per {wav_path}: atteso {TARGET_SAMPLE_WIDTH_BYTES}, trovato {sample_width}"
                )
            if nframes <= 0:
                raise AudioProcessingError(
                    f"Il file WAV {wav_path} non contiene alcun frame audio."
                )

            frame_bytes = channels * sample_width
            expected_min_size = _WAV_HEADER_MIN_BYTES + (nframes * frame_bytes)
            if file_size < expected_min_size:
                raise AudioProcessingError(
                    f"Dimensione WAV incoerente per {wav_path}: {file_size} < {expected_min_size}"
                )

            sample_read = wav_file.readframes(min(nframes, 1024))
            if not sample_read or len(sample_read) % frame_bytes != 0:
                raise AudioProcessingError(
                    f"Frame WAV corrotti o illeggibili in {wav_path}"
                )
    except AudioProcessingError:
        raise
    except (wave.Error, EOFError, OSError) as exc:
        raise AudioProcessingError(
            f"Impossibile aprire o validare il file WAV {wav_path}: {exc}"
        ) from exc

    return True


def is_valid_normalized_wav(wav_path: Path) -> bool:
    """Restituisce True se il file WAV rispetta tutti i criteri di normalizzazione, False altrimenti."""
    try:
        return validate_normalized_wav(wav_path)
    except AudioProcessingError:
        return False


def normalize_wav_with_ffmpeg(
    ffmpeg_path: Path,
    raw_wav: Path,
    normalized_wav: Path,
    *,
    delete_raw_on_success: bool = True,
    timeout_sec: int = 300,
) -> Path:
    """Normalizza un file WAV grezzo a 16 kHz mono tramite FFmpeg (Sezioni 3.2 e 20.7).

    Elimina il file raw esclusivamente dopo che il file normalizzato è stato
    scritto, promosso atomicamente, riaperto e validato con `wave`.
    In caso di errore FFmpeg o validazione fallita, il file raw viene preservato.
    """
    if not raw_wav.exists() or not raw_wav.is_file():
        raise AudioProcessingError(f"File WAV grezzo non trovato: {raw_wav}")

    normalized_wav.parent.mkdir(parents=True, exist_ok=True)
    temp_output = normalized_wav.with_suffix(".tmp.wav")

    if temp_output.exists():
        try:
            temp_output.unlink()
        except OSError as exc:
            logger.debug("Impossibile rimuovere file temporaneo preesistente %s: %s", temp_output, exc)

    command = build_ffmpeg_normalize_command(
        ffmpeg_path=ffmpeg_path,
        input_wav=raw_wav,
        output_wav=temp_output,
    )

    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_sec,
            check=False,
            creationflags=windows_creation_flags() | BELOW_NORMAL_PRIORITY_CLASS,
        )
    except subprocess.TimeoutExpired as exc:
        if temp_output.exists():
            try:
                temp_output.unlink()
            except OSError as unlink_exc:
                logger.debug("Errore pulizia temp WAV dopo timeout: %s", unlink_exc)
        raise AudioProcessingError(
            f"Timeout FFmpeg ({timeout_sec}s) durante la normalizzazione di {raw_wav}"
        ) from exc
    except OSError as exc:
        if temp_output.exists():
            try:
                temp_output.unlink()
            except OSError as unlink_exc:
                logger.debug("Errore pulizia temp WAV dopo OSError: %s", unlink_exc)
        raise AudioProcessingError(
            f"Impossibile eseguire FFmpeg ({ffmpeg_path}): {exc}"
        ) from exc

    if completed.returncode != 0:
        if temp_output.exists():
            try:
                temp_output.unlink()
            except OSError as unlink_exc:
                logger.debug("Errore pulizia temp WAV dopo fallimento FFmpeg: %s", unlink_exc)
        stderr_msg = (completed.stderr or "").strip()
        raise AudioProcessingError(
            f"FFmpeg terminato con codice {completed.returncode} su {raw_wav}: {stderr_msg}"
        )

    try:
        validate_normalized_wav(temp_output)
        replace_with_retry(temp_output, normalized_wav)
        validate_normalized_wav(normalized_wav)
    except Exception as exc:
        if temp_output.exists():
            try:
                temp_output.unlink()
            except OSError as unlink_exc:
                logger.debug("Errore rimozione temp WAV invalido: %s", unlink_exc)
        if isinstance(exc, AudioProcessingError):
            raise
        raise AudioProcessingError(
            f"Errore durante la promozione/validazione di {normalized_wav}: {exc}"
        ) from exc

    if delete_raw_on_success and raw_wav.resolve() != normalized_wav.resolve():
        try:
            raw_wav.unlink()
        except OSError as exc:
            logger.warning(
                "WAV normalizzato valido ma impossibile eliminare il raw %s: %s",
                raw_wav,
                exc,
            )

    return normalized_wav


def create_silent_wav(
    wav_path: Path,
    duration_sec: float,
    sample_rate: int = TARGET_SAMPLE_RATE,
    channels: int = TARGET_CHANNELS,
    sample_width: int = TARGET_SAMPLE_WIDTH_BYTES,
) -> Path:
    """Crea atomicamente un file WAV contenente silenzio digitale della durata specificata."""
    if duration_sec <= 0.0:
        raise ValueError("La durata del file WAV silenzioso deve essere positiva.")
    if sample_rate <= 0 or channels <= 0 or sample_width <= 0:
        raise ValueError("Parametri formato WAV non validi.")

    wav_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = wav_path.with_suffix(".tmp.wav")
    total_frames = max(1, round(duration_sec * sample_rate))

    try:
        with wave.open(str(temp_path), "wb") as wav_file:
            wav_file.setnchannels(channels)
            wav_file.setsampwidth(sample_width)
            wav_file.setframerate(sample_rate)

            remaining = total_frames
            block_bytes = generate_silence_bytes(
                min(remaining, _SILENCE_WRITE_BLOCK_FRAMES),
                channels=channels,
                sample_width=sample_width,
            )
            block_frames = min(remaining, _SILENCE_WRITE_BLOCK_FRAMES)

            while remaining > 0:
                if remaining >= block_frames:
                    wav_file.writeframes(block_bytes)
                    remaining -= block_frames
                else:
                    tail_bytes = generate_silence_bytes(
                        remaining,
                        channels=channels,
                        sample_width=sample_width,
                    )
                    wav_file.writeframes(tail_bytes)
                    remaining = 0

        os.replace(temp_path, wav_path)
        return wav_path
    except Exception as exc:
        if temp_path.exists():
            try:
                temp_path.unlink()
            except OSError as unlink_exc:
                logger.debug("Errore pulizia temp silent WAV: %s", unlink_exc)
        raise AudioProcessingError(
            f"Impossibile generare il file WAV silenzioso {wav_path}: {exc}"
        ) from exc


def get_wav_duration_sec(wav_path: Path) -> float:
    """Restituisce la durata in secondi di un file WAV."""
    if not wav_path.exists() or not wav_path.is_file():
        raise AudioProcessingError(f"File WAV non trovato: {wav_path}")
    try:
        with wave.open(str(wav_path), "rb") as wav_file:
            rate = wav_file.getframerate()
            frames = wav_file.getnframes()
            if rate <= 0:
                raise AudioProcessingError(f"Sample rate non valido in {wav_path}: {rate}")
            return max(0.0, frames / float(rate))
    except AudioProcessingError:
        raise
    except (wave.Error, EOFError, OSError) as exc:
        raise AudioProcessingError(
            f"Impossibile leggere la durata del file WAV {wav_path}: {exc}"
        ) from exc


def split_wav_file(
    input_wav: Path,
    output_dir: Path,
    chunk_duration_sec: int = CHUNK_DURATION_SEC,
    overlap_sec: int = CHUNK_OVERLAP_SEC,
) -> list[tuple[Path, float, float]]:
    """Suddivide un file WAV in chunk con overlap per la modalità --test-pipeline (Sezione 30).

    Restituisce una lista di tuple `(chunk_wav_path, start_offset_sec, end_offset_sec)`.
    Non modifica né elimina mai il file `input_wav`.
    """
    if not input_wav.exists() or not input_wav.is_file():
        raise AudioProcessingError(f"File WAV di input non trovato: {input_wav}")
    if chunk_duration_sec <= 0:
        raise ValueError("chunk_duration_sec deve essere maggiore di zero.")
    if overlap_sec < 0 or overlap_sec >= chunk_duration_sec:
        raise ValueError("overlap_sec deve essere >= 0 e minore di chunk_duration_sec.")

    output_dir.mkdir(parents=True, exist_ok=True)
    results: list[tuple[Path, float, float]] = []

    try:
        with wave.open(str(input_wav), "rb") as src:
            channels = src.getnchannels()
            sample_width = src.getsampwidth()
            sample_rate = src.getframerate()
            total_frames = src.getnframes()

            if sample_rate <= 0 or total_frames <= 0:
                raise AudioProcessingError(
                    f"Il file WAV {input_wav} è vuoto o ha sample rate non valido."
                )

            chunk_frames = chunk_duration_sec * sample_rate
            overlap_frames = overlap_sec * sample_rate

            chunk_index = 0
            start_frame = 0

            while start_frame < total_frames:
                end_frame = min(total_frames, start_frame + chunk_frames)
                frames_to_read = end_frame - start_frame
                if frames_to_read <= 0:
                    break

                src.setpos(start_frame)
                raw_data = src.readframes(frames_to_read)

                chunk_path = output_dir / f"chunk_{chunk_index:03d}_altri_raw.wav"
                temp_chunk = chunk_path.with_suffix(".tmp.wav")
                with wave.open(str(temp_chunk), "wb") as dst:
                    dst.setnchannels(channels)
                    dst.setsampwidth(sample_width)
                    dst.setframerate(sample_rate)
                    dst.writeframes(raw_data)

                os.replace(temp_chunk, chunk_path)

                start_sec = round(start_frame / float(sample_rate), 3)
                end_sec = round(end_frame / float(sample_rate), 3)
                results.append((chunk_path, start_sec, end_sec))

                if end_frame >= total_frames:
                    break

                start_frame = max(start_frame + 1, end_frame - overlap_frames)
                chunk_index += 1

        return results
    except AudioProcessingError:
        raise
    except (wave.Error, EOFError, OSError) as exc:
        raise AudioProcessingError(
            f"Errore durante la suddivisione del file WAV {input_wav}: {exc}"
        ) from exc
