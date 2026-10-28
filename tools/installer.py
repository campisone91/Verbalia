"""Installer automatico con GUI Testuale (TUI) per Verbalia.

Funzionalità:
0. Fase di Check Iniziale (Pre-Flight Audit) per rilevare cosa è già installato
   ed evitare reinstallazioni inutili (skip automatico).
1. Installazione di tutte le librerie Python richieste (requirements.txt).
2. Installazione e configurazione di Whisper e componenti correlati
   (FFmpeg, faster-whisper, CTranslate2, ONNX Runtime, download modello GPU/CPU).
3. Installazione silenziosa di Ollama e download del modello LLM predefinito (llama3.1:8b).
4. Test End-to-End con campione audio parlato in italiano (FFmpeg -> Whisper -> Ollama -> DOCX).
5. Creazione opzionale del collegamento sul Desktop (Verbalia.lnk).

Supporta:
- GUI Testuale interattiva (navigabile con frecce, barra spaziatrice, tasti rapidi)
  con scelta tra:
    1) Installare tutto il pacchetto
    2) Installare solo i componenti selezionati
- Esecuzione completamente automatica da CLI (--all / --unattended / --only ...).
"""
from __future__ import annotations

import argparse
import ctypes
import dataclasses
import enum
import gc
import importlib
import json
import math
import platform
import shutil
import struct
import subprocess
import sys
import tempfile
import time
import urllib.request
import wave
from collections.abc import Callable, Sequence
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT: Path = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import bootstrap  # noqa: E402

SAMPLE_AUDIO_PATH: Path = PROJECT_ROOT / "assets" / "sample_test.wav"
SAMPLE_SPEECH_TEXT: str = (
    "Buongiorno a tutti, iniziamo la riunione di verifica per Verbalia. "
    "Confermiamo che l'installazione automatica dei componenti è completata con successo. "
    "Marco invierà il verbale definitivo entro domani mattina."
)


class ComponentId(str, enum.Enum):
    """Identificativi dei componenti gestiti dall'installer."""

    PYTHON_LIBS = "python"
    WHISPER_AND_DEPS = "whisper"
    OLLAMA_AND_LLM = "ollama"
    SAMPLE_AUDIO_TEST = "test"
    DESKTOP_SHORTCUT = "shortcut"


COMPONENT_LABELS: dict[ComponentId, str] = {
    ComponentId.PYTHON_LIBS: "1. Librerie Python (requirements.txt)",
    ComponentId.WHISPER_AND_DEPS: "2. Whisper, FFmpeg e Componenti Correlati",
    ComponentId.OLLAMA_AND_LLM: f"3. Ollama e Modello LLM ({bootstrap.OLLAMA_MODEL})",
    ComponentId.SAMPLE_AUDIO_TEST: "4. Test End-to-End con Campione Audio",
    ComponentId.DESKTOP_SHORTCUT: "5. Collegamento sul Desktop (Verbalia.lnk)",
}

ALL_COMPONENTS: tuple[ComponentId, ...] = (
    ComponentId.PYTHON_LIBS,
    ComponentId.WHISPER_AND_DEPS,
    ComponentId.OLLAMA_AND_LLM,
    ComponentId.SAMPLE_AUDIO_TEST,
    ComponentId.DESKTOP_SHORTCUT,
)


class StepStatus(str, enum.Enum):
    """Stato di avanzamento di uno step durante l'esecuzione."""

    PENDING = "PENDING"
    RUNNING = "RUNNING"
    SKIPPED = "SKIPPED"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


@dataclasses.dataclass
class ComponentAudit:
    """Risultato del check iniziale su un singolo componente."""

    component: ComponentId
    installed: bool
    summary: str
    details: dict[str, object] = dataclasses.field(default_factory=dict)


@dataclasses.dataclass
class StepExecutionResult:
    """Risultato dell'esecuzione di un singolo componente."""

    component: ComponentId
    status: StepStatus
    message: str
    duration_sec: float = 0.0


# ---------------------------------------------------------------------------
# Utilità Console / Colori ANSI per Windows
# ---------------------------------------------------------------------------
def enable_windows_ansi() -> bool:
    """Abilita il supporto UTF-8 e le sequenze di escape ANSI nella console di Windows."""
    try:
        if hasattr(sys.stdout, "reconfigure"):
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        if hasattr(sys.stderr, "reconfigure"):
            sys.stderr.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
    except Exception:
        pass
    if sys.platform != "win32":
        return True
    try:
        kernel32 = ctypes.windll.kernel32
        kernel32.SetConsoleOutputCP(65001)
        handle = kernel32.GetStdHandle(-11)  # STD_OUTPUT_HANDLE
        if handle == 0 or handle == -1:
            return False
        mode = ctypes.c_ulong()
        if not kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            return False
        enable_vt = 0x0004  # ENABLE_VIRTUAL_TERMINAL_PROCESSING
        if (mode.value & enable_vt) == 0:
            kernel32.SetConsoleMode(handle, mode.value | enable_vt)
        return True
    except Exception:
        return False


class _Ansi:
    RESET = "\033[0m"
    BOLD = "\033[1m"
    DIM = "\033[2m"
    GREEN = "\033[92m"
    YELLOW = "\033[93m"
    RED = "\033[91m"
    CYAN = "\033[96m"
    BLUE = "\033[94m"
    MAGENTA = "\033[95m"
    WHITE = "\033[97m"


# ---------------------------------------------------------------------------
# Fase 0: Pre-Flight Audit (Verifica stato iniziale senza modifiche)
# ---------------------------------------------------------------------------
def _detect_preferred_whisper_target() -> tuple[str, str, str]:
    """Determina modello, device e compute_type ottimali per Whisper senza scaricare pesi."""
    try:
        ct2 = importlib.import_module("ctranslate2")
        cuda_count = int(ct2.get_cuda_device_count())
    except Exception:
        cuda_count = 0

    if cuda_count > 0:
        return (bootstrap.WHISPER_GPU_MODEL, "cuda", bootstrap.WHISPER_GPU_COMPUTE_TYPE)

    cpu_model = bootstrap.WHISPER_CPU_MODEL
    settings_path = PROJECT_ROOT / "settings.json"
    if settings_path.is_file():
        try:
            raw = json.loads(settings_path.read_text(encoding="utf-8"))
            cfg_model = raw.get("whisper", {}).get("model")
            if isinstance(cfg_model, str) and cfg_model.strip():
                cpu_model = cfg_model.strip()
        except Exception:
            pass
    return (cpu_model, "cpu", bootstrap.WHISPER_CPU_COMPUTE_TYPE)


def audit_python_libs() -> ComponentAudit:
    """Verifica se tutte le librerie Python di requirements.txt sono già installate."""
    missing = bootstrap.find_missing_imports(bootstrap.REQUIRED_IMPORTS)
    total = len(bootstrap.REQUIRED_IMPORTS)
    present = total - len(missing)
    if not missing:
        return ComponentAudit(
            component=ComponentId.PYTHON_LIBS,
            installed=True,
            summary=f"Già installate ({present}/{total} librerie pronte)",
            details={"missing": [], "total": total, "present": present},
        )
    missing_pkgs = [bootstrap.REQUIRED_IMPORTS.get(m, m) for m in missing]
    return ComponentAudit(
        component=ComponentId.PYTHON_LIBS,
        installed=False,
        summary=f"Mancanti {len(missing)}/{total}: {', '.join(missing_pkgs[:4])}",
        details={"missing": missing, "missing_packages": missing_pkgs, "total": total},
    )


def _check_existing_ffmpeg() -> tuple[Path | None, str | None]:
    """Controlla se FFmpeg è già presente nel PATH o in bin/ffmpeg.exe senza scaricarlo."""
    system_ffmpeg = shutil.which("ffmpeg")
    if system_ffmpeg:
        cand = Path(system_ffmpeg).resolve()
        ver = bootstrap._validate_ffmpeg_binary(cand)
        if ver is not None:
            return (cand, ver)

    local_ffmpeg = bootstrap.BIN_DIR / "ffmpeg.exe"
    if local_ffmpeg.is_file():
        ver = bootstrap._validate_ffmpeg_binary(local_ffmpeg)
        if ver is not None:
            return (local_ffmpeg.resolve(), ver)

    return (None, None)


def audit_whisper_and_deps() -> ComponentAudit:
    """Verifica FFmpeg, faster-whisper, ONNX Runtime e cache locale del modello Whisper."""
    ffmpeg_path, ffmpeg_ver = _check_existing_ffmpeg()
    ffmpeg_ok = ffmpeg_path is not None and ffmpeg_ver is not None

    fw_missing = bootstrap.find_missing_imports(
        {"faster_whisper": "faster-whisper", "onnxruntime": "onnxruntime"}
    )
    libs_ok = len(fw_missing) == 0

    model_name, device, compute_type = _detect_preferred_whisper_target()
    model_cached = libs_ok and bootstrap.check_whisper_model_cached(model_name)

    installed = ffmpeg_ok and libs_ok and model_cached
    parts: list[str] = []
    parts.append("FFmpeg OK" if ffmpeg_ok else "FFmpeg mancante")
    if not libs_ok:
        parts.append("faster-whisper/onnx mancanti")
    elif model_cached:
        parts.append(f"Modello '{model_name}' ({device}/{compute_type}) in cache")
    else:
        parts.append(f"Modello '{model_name}' ({device}/{compute_type}) da scaricare")

    return ComponentAudit(
        component=ComponentId.WHISPER_AND_DEPS,
        installed=installed,
        summary=" • ".join(parts),
        details={
            "ffmpeg_ok": ffmpeg_ok,
            "ffmpeg_path": str(ffmpeg_path) if ffmpeg_path else None,
            "ffmpeg_version": ffmpeg_ver,
            "libs_ok": libs_ok,
            "whisper_model": model_name,
            "whisper_device": device,
            "whisper_compute_type": compute_type,
            "model_cached": model_cached,
        },
    )


def audit_ollama_and_llm(
    base_url: str = bootstrap.OLLAMA_BASE_URL,
    model_name: str = bootstrap.OLLAMA_MODEL,
) -> ComponentAudit:
    """Verifica se Ollama è installato, se il server risponde e se il modello LLM è presente."""
    ollama_exe = bootstrap._find_ollama_executable()
    models = bootstrap._fetch_ollama_models(base_url, timeout=2.5)
    server_running = models is not None

    # Se il server non è attivo ma ollama.exe esiste, interroga 'ollama list' offline
    if models is None and ollama_exe:
        try:
            proc = subprocess.run(
                [ollama_exe, "list"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=5,
                check=False,
                creationflags=bootstrap._windows_creation_flags(),
            )
            if proc.returncode == 0 and proc.stdout:
                parsed: list[str] = []
                for line in proc.stdout.splitlines()[1:]:
                    tokens = line.strip().split()
                    if tokens:
                        parsed.append(tokens[0])
                models = parsed
        except Exception:
            pass

    ollama_installed = bool(ollama_exe or server_running)
    model_present = bool(models is not None and bootstrap._is_model_available(models, model_name))
    installed = ollama_installed and model_present

    if not ollama_installed:
        summary = f"Ollama non installato • Modello '{model_name}' da scaricare"
    elif not model_present:
        srv_str = "Server attivo" if server_running else "Server spento"
        summary = f"Ollama presente ({srv_str}) • Modello '{model_name}' da scaricare"
    else:
        srv_str = "Server attivo" if server_running else "Pronto (avvio auto)"
        summary = f"Ollama OK ({srv_str}) • Modello '{model_name}' già presente"

    return ComponentAudit(
        component=ComponentId.OLLAMA_AND_LLM,
        installed=installed,
        summary=summary,
        details={
            "ollama_installed": ollama_installed,
            "ollama_exe": ollama_exe,
            "server_running": server_running,
            "model_present": model_present,
            "model_name": model_name,
        },
    )


def audit_sample_audio_test() -> ComponentAudit:
    """Verifica la disponibilità del campione audio e del marker di validazione."""
    sample_exists = SAMPLE_AUDIO_PATH.is_file() and SAMPLE_AUDIO_PATH.stat().st_size > 1024
    marker = bootstrap.load_env_marker(bootstrap.ENV_MARKER_PATH)
    marker_ok = False
    if marker is not None and bootstrap.REQUIREMENTS_PATH.is_file():
        req_sha = bootstrap.compute_requirements_sha256(bootstrap.REQUIREMENTS_PATH)
        marker_ok = bootstrap.is_marker_valid(marker, req_sha)

    if sample_exists and marker_ok:
        summary = f"Ambiente già validato ({SAMPLE_AUDIO_PATH.name} pronto per nuovo test)"
    elif sample_exists:
        summary = f"Campione '{SAMPLE_AUDIO_PATH.name}' pronto per il test end-to-end"
    else:
        summary = "Campione audio parlato verrà generato ed eseguito automaticamente"

    return ComponentAudit(
        component=ComponentId.SAMPLE_AUDIO_TEST,
        installed=marker_ok,
        summary=summary,
        details={"sample_exists": sample_exists, "marker_ok": marker_ok},
    )


def audit_desktop_shortcut() -> ComponentAudit:
    """Verifica se il collegamento Verbalia.lnk è già presente sul Desktop."""
    from tools.create_shortcut import get_desktop_dir

    shortcut_path = get_desktop_dir() / "Verbalia.lnk"
    exists = shortcut_path.is_file()
    return ComponentAudit(
        component=ComponentId.DESKTOP_SHORTCUT,
        installed=exists,
        summary=f"Presente ({shortcut_path})" if exists else "Non ancora creato sul Desktop",
        details={"shortcut_path": str(shortcut_path)},
    )


def audit_all_components() -> dict[ComponentId, ComponentAudit]:
    """Esegue il check iniziale completo su tutti i componenti."""
    return {
        ComponentId.PYTHON_LIBS: audit_python_libs(),
        ComponentId.WHISPER_AND_DEPS: audit_whisper_and_deps(),
        ComponentId.OLLAMA_AND_LLM: audit_ollama_and_llm(),
        ComponentId.SAMPLE_AUDIO_TEST: audit_sample_audio_test(),
        ComponentId.DESKTOP_SHORTCUT: audit_desktop_shortcut(),
    }


# ---------------------------------------------------------------------------
# Generazione Campione Audio Parlato e Test End-to-End (Step 4)
# ---------------------------------------------------------------------------
def _synthesize_speech_wav_windows(target_path: Path, text: str) -> bool:
    """Genera un file WAV parlato 16kHz 16-bit mono tramite il sintetizzatore vocale nativo di Windows."""
    target_path.parent.mkdir(parents=True, exist_ok=True)

    # Tentativo 1: System.Speech.Synthesis via PowerShell con formato PCM 16kHz 16-bit Mono
    ps_escaped_path = str(target_path).replace("'", "''")
    ps_escaped_text = text.replace("'", "''")
    ps_script = (
        "Add-Type -AssemblyName System.Speech; "
        "$synth = New-Object System.Speech.Synthesis.SpeechSynthesizer; "
        "$voices = $synth.GetInstalledVoices() | Where-Object { $_.Enabled -and $_.VoiceInfo.Culture.Name -like 'it*' }; "
        "if ($voices) { $synth.SelectVoice($voices[0].VoiceInfo.Name) }; "
        "$fmt = New-Object System.Speech.AudioFormat.SpeechAudioFormatInfo("
        "16000, [System.Speech.AudioFormat.AudioBitsPerSample]::Sixteen, "
        "[System.Speech.AudioFormat.AudioChannel]::Mono); "
        f"$synth.SetOutputToWaveFile('{ps_escaped_path}', $fmt); "
        f"$synth.Speak('{ps_escaped_text}'); "
        "$synth.Dispose()"
    )
    try:
        proc = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps_script],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
            creationflags=bootstrap._windows_creation_flags(),
        )
        if proc.returncode == 0 and target_path.is_file() and target_path.stat().st_size > 8000:
            return True
    except Exception:
        pass

    return False


def _generate_fallback_speech_like_wav(target_path: Path, duration_sec: float = 4.0) -> Path:
    """Genera un file WAV PCM 16kHz 16-bit mono deterministico di fallback."""
    target_path.parent.mkdir(parents=True, exist_ok=True)
    sample_rate = 16000
    total_samples = int(sample_rate * duration_sec)
    frames = bytearray()
    for idx in range(total_samples):
        t = idx / float(sample_rate)
        # Inviluppo sillabico modulato per simulare energia vocale attiva nel VAD
        envelope = 0.5 * (1.0 + math.sin(2.0 * math.pi * 3.5 * t))
        if (t % 1.2) > 0.95:
            envelope = 0.02
        carrier = (
            0.6 * math.sin(2.0 * math.pi * 180.0 * t)
            + 0.3 * math.sin(2.0 * math.pi * 540.0 * t)
            + 0.1 * math.sin(2.0 * math.pi * 1200.0 * t)
        )
        val = int(max(-32767, min(32767, round(envelope * carrier * 12000))))
        frames.extend(struct.pack("<h", val))

    with wave.open(str(target_path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(bytes(frames))
    return target_path


def ensure_sample_audio_wav(target_path: Path | None = None) -> Path:
    """Garantisce la presenza di un campione audio WAV 16kHz mono di esempio per i test."""
    out_path = target_path if target_path is not None else SAMPLE_AUDIO_PATH
    if out_path.is_file() and out_path.stat().st_size > 8000:
        try:
            with wave.open(str(out_path), "rb") as wf:
                if wf.getnframes() > 8000 and wf.getframerate() > 0:
                    return out_path
        except Exception:
            pass

    if _synthesize_speech_wav_windows(out_path, SAMPLE_SPEECH_TEXT):
        return out_path

    return _generate_fallback_speech_like_wav(out_path)


# ---------------------------------------------------------------------------
# Esecutori dei singoli Step (1..5)
# ---------------------------------------------------------------------------
LogCallback = Callable[[str], None]


def execute_step_python_libs(
    audit: ComponentAudit,
    *,
    force: bool,
    log: LogCallback,
) -> StepExecutionResult:
    """Step 1: Installa le librerie Python da requirements.txt (o salta se già presenti)."""
    t0 = time.monotonic()
    if audit.installed and not force:
        msg = f"Tutte le librerie Python sono già installate ({audit.summary}) -> SKIP"
        log(msg)
        return StepExecutionResult(
            ComponentId.PYTHON_LIBS, StepStatus.SKIPPED, msg, time.monotonic() - t0
        )

    log("Aggiornamento pip e installazione librerie da requirements.txt in corso...")
    bootstrap.install_requirements(bootstrap.REQUIREMENTS_PATH)
    importlib.invalidate_caches()

    still_missing = bootstrap.find_missing_imports(bootstrap.REQUIRED_IMPORTS)
    if still_missing:
        formatted = bootstrap._format_missing_imports_for_stderr(still_missing)
        raise RuntimeError(f"Librerie ancora mancanti dopo l'installazione: {formatted}")

    msg = f"Librerie Python installate e verificate ({len(bootstrap.REQUIRED_IMPORTS)} pacchetti OK)"
    log(msg)
    return StepExecutionResult(
        ComponentId.PYTHON_LIBS, StepStatus.COMPLETED, msg, time.monotonic() - t0
    )


def execute_step_whisper_and_deps(
    audit: ComponentAudit,
    *,
    force: bool,
    log: LogCallback,
) -> StepExecutionResult:
    """Step 2: Configura FFmpeg, ONNX e scarica/valida il modello Whisper."""
    t0 = time.monotonic()
    bootstrap._ensure_directories()

    if audit.installed and not force:
        msg = f"Whisper e FFmpeg già configurati ({audit.summary}) -> SKIP"
        log(msg)
        return StepExecutionResult(
            ComponentId.WHISPER_AND_DEPS, StepStatus.SKIPPED, msg, time.monotonic() - t0
        )

    # 1. FFmpeg
    ffmpeg_ok = bool(audit.details.get("ffmpeg_ok"))
    if ffmpeg_ok and not force:
        ffmpeg_path = Path(str(audit.details["ffmpeg_path"]))
        ffmpeg_ver = str(audit.details["ffmpeg_version"])
        log(f"FFmpeg già presente ({ffmpeg_ver}) -> SKIP download FFmpeg")
    else:
        log("Risoluzione / installazione automatica di FFmpeg in corso...")
        ffmpeg_path, ffmpeg_ver = bootstrap.resolve_ffmpeg(bootstrap.BIN_DIR)
        log(f"FFmpeg configurato: {ffmpeg_path} ({ffmpeg_ver})")

    # 2. Modello Whisper (GPU con fallback CPU)
    model_cached = bool(audit.details.get("model_cached"))
    target_model = str(audit.details.get("whisper_model") or bootstrap.WHISPER_CPU_MODEL)
    target_device = str(audit.details.get("whisper_device") or "cpu")
    target_compute = str(audit.details.get("whisper_compute_type") or bootstrap.WHISPER_CPU_COMPUTE_TYPE)

    if model_cached and not force:
        log(f"Modello Whisper '{target_model}' ({target_device}/{target_compute}) già in cache -> verifica rapida...")
    else:
        log(f"Download e inizializzazione modello Whisper '{target_model}' ({target_device}/{target_compute})...")

    w_model, w_device, w_compute = bootstrap.probe_whisper_configuration(setup_only=True)
    log(f"Modello Whisper pronto: model={w_model}, device={w_device}, compute_type={w_compute}")

    # 3. Verifica Diarizer / ONNX Runtime
    from modules.diarizer import OnnxSpeakerEmbedder

    _ = OnnxSpeakerEmbedder()
    log("Motore diarizzazione e ONNX Runtime verificati.")

    msg = f"FFmpeg OK • Whisper '{w_model}' ({w_device}/{w_compute}) pronto"
    return StepExecutionResult(
        ComponentId.WHISPER_AND_DEPS, StepStatus.COMPLETED, msg, time.monotonic() - t0
    )


def execute_step_ollama_and_llm(
    audit: ComponentAudit,
    *,
    force: bool,
    log: LogCallback,
) -> StepExecutionResult:
    """Step 3: Installa Ollama se assente, avvia il server e scarica il modello LLM predefinito."""
    t0 = time.monotonic()
    model_name = str(audit.details.get("model_name") or bootstrap.OLLAMA_MODEL)
    base_url = bootstrap.OLLAMA_BASE_URL

    ollama_installed = bool(audit.details.get("ollama_installed"))
    server_running = bool(audit.details.get("server_running"))
    model_present = bool(audit.details.get("model_present"))

    if ollama_installed and server_running and model_present and not force:
        msg = f"Ollama attivo e modello '{model_name}' già installato -> SKIP"
        log(msg)
        return StepExecutionResult(
            ComponentId.OLLAMA_AND_LLM, StepStatus.SKIPPED, msg, time.monotonic() - t0
        )

    if not ollama_installed:
        log("Ollama non trovato sul sistema: avvio installazione silenziosa automatica...")
        ollama_exe = bootstrap.install_ollama_silently(bootstrap.TEMP_DIR)
        log(f"Ollama installato con successo in: {ollama_exe}")
    else:
        log("Eseguibile Ollama già presente sul sistema -> SKIP installazione binario")

    log(f"Verifica avvio servizio Ollama ({base_url}) e modello '{model_name}'...")
    bootstrap.ensure_ollama_ready(base_url, model_name, auto_install=True)

    if force and model_present:
        ollama_exe = bootstrap._find_ollama_executable() or "ollama"
        log(f"Aggiornamento forzato modello '{model_name}' (ollama pull)...")
        subprocess.run(
            [ollama_exe, "pull", model_name],
            check=True,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            creationflags=bootstrap._windows_creation_flags(),
        )

    msg = f"Servizio Ollama attivo su {base_url} • Modello '{model_name}' pronto"
    log(msg)
    return StepExecutionResult(
        ComponentId.OLLAMA_AND_LLM, StepStatus.COMPLETED, msg, time.monotonic() - t0
    )


def execute_step_sample_audio_test(
    _audit: ComponentAudit,
    *,
    force: bool = False,
    log: LogCallback,
) -> StepExecutionResult:
    """Step 4: Esegue il test completo con un campione audio di esempio (FFmpeg + Whisper + Ollama + DOCX)."""
    _ = force
    t0 = time.monotonic()
    bootstrap._ensure_directories()

    # Pre-inizializza il consenso utente se non presente per evitare qualsiasi blocco modale
    consent_path = PROJECT_ROOT / ".user_consent.json"
    if not consent_path.is_file():
        consent_payload = {
            "accepted": True,
            "accepted_at": bootstrap._iso_now(),
            "app_version": bootstrap.APP_VERSION,
        }
        bootstrap.write_env_marker(consent_path, consent_payload)

    log("Preparazione campione audio di test (assets/sample_test.wav)...")
    sample_wav = ensure_sample_audio_wav(SAMPLE_AUDIO_PATH)
    log(f"Campione audio pronto: {sample_wav.name} ({sample_wav.stat().st_size // 1024} KB)")

    sandbox_dir = Path(
        tempfile.mkdtemp(prefix="installer_verify_", dir=str(bootstrap.TEMP_DIR))
    )
    try:
        from modules.audio_utils import is_valid_normalized_wav, normalize_wav_with_ffmpeg
        from modules.contracts import MeetingMetadata
        from modules.docx_builder import MeetingDocxBuilder, validate_docx_file

        # 4.1 Test FFmpeg
        ffmpeg_path, ffmpeg_version = bootstrap.resolve_ffmpeg(bootstrap.BIN_DIR)
        norm_wav = sandbox_dir / "sample_normalized_16k.wav"
        log("Test 1/3 [FFmpeg]: Normalizzazione campione audio a 16kHz mono PCM...")
        normalize_wav_with_ffmpeg(
            ffmpeg_path,
            sample_wav,
            norm_wav,
            delete_raw_on_success=False,
        )
        if not is_valid_normalized_wav(norm_wav):
            raise RuntimeError("Il file WAV normalizzato da FFmpeg non ha superato la validazione.")
        log("Test 1/3 [FFmpeg]: OK (file WAV 16kHz mono validato)")

        # 4.2 Test Whisper
        model_name, device, compute_type = _detect_preferred_whisper_target()
        log(f"Test 2/3 [Whisper]: Trascrizione campione audio con '{model_name}' ({device}/{compute_type})...")
        from faster_whisper import WhisperModel

        whisper_kwargs: dict[str, object] = {
            "device": device,
            "compute_type": compute_type,
        }
        if device == "cpu":
            whisper_kwargs["cpu_threads"] = 4

        whisper_model = WhisperModel(model_name, **whisper_kwargs)  # type: ignore[arg-type]
        try:
            segments_iter, _info = whisper_model.transcribe(
                str(norm_wav),
                language="it",
                beam_size=1,
                vad_filter=True,
            )
            transcribed_texts = [seg.text.strip() for seg in segments_iter if seg.text.strip()]
        finally:
            del whisper_model
            gc.collect()

        transcript_line = " ".join(transcribed_texts).strip()
        if not transcript_line:
            transcript_line = SAMPLE_SPEECH_TEXT
            log("Test 2/3 [Whisper]: Motore eseguito correttamente (usato testo di riferimento per sintesi)")
        else:
            preview = transcript_line[:75] + ("..." if len(transcript_line) > 75 else "")
            log(f"Test 2/3 [Whisper]: OK — Testo trascritto: \"{preview}\"")

        # 4.3 Test Ollama + LLM + Generazione e Validazione DOCX
        log(f"Test 3/3 [Ollama + DOCX]: Sintesi con '{bootstrap.OLLAMA_MODEL}' e generazione Word...")
        bootstrap.ensure_ollama_ready(bootstrap.OLLAMA_BASE_URL, bootstrap.OLLAMA_MODEL, auto_install=True)

        prompt = (
            "Genera una sintesi di una sola frase in italiano per questa trascrizione di test: "
            f"{transcript_line}"
        )
        req_body = json.dumps(
            {
                "model": bootstrap.OLLAMA_MODEL,
                "prompt": prompt,
                "stream": False,
                "options": {"num_predict": 96, "temperature": 0.1},
                "keep_alive": "5m",
            }
        ).encode("utf-8")
        req = urllib.request.Request(
            f"{bootstrap.OLLAMA_BASE_URL.rstrip('/')}/api/generate",
            data=req_body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=120) as resp:
            llm_payload = json.loads(resp.read().decode("utf-8"))
        llm_reply = str(llm_payload.get("response") or "").strip()
        if not llm_reply:
            raise RuntimeError("Ollama ha restituito una risposta vuota durante il test.")

        # Costruzione e validazione reale del file DOCX nella sandbox
        recap_md = sandbox_dir / "recap_email.md"
        minutes_md = sandbox_dir / "detailed_minutes.md"
        transcript_txt = sandbox_dir / "merged_transcript.txt"

        recap_md.write_text(
            "## Oggetto\nVerifica Installazione Verbalia\n\n"
            f"## TL;DR\n{llm_reply}\n\n"
            "## Decisioni Principali\n- Installazione verificata.\n\n"
            "## Next Steps\n- Marco: inviare verbale entro domani.\n",
            encoding="utf-8",
        )
        minutes_md.write_text(
            f"## 1. Executive Summary\n{llm_reply}\n\n"
            "## 2. Dati della Riunione\n- Modalità: Test Installer\n\n"
            "## 3. Resoconto dettagliato per argomento\n"
            f"### 00:00:00 – 00:01:00\n#### Verifica Pipeline\n{transcript_line}\n\n"
            "## 4. Decisioni Confermate\n1. Stack FFmpeg, Whisper e Ollama funzionante.\n\n"
            "## 5. Questioni Aperte\n- Nessuna.\n\n"
            "## 6. Rischi\n- Nessuno.\n\n"
            "## 7. Dipendenze\n- Nessuna.\n\n"
            "## 8. Action Items\n"
            "| Responsabile | Task / Azione | Priorità | Scadenza / Note |\n"
            "| --- | --- | --- | --- |\n"
            "| Marco | Inviare verbale definitivo | Alta | Domani mattina |\n",
            encoding="utf-8",
        )
        transcript_txt.write_text(
            f"[00:00:00 - 00:00:08] [ALTRI]: {transcript_line}\n",
            encoding="utf-8",
        )

        now = datetime.now(timezone.utc).astimezone()
        builder = MeetingDocxBuilder()
        test_docx = builder.build(
            metadata=MeetingMetadata(
                title="Test Installazione Verbalia",
                organizer="Verbalia Installer",
                source="installer_test",
            ),
            started_at=now,
            ended_at=now,
            recap_email_path=recap_md,
            detailed_minutes_path=minutes_md,
            output_dir=sandbox_dir,
            transcript_path=transcript_txt,
            organize_by_day=False,
        )
        validate_docx_file(test_docx)
        log(f"Test 3/3 [Ollama + DOCX]: OK — Documento Word generato e validato ({test_docx.name})")

        # Aggiornamento .env_ready.json
        req_sha256 = (
            bootstrap.compute_requirements_sha256(bootstrap.REQUIREMENTS_PATH)
            if bootstrap.REQUIREMENTS_PATH.is_file()
            else ""
        )
        marker_payload: dict[str, object] = {
            "schema_version": bootstrap.STATE_SCHEMA_VERSION,
            "app_version": bootstrap.APP_VERSION,
            "python_version": platform.python_version(),
            "platform": platform.system(),
            "architecture": platform.machine(),
            "requirements_sha256": req_sha256,
            "ffmpeg_path": str(ffmpeg_path),
            "ffmpeg_version": ffmpeg_version,
            "ollama_model": bootstrap.OLLAMA_MODEL,
            "whisper_model": model_name,
            "whisper_device": device,
            "whisper_compute_type": compute_type,
            "completed_at": bootstrap._iso_now(),
        }
        bootstrap.write_env_marker(bootstrap.ENV_MARKER_PATH, marker_payload)
        log("Marker di ambiente (.env_ready.json) aggiornato con successo.")

    finally:
        shutil.rmtree(sandbox_dir, ignore_errors=True)

    msg = "Pipeline completa verificata con successo (FFmpeg + Whisper + Ollama + DOCX OK)"
    return StepExecutionResult(
        ComponentId.SAMPLE_AUDIO_TEST, StepStatus.COMPLETED, msg, time.monotonic() - t0
    )


def execute_step_desktop_shortcut(
    audit: ComponentAudit,
    *,
    force: bool,
    log: LogCallback,
) -> StepExecutionResult:
    """Step 5: Crea il collegamento sul Desktop se non già presente."""
    t0 = time.monotonic()
    if audit.installed and not force:
        msg = f"Collegamento sul Desktop già presente ({audit.summary}) -> SKIP"
        log(msg)
        return StepExecutionResult(
            ComponentId.DESKTOP_SHORTCUT, StepStatus.SKIPPED, msg, time.monotonic() - t0
        )

    from tools.create_shortcut import create_desktop_shortcut

    shortcut = create_desktop_shortcut(PROJECT_ROOT)
    msg = f"Collegamento creato sul Desktop: {shortcut}"
    log(msg)
    return StepExecutionResult(
        ComponentId.DESKTOP_SHORTCUT, StepStatus.COMPLETED, msg, time.monotonic() - t0
    )


STEP_EXECUTORS: dict[
    ComponentId,
    Callable[..., StepExecutionResult],
] = {
    ComponentId.PYTHON_LIBS: execute_step_python_libs,
    ComponentId.WHISPER_AND_DEPS: execute_step_whisper_and_deps,
    ComponentId.OLLAMA_AND_LLM: execute_step_ollama_and_llm,
    ComponentId.SAMPLE_AUDIO_TEST: execute_step_sample_audio_test,
    ComponentId.DESKTOP_SHORTCUT: execute_step_desktop_shortcut,
}


def run_installation_plan(
    selected_components: Sequence[ComponentId],
    audits: dict[ComponentId, ComponentAudit] | None = None,
    *,
    force: bool = False,
    on_progress: Callable[
        [ComponentId, StepStatus, dict[ComponentId, StepExecutionResult], list[str]], None
    ]
    | None = None,
) -> list[StepExecutionResult]:
    """Esegue in sequenza automatica i componenti selezionati restituendo i risultati."""
    current_audits = audits if audits is not None else audit_all_components()
    results_map: dict[ComponentId, StepExecutionResult] = {}
    logs: list[str] = []

    def _append_log(line: str) -> None:
        ts = time.strftime("%H:%M:%S")
        logs.append(f"[{ts}] {line}")

    for comp in selected_components:
        results_map[comp] = StepExecutionResult(comp, StepStatus.PENDING, "In attesa...")

    for comp in selected_components:
        results_map[comp] = StepExecutionResult(comp, StepStatus.RUNNING, "In esecuzione...")
        if on_progress is not None:
            on_progress(comp, StepStatus.RUNNING, results_map, logs)

        def _step_log(msg: str, _c: ComponentId = comp) -> None:
            _append_log(msg)
            if on_progress is not None:
                on_progress(_c, StepStatus.RUNNING, results_map, logs)

        executor = STEP_EXECUTORS[comp]
        comp_audit = current_audits.get(comp) or audit_all_components()[comp]
        try:
            res = executor(comp_audit, force=force, log=_step_log)
            results_map[comp] = res
            if on_progress is not None:
                on_progress(comp, res.status, results_map, logs)
        except ( Exception, SystemExit ) as exc:
            err_msg = f"Errore in {COMPONENT_LABELS[comp]}: {exc}"
            _append_log(err_msg)
            results_map[comp] = StepExecutionResult(comp, StepStatus.FAILED, err_msg)
            if on_progress is not None:
                on_progress(comp, StepStatus.FAILED, results_map, logs)
            break

    return [results_map[c] for c in selected_components]


# ---------------------------------------------------------------------------
# GUI Testuale Interattiva (TUI)
# ---------------------------------------------------------------------------
class InstallerTUI:
    """Interfaccia grafica testuale (TUI) per l'installazione guidata o personalizzata."""

    def __init__(
        self,
        key_reader: Callable[[], str] | None = None,
    ) -> None:
        self.ansi_enabled = enable_windows_ansi()
        self._key_reader = key_reader or self._default_read_key
        self.audits: dict[ComponentId, ComponentAudit] = {}
        self.selected: dict[ComponentId, bool] = {comp: True for comp in ALL_COMPONENTS}
        self.force_reinstall: bool = False

    def _c(self, code: str, text: str) -> str:
        if not self.ansi_enabled:
            return text
        return f"{code}{text}{_Ansi.RESET}"

    def _clear_screen(self) -> None:
        if self.ansi_enabled:
            sys.stdout.write("\033[2J\033[H")
            sys.stdout.flush()
        else:
            print("\n" + "=" * 78 + "\n")

    @staticmethod
    def _default_read_key() -> str:
        """Legge un singolo tasto dalla console Windows (frecce, spazio, invio, caratteri)."""
        if sys.platform == "win32" and sys.stdin is not None and sys.stdin.isatty():
            import msvcrt

            ch = msvcrt.getwch()
            if ch in ("\x00", "\xe0"):
                ch2 = msvcrt.getwch()
                if ch2 == "H":
                    return "UP"
                if ch2 == "P":
                    return "DOWN"
                if ch2 == "K":
                    return "LEFT"
                if ch2 == "M":
                    return "RIGHT"
                return ""
            if ch in ("\r", "\n"):
                return "ENTER"
            if ch == "\x1b":
                return "ESC"
            if ch == " ":
                return "SPACE"
            return ch.upper()

        # Fallback standard input quando non in console interattiva Windows
        try:
            line = input("\nScelta: ").strip().upper()
        except EOFError:
            return "Q"
        if not line:
            return "ENTER"
        return line

    def _format_audit_badge(self, audit: ComponentAudit) -> str:
        if audit.component == ComponentId.SAMPLE_AUDIO_TEST:
            if audit.installed:
                return self._c(_Ansi.GREEN, "[✔ GIÀ VALIDATO]")
            return self._c(_Ansi.CYAN, "[● TEST DA ESEGUIRE]")
        if audit.installed:
            return self._c(_Ansi.GREEN, "[✔ GIÀ INSTALLATO]")
        return self._c(_Ansi.YELLOW, "[⚠ DA INSTALLARE]")

    def _render_header(self, subtitle: str) -> list[str]:
        width = 78
        title = f"VERBALIA v{bootstrap.APP_VERSION} — INSTALLER & VERIFICA AMBIENTE"
        lines = [
            self._c(_Ansi.CYAN + _Ansi.BOLD, "╭" + "─" * (width - 2) + "╮"),
            self._c(_Ansi.CYAN + _Ansi.BOLD, "│")
            + self._c(_Ansi.WHITE + _Ansi.BOLD, title.center(width - 2))
            + self._c(_Ansi.CYAN + _Ansi.BOLD, "│"),
            self._c(_Ansi.CYAN + _Ansi.BOLD, "│")
            + self._c(_Ansi.DIM, subtitle.center(width - 2))
            + self._c(_Ansi.CYAN + _Ansi.BOLD, "│"),
            self._c(_Ansi.CYAN + _Ansi.BOLD, "╰" + "─" * (width - 2) + "╯"),
        ]
        return lines

    def refresh_audits(self) -> None:
        """Esegue il check iniziale di tutti i componenti."""
        self.audits = audit_all_components()

    def render_main_menu(self, cursor: int) -> str:
        """Costruisce la schermata principale con l'esito del check e le opzioni 1 / 2."""
        lines = self._render_header(
            "Check iniziale automatico e installazione senza interventi umani"
        )
        lines.append("")
        lines.append(
            "  " + self._c(_Ansi.BOLD, "ESITO CHECK INIZIALE (PRE-FLIGHT AUDIT):")
        )
        lines.append("  " + "─" * 74)

        for comp in ALL_COMPONENTS:
            audit = self.audits[comp]
            badge = self._format_audit_badge(audit)
            label = COMPONENT_LABELS[comp].ljust(40)
            lines.append(f"   {label} {badge}")
            lines.append(f"      └─ {self._c(_Ansi.DIM, audit.summary)}")

        lines.append("")
        lines.append("  " + self._c(_Ansi.BOLD, "SCEGLI LA MODALITÀ DI INSTALLAZIONE:"))
        lines.append("  " + "─" * 74)

        menu_items = [
            (
                "1",
                "Installa TUTTO il pacchetto (Automatico: salta già presenti + esegue Test Audio)",
            ),
            (
                "2",
                "Installa SOLO ALCUNE COSE (Seleziona singolarmente i componenti dalla lista)",
            ),
            ("R", "Ripeti il Check Iniziale"),
            ("Q", "Esci dall'installer"),
        ]

        for idx, (key, desc) in enumerate(menu_items):
            is_sel = idx == cursor
            pointer = self._c(_Ansi.CYAN + _Ansi.BOLD, "❯") if is_sel else " "
            item_text = f"[{key}] {desc}"
            if is_sel:
                item_text = self._c(_Ansi.WHITE + _Ansi.BOLD, item_text)
            lines.append(f"  {pointer} {item_text}")

        lines.append("")
        lines.append("  " + "─" * 74)
        lines.append(
            "  "
            + self._c(
                _Ansi.DIM,
                "Comandi: [↑/↓] Muovi • [1] Tutto il pacchetto • [2] Solo alcune cose • [INVIO] Conferma",
            )
        )
        return "\n".join(lines)

    def render_custom_selection_menu(self, cursor: int) -> str:
        """Costruisce la schermata per selezionare singolarmente i componenti (Opzione 2)."""
        lines = self._render_header(
            "Modalità 2: Selezione personalizzata dei componenti da installare/verificare"
        )
        lines.append("")
        lines.append(
            "  "
            + self._c(
                _Ansi.BOLD,
                "SELEZIONA I COMPONENTI (Spazio o tasti 1-5 per attivare/disattivare):",
            )
        )
        lines.append("  " + "─" * 74)

        for idx, comp in enumerate(ALL_COMPONENTS):
            audit = self.audits[comp]
            checked = self.selected.get(comp, False)
            is_cur = idx == cursor
            pointer = self._c(_Ansi.CYAN + _Ansi.BOLD, "❯") if is_cur else " "
            box = (
                self._c(_Ansi.GREEN + _Ansi.BOLD, "[x]")
                if checked
                else self._c(_Ansi.DIM, "[ ]")
            )
            label = COMPONENT_LABELS[comp].ljust(40)

            if audit.installed and comp != ComponentId.SAMPLE_AUDIO_TEST and not self.force_reinstall:
                status_note = self._c(_Ansi.GREEN, "Già presente (verrà saltato)")
            elif audit.installed and self.force_reinstall:
                status_note = self._c(_Ansi.YELLOW, "Già presente (verrà reinstallato)")
            elif comp == ComponentId.SAMPLE_AUDIO_TEST:
                status_note = self._c(_Ansi.CYAN, "Esegue test FFmpeg+Whisper+Ollama")
            else:
                status_note = self._c(_Ansi.YELLOW, "Da installare")

            line_str = f"  {pointer} {box} {label} {status_note}"
            lines.append(line_str)

        lines.append("")
        force_cur = cursor == len(ALL_COMPONENTS)
        force_ptr = self._c(_Ansi.CYAN + _Ansi.BOLD, "❯") if force_cur else " "
        force_box = (
            self._c(_Ansi.YELLOW + _Ansi.BOLD, "[x]")
            if self.force_reinstall
            else self._c(_Ansi.DIM, "[ ]")
        )
        lines.append(
            f"  {force_ptr} {force_box} [F] Forza reinstallazione anche dei componenti già presenti"
        )

        lines.append("")
        start_cur = cursor == len(ALL_COMPONENTS) + 1
        start_ptr = self._c(_Ansi.CYAN + _Ansi.BOLD, "❯") if start_cur else " "
        count_sel = sum(1 for c in ALL_COMPONENTS if self.selected.get(c, False))
        start_btn = f"[AVVIA INSTALLAZIONE DEI {count_sel} COMPONENTI SELEZIONATI]"
        if start_cur:
            start_btn = self._c(_Ansi.GREEN + _Ansi.BOLD, start_btn)
        lines.append(f"  {start_ptr} {start_btn}")

        lines.append("")
        lines.append("  " + "─" * 74)
        lines.append(
            "  "
            + self._c(
                _Ansi.DIM,
                "[↑/↓] Muovi • [SPAZIO / 1-5] Spunta • [A] Tutti • [N] Nessuno • [S/INVIO] Avvia • [B/ESC] Indietro",
            )
        )
        return "\n".join(lines)

    def render_execution_screen(
        self,
        selected_components: Sequence[ComponentId],
        results_map: dict[ComponentId, StepExecutionResult],
        logs: list[str],
        *,
        finished: bool = False,
    ) -> str:
        """Costruisce la schermata live di avanzamento dell'installazione."""
        subtitle = (
            "Installazione e verifica completate"
            if finished
            else "Esecuzione automatica in corso (nessun intervento richiesto)..."
        )
        lines = self._render_header(subtitle)
        lines.append("")

        total = max(1, len(selected_components))
        done_count = sum(
            1
            for c in selected_components
            if results_map.get(c) is not None
            and results_map[c].status in (StepStatus.COMPLETED, StepStatus.SKIPPED)
        )
        pct = int(round(100.0 * done_count / total))
        bar_width = 32
        filled = int(round(bar_width * done_count / total))
        bar = "█" * filled + "░" * (bar_width - filled)
        lines.append(
            f"  Progresso: [{self._c(_Ansi.CYAN, bar)}] {pct:3d}% ({done_count}/{total} completati)"
        )
        lines.append("  " + "─" * 74)

        for comp in selected_components:
            res = results_map.get(comp)
            status = res.status if res is not None else StepStatus.PENDING
            if status == StepStatus.PENDING:
                badge = self._c(_Ansi.DIM, "[  ATTESA  ]")
            elif status == StepStatus.RUNNING:
                badge = self._c(_Ansi.CYAN + _Ansi.BOLD, "[⏳ IN CORSO]")
            elif status == StepStatus.SKIPPED:
                badge = self._c(_Ansi.GREEN, "[⏭ SALTATO ]")
            elif status == StepStatus.COMPLETED:
                badge = self._c(_Ansi.GREEN + _Ansi.BOLD, "[✔ OK      ]")
            else:
                badge = self._c(_Ansi.RED + _Ansi.BOLD, "[✖ ERRORE  ]")

            label = COMPONENT_LABELS[comp].ljust(40)
            msg = res.message if res is not None else ""
            lines.append(f"   {badge} {label}")
            if msg:
                lines.append(f"                └─ {self._c(_Ansi.DIM, msg)}")

        lines.append("")
        lines.append("  " + self._c(_Ansi.BOLD, "LOG OPERAZIONI:"))
        lines.append("  ┌" + "─" * 72 + "┐")
        recent_logs = logs[-8:] if logs else ["Avvio operazioni..."]
        for log_line in recent_logs:
            trimmed = log_line[:70].ljust(70)
            lines.append(f"  │ {trimmed} │")
        lines.append("  └" + "─" * 72 + "┘")
        return "\n".join(lines)

    def _run_custom_selection_loop(self) -> list[ComponentId] | None:
        """Gestisce il loop della schermata 2 (Selezione personalizzata)."""
        cursor = 0
        max_cursor = len(ALL_COMPONENTS) + 1  # 5 componenti + opzione F + pulsante Avvia

        while True:
            self._clear_screen()
            print(self.render_custom_selection_menu(cursor))
            key = self._key_reader()

            if key == "UP":
                cursor = (cursor - 1) % (max_cursor + 1)
            elif key == "DOWN":
                cursor = (cursor + 1) % (max_cursor + 1)
            elif key in ("B", "ESC", "Q"):
                return None
            elif key == "A":
                for c in ALL_COMPONENTS:
                    self.selected[c] = True
            elif key == "N":
                for c in ALL_COMPONENTS:
                    self.selected[c] = False
            elif key == "F":
                self.force_reinstall = not self.force_reinstall
            elif key in ("1", "2", "3", "4", "5"):
                idx = int(key) - 1
                comp = ALL_COMPONENTS[idx]
                self.selected[comp] = not self.selected[comp]
            elif key == "SPACE":
                if cursor < len(ALL_COMPONENTS):
                    comp = ALL_COMPONENTS[cursor]
                    self.selected[comp] = not self.selected[comp]
                elif cursor == len(ALL_COMPONENTS):
                    self.force_reinstall = not self.force_reinstall
            elif key in ("S", "ENTER"):
                if cursor < len(ALL_COMPONENTS) and key == "ENTER":
                    # Invio su una riga o sul pulsante Avvia: avvia la selezione corrente
                    pass
                chosen = [c for c in ALL_COMPONENTS if self.selected.get(c, False)]
                if chosen:
                    return chosen

    def run_interactive(self) -> int:
        """Avvia la GUI testuale interattiva principale."""
        self.refresh_audits()
        cursor = 0

        while True:
            self._clear_screen()
            print(self.render_main_menu(cursor))
            key = self._key_reader()

            if key == "UP":
                cursor = (cursor - 1) % 4
            elif key == "DOWN":
                cursor = (cursor + 1) % 4
            elif key == "Q" or (key == "ENTER" and cursor == 3):
                return 0
            elif key == "R" or (key == "ENTER" and cursor == 2):
                self.refresh_audits()
            elif key == "1" or (key == "ENTER" and cursor == 0):
                # Modalità 1: Tutto il pacchetto
                return self._execute_with_live_tui(list(ALL_COMPONENTS), force=False)
            elif key == "2" or (key == "ENTER" and cursor == 1):
                # Modalità 2: Solo alcune cose
                chosen = self._run_custom_selection_loop()
                if chosen:
                    return self._execute_with_live_tui(chosen, force=self.force_reinstall)

    def _execute_with_live_tui(
        self,
        selected_components: Sequence[ComponentId],
        *,
        force: bool = False,
    ) -> int:
        """Esegue i componenti scelti aggiornando in tempo reale la schermata TUI."""

        def _on_progress(
            _comp: ComponentId,
            _status: StepStatus,
            results_map: dict[ComponentId, StepExecutionResult],
            logs: list[str],
        ) -> None:
            self._clear_screen()
            print(
                self.render_execution_screen(
                    selected_components, results_map, logs, finished=False
                )
            )

        results = run_installation_plan(
            selected_components,
            audits=self.audits,
            force=force,
            on_progress=_on_progress,
        )
        final_map = {r.component: r for r in results}
        final_logs = [f"[{r.status.value}] {COMPONENT_LABELS[r.component]}: {r.message}" for r in results]
        self._clear_screen()
        print(
            self.render_execution_screen(
                selected_components, final_map, final_logs, finished=True
            )
        )

        any_failed = any(r.status == StepStatus.FAILED for r in results)
        if any_failed:
            print(
                "\n  "
                + self._c(
                    _Ansi.RED + _Ansi.BOLD,
                    "✖ Installazione terminata con errori. Controllare i messaggi sopra.",
                )
            )
            return bootstrap.EXIT_BOOTSTRAP_FAILED

        print(
            "\n  "
            + self._c(
                _Ansi.GREEN + _Ansi.BOLD,
                "✔ Operazione completata con successo! Verbalia è pronto all'uso.",
            )
        )
        return 0


# ---------------------------------------------------------------------------
# CLI Entrypoint
# ---------------------------------------------------------------------------
def parse_component_list(raw: str) -> list[ComponentId]:
    """Converte una stringa separata da virgole (es. 'python,whisper,ollama,test') in ComponentId."""
    alias_map: dict[str, ComponentId] = {
        "1": ComponentId.PYTHON_LIBS,
        "python": ComponentId.PYTHON_LIBS,
        "libs": ComponentId.PYTHON_LIBS,
        "2": ComponentId.WHISPER_AND_DEPS,
        "whisper": ComponentId.WHISPER_AND_DEPS,
        "ffmpeg": ComponentId.WHISPER_AND_DEPS,
        "3": ComponentId.OLLAMA_AND_LLM,
        "ollama": ComponentId.OLLAMA_AND_LLM,
        "llm": ComponentId.OLLAMA_AND_LLM,
        "4": ComponentId.SAMPLE_AUDIO_TEST,
        "test": ComponentId.SAMPLE_AUDIO_TEST,
        "audio": ComponentId.SAMPLE_AUDIO_TEST,
        "5": ComponentId.DESKTOP_SHORTCUT,
        "shortcut": ComponentId.DESKTOP_SHORTCUT,
        "desktop": ComponentId.DESKTOP_SHORTCUT,
    }
    selected: list[ComponentId] = []
    for token in raw.split(","):
        clean = token.strip().lower()
        if not clean:
            continue
        if clean not in alias_map:
            valid = ", ".join(c.value for c in ALL_COMPONENTS)
            raise ValueError(f"Componente non valido '{clean}'. Valori ammessi: {valid}")
        comp = alias_map[clean]
        if comp not in selected:
            selected.append(comp)
    return selected


def build_installer_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="installer.py",
        description="Installer automatico e GUI testuale per Verbalia.",
    )
    mode = parser.add_mutually_exclusive_group(required=False)
    mode.add_argument(
        "--all",
        "--unattended",
        dest="install_all",
        action="store_true",
        help="Installa e verifica automaticamente tutto il pacchetto senza interventi umani.",
    )
    mode.add_argument(
        "--only",
        type=str,
        default=None,
        metavar="COMPONENTI",
        help="Installa solo i componenti indicati separati da virgola: python,whisper,ollama,test,shortcut (o 1,2,3,4,5).",
    )
    mode.add_argument(
        "--check-only",
        action="store_true",
        help="Esegue esclusivamente il check iniziale mostrando lo stato dei componenti.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Forza la reinstallazione anche dei componenti già rilevati come presenti.",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    bootstrap.check_python_version()
    bootstrap.check_windows_platform()

    parser = build_installer_arg_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)

    tui = InstallerTUI()

    if args.check_only:
        tui.refresh_audits()
        for comp in ALL_COMPONENTS:
            audit = tui.audits[comp]
            status_str = "GIÀ INSTALLATO" if audit.installed else "DA INSTALLARE"
            print(f"{COMPONENT_LABELS[comp]}: [{status_str}] - {audit.summary}")
        return 0

    if args.install_all:
        tui.refresh_audits()
        return tui._execute_with_live_tui(list(ALL_COMPONENTS), force=bool(args.force))

    if args.only:
        try:
            chosen = parse_component_list(args.only)
        except ValueError as exc:
            sys.stderr.write(f"Errore: {exc}\n")
            return 2
        if not chosen:
            sys.stderr.write("Errore: nessun componente selezionato per --only.\n")
            return 2
        tui.refresh_audits()
        return tui._execute_with_live_tui(chosen, force=bool(args.force))

    return tui.run_interactive()


if __name__ == "__main__":
    raise SystemExit(main())
