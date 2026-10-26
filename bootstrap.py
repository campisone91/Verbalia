from __future__ import annotations

import hashlib
import importlib
import importlib.util
import json
import os
import pathlib
import platform
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

APP_NAME: str = "Verbalia"
APP_VERSION: str = "0.1.0"
STATE_SCHEMA_VERSION: int = 1
MIN_PYTHON_VERSION: tuple[int, int] = (3, 10)

EXIT_BOOTSTRAP_FAILED: int = 3
EXIT_MISSING_DEPENDENCY: int = 4

PROJECT_ROOT: Path = pathlib.Path(__file__).resolve().parent
REQUIREMENTS_PATH: Path = PROJECT_ROOT / "requirements.txt"
ENV_MARKER_PATH: Path = PROJECT_ROOT / ".env_ready.json"

PROMPTS_DIR: Path = PROJECT_ROOT / "prompts"
BIN_DIR: Path = PROJECT_ROOT / "bin"
LOG_DIR: Path = PROJECT_ROOT / "logs"
TEMP_DIR: Path = PROJECT_ROOT / "temp"
OUTPUT_DIR: Path = PROJECT_ROOT / "output"
FFMPEG_LOCAL_PATH: Path = BIN_DIR / "ffmpeg.exe"

FFMPEG_DOWNLOAD_URL: str = (
    "https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/"
    "ffmpeg-master-latest-win64-gpl.zip"
)
FFMPEG_DOWNLOAD_SHA256: str = ""
FFMPEG_DOWNLOAD_TIMEOUT_SEC: int = 120

OLLAMA_BASE_URL: str = "http://localhost:11434"
OLLAMA_MODEL: str = "llama3.1:8b"
OLLAMA_STARTUP_POLL_ATTEMPTS: int = 20
OLLAMA_STARTUP_POLL_INTERVAL_SEC: float = 0.5

WHISPER_GPU_MODEL: str = "large-v3-turbo"
WHISPER_GPU_COMPUTE_TYPE: str = "float16"
WHISPER_CPU_MODEL: str = "medium"
WHISPER_CPU_COMPUTE_TYPE: str = "int8"

REQUIRED_IMPORTS: dict[str, str] = {
    "pyaudiowpatch": "PyAudioWPatch",
    "pycaw": "pycaw",
    "comtypes": "comtypes",
    "psutil": "psutil",
    "faster_whisper": "faster-whisper",
    "requests": "requests",
    "docx": "python-docx",
    "win32com": "pywin32",
    "pystray": "pystray",
    "PIL": "Pillow",
}

REQUIRED_MARKER_KEYS: frozenset[str] = frozenset(
    {
        "schema_version",
        "app_version",
        "python_version",
        "platform",
        "architecture",
        "requirements_sha256",
        "ffmpeg_path",
        "ffmpeg_version",
        "ollama_model",
        "whisper_model",
        "whisper_device",
        "whisper_compute_type",
        "completed_at",
    }
)


def _windows_creation_flags() -> int:
    """Restituisce il flag CREATE_NO_WINDOW su Windows per subprocess."""
    return int(getattr(subprocess, "CREATE_NO_WINDOW", 0))


def _iso_now() -> str:
    """Restituisce il timestamp corrente in formato ISO 8601 con offset."""
    raw = time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime())
    if len(raw) >= 5 and raw[-5] in ("+", "-"):
        return f"{raw[:-2]}:{raw[-2:]}"
    return raw


def _ensure_directories() -> None:
    """Crea le directory runtime di base se non esistono."""
    for directory in (BIN_DIR, LOG_DIR, TEMP_DIR, OUTPUT_DIR):
        directory.mkdir(parents=True, exist_ok=True)


def check_python_version() -> None:
    """Verifica che la versione di Python soddisfi il requisito minimo."""
    current = (sys.version_info.major, sys.version_info.minor)
    if current < MIN_PYTHON_VERSION:
        sys.stderr.write(
            f"Versione Python non supportata: {current[0]}.{current[1]}. "
            f"Richiesto Python {MIN_PYTHON_VERSION[0]}.{MIN_PYTHON_VERSION[1]}+.\n"
        )
        raise SystemExit(EXIT_BOOTSTRAP_FAILED)


def check_windows_platform() -> None:
    """Verifica che il sistema operativo sia Windows."""
    if sys.platform != "win32" and platform.system().lower() != "windows":
        sys.stderr.write(
            f"Piattaforma non supportata: {platform.system()}. "
            "Questa applicazione supporta esclusivamente Windows 10/11.\n"
        )
        raise SystemExit(EXIT_BOOTSTRAP_FAILED)


def compute_requirements_sha256(requirements_path: Path) -> str:
    """Calcola lo SHA-256 del file requirements.txt."""
    if not requirements_path.is_file():
        raise FileNotFoundError(
            f"File requirements non trovato: {requirements_path}"
        )
    digest = hashlib.sha256()
    with requirements_path.open("rb") as handle:
        while True:
            chunk = handle.read(65536)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _quarantine_corrupt_marker(marker_path: Path) -> None:
    """Rinomina un file marker corrotto aggiungendo il timestamp."""
    timestamp = int(time.time())
    corrupt_path = marker_path.with_name(
        f".env_ready.corrupt.{timestamp}.json"
    )
    try:
        os.replace(marker_path, corrupt_path)
    except OSError as exc:
        sys.stderr.write(
            f"Impossibile rinominare marker corrotto {marker_path}: {exc}\n"
        )


def load_env_marker(marker_path: Path) -> dict[str, object] | None:
    """Carica e valida strutturalmente il marker .env_ready.json."""
    if not marker_path.exists():
        return None
    if not marker_path.is_file():
        _quarantine_corrupt_marker(marker_path)
        return None

    try:
        raw_text = marker_path.read_text(encoding="utf-8")
        loaded = json.loads(raw_text)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        _quarantine_corrupt_marker(marker_path)
        return None

    if not isinstance(loaded, dict):
        _quarantine_corrupt_marker(marker_path)
        return None

    if not REQUIRED_MARKER_KEYS.issubset(loaded.keys()):
        _quarantine_corrupt_marker(marker_path)
        return None

    schema_ver = loaded.get("schema_version")
    if isinstance(schema_ver, bool) or not isinstance(schema_ver, int):
        _quarantine_corrupt_marker(marker_path)
        return None

    for key in REQUIRED_MARKER_KEYS - {"schema_version"}:
        val = loaded.get(key)
        if not isinstance(val, str) or not val.strip():
            _quarantine_corrupt_marker(marker_path)
            return None

    return {str(k): v for k, v in loaded.items()}


def is_marker_valid(
    marker: dict[str, object],
    requirements_sha256: str,
) -> bool:
    """Verifica se il marker corrisponde all'ambiente e alla configurazione correnti."""
    if not REQUIRED_MARKER_KEYS.issubset(marker.keys()):
        return False

    if marker.get("schema_version") != STATE_SCHEMA_VERSION:
        return False
    if marker.get("app_version") != APP_VERSION:
        return False
    if marker.get("python_version") != platform.python_version():
        return False
    if marker.get("requirements_sha256") != requirements_sha256:
        return False
    if marker.get("ollama_model") != OLLAMA_MODEL:
        return False

    ffmpeg_path_raw = marker.get("ffmpeg_path")
    if not isinstance(ffmpeg_path_raw, str) or not ffmpeg_path_raw.strip():
        return False
    if not Path(ffmpeg_path_raw).is_file():
        return False

    return True


def find_missing_imports(
    required_imports: dict[str, str] | None = None,
) -> list[str]:
    """Restituisce la lista dei moduli richiesti non importabili."""
    target = (
        required_imports if required_imports is not None else REQUIRED_IMPORTS
    )
    missing: list[str] = []
    for module_name in target:
        try:
            spec = importlib.util.find_spec(module_name)
        except (ImportError, ValueError):
            spec = None
        if spec is None:
            missing.append(module_name)
    return missing


def install_requirements(requirements_path: Path) -> None:
    """Aggiorna pip e installa le dipendenze da requirements.txt."""
    if not requirements_path.is_file():
        raise FileNotFoundError(
            f"File requirements.txt non trovato: {requirements_path}"
        )

    in_venv = sys.prefix != getattr(sys, "base_prefix", sys.prefix)
    _ = in_venv

    subprocess.check_call(
        [
            sys.executable,
            "-m",
            "pip",
            "install",
            "--upgrade",
            "pip",
        ]
    )
    subprocess.check_call(
        [
            sys.executable,
            "-m",
            "pip",
            "install",
            "-r",
            str(requirements_path),
        ]
    )


def _validate_ffmpeg_binary(executable: Path) -> str | None:
    """Esegue 'ffmpeg -hide_banner -version' e restituisce la stringa di versione se valido."""
    if not executable.is_file():
        return None
    try:
        completed = subprocess.run(
            [str(executable), "-hide_banner", "-version"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=15,
            check=False,
            creationflags=_windows_creation_flags(),
        )
    except (OSError, subprocess.SubprocessError):
        return None

    if completed.returncode != 0:
        return None

    stdout_text = (completed.stdout or "").strip()
    if not stdout_text:
        return "ffmpeg version (verified)"
    first_line = stdout_text.splitlines()[0].strip()
    return first_line if first_line else "ffmpeg version (verified)"


def _safe_extract_zip(archive_path: Path, extract_dir: Path) -> None:
    """Estrae un archivio ZIP verificando l'assenza di path traversal."""
    base_resolved = extract_dir.resolve()
    with zipfile.ZipFile(archive_path, "r") as zf:
        for member in zf.infolist():
            member_path = Path(member.filename)
            if member_path.is_absolute() or ".." in member_path.parts:
                raise RuntimeError(
                    f"Rilevato path traversal nell'archivio ZIP: {member.filename}"
                )
            target_resolved = (base_resolved / member_path).resolve()
            if (
                target_resolved != base_resolved
                and base_resolved not in target_resolved.parents
            ):
                raise RuntimeError(
                    f"Destinazione ZIP esterna alla directory permessa: {member.filename}"
                )
        zf.extractall(base_resolved)


def _download_and_install_ffmpeg(local_target: Path) -> tuple[Path, str]:
    """Scarica FFmpeg via HTTPS, verifica SHA-256, estrae e promuove atomicamente."""
    if not FFMPEG_DOWNLOAD_URL.lower().startswith("https://"):
        raise RuntimeError(
            f"L'URL di download FFmpeg deve usare HTTPS: {FFMPEG_DOWNLOAD_URL}"
        )

    target_dir = local_target.parent
    target_dir.mkdir(parents=True, exist_ok=True)
    part_path = target_dir / "ffmpeg_download.zip.part"
    extract_dir = Path(
        tempfile.mkdtemp(prefix="ffmpeg_extract_", dir=str(target_dir))
    )
    temp_binary = target_dir / "ffmpeg.exe.tmp"

    try:
        digest = hashlib.sha256()
        request = urllib.request.Request(
            FFMPEG_DOWNLOAD_URL,
            headers={"User-Agent": f"{APP_NAME}/{APP_VERSION}"},
        )
        with (
            urllib.request.urlopen(
                request, timeout=FFMPEG_DOWNLOAD_TIMEOUT_SEC
            ) as response,
            part_path.open("wb") as part_handle,
        ):
            while True:
                chunk = response.read(65536)
                if not chunk:
                    break
                part_handle.write(chunk)
                digest.update(chunk)
            part_handle.flush()
            os.fsync(part_handle.fileno())

        actual_sha256 = digest.hexdigest()
        expected_sha256 = FFMPEG_DOWNLOAD_SHA256.strip()
        if expected_sha256 and actual_sha256.lower() != expected_sha256.lower():
            raise RuntimeError(
                "Checksum SHA-256 FFmpeg non valido: "
                f"atteso {expected_sha256}, calcolato {actual_sha256}"
            )

        _safe_extract_zip(part_path, extract_dir)

        found_ffmpeg = next(extract_dir.rglob("ffmpeg.exe"), None)
        if found_ffmpeg is None or not found_ffmpeg.is_file():
            raise RuntimeError(
                "ffmpeg.exe non trovato all'interno dell'archivio scaricato."
            )

        version = _validate_ffmpeg_binary(found_ffmpeg)
        if version is None:
            raise RuntimeError(
                "Il binario ffmpeg.exe estratto non ha superato la validazione."
            )

        shutil.copy2(found_ffmpeg, temp_binary)
        os.replace(temp_binary, local_target)

        final_version = _validate_ffmpeg_binary(local_target)
        if final_version is None:
            raise RuntimeError(
                "Il binario ffmpeg.exe copiato in bin/ non è valido."
            )
        return (local_target.resolve(), final_version)
    except Exception as exc:
        if isinstance(exc, RuntimeError):
            raise
        raise RuntimeError(
            f"Download o installazione di FFmpeg fallita: {exc}"
        ) from exc
    finally:
        if part_path.exists():
            try:
                part_path.unlink()
            except OSError as cleanup_exc:
                sys.stderr.write(
                    f"Impossibile rimuovere file parziale {part_path}: {cleanup_exc}\n"
                )
        if temp_binary.exists():
            try:
                temp_binary.unlink()
            except OSError as cleanup_exc:
                sys.stderr.write(
                    f"Impossibile rimuovere binario temporaneo {temp_binary}: {cleanup_exc}\n"
                )
        if extract_dir.exists():
            shutil.rmtree(extract_dir, ignore_errors=True)


def resolve_ffmpeg(bin_dir: Path | None = None) -> tuple[Path, str]:
    """Risolve FFmpeg secondo l'ordine: PATH -> bin/ffmpeg.exe -> winget -> download HTTPS."""
    target_bin_dir = (bin_dir if bin_dir is not None else BIN_DIR).resolve()
    target_bin_dir.mkdir(parents=True, exist_ok=True)
    local_ffmpeg = target_bin_dir / "ffmpeg.exe"

    # 1. shutil.which("ffmpeg")
    system_ffmpeg = shutil.which("ffmpeg")
    if system_ffmpeg:
        candidate = Path(system_ffmpeg).resolve()
        version = _validate_ffmpeg_binary(candidate)
        if version is not None:
            return (candidate, version)

    # 2. bin/ffmpeg.exe
    if local_ffmpeg.is_file():
        version = _validate_ffmpeg_binary(local_ffmpeg)
        if version is not None:
            return (local_ffmpeg.resolve(), version)

    # 3. winget
    winget_exe = shutil.which("winget")
    if winget_exe:
        try:
            winget_proc = subprocess.run(
                [
                    winget_exe,
                    "install",
                    "--id",
                    "Gyan.FFmpeg",
                    "-e",
                    "--silent",
                    "--accept-package-agreements",
                    "--accept-source-agreements",
                ],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=FFMPEG_DOWNLOAD_TIMEOUT_SEC,
                check=False,
                creationflags=_windows_creation_flags(),
            )
            if winget_proc.returncode == 0:
                post_winget = shutil.which("ffmpeg")
                if post_winget:
                    candidate = Path(post_winget).resolve()
                    version = _validate_ffmpeg_binary(candidate)
                    if version is not None:
                        return (candidate, version)
        except (OSError, subprocess.SubprocessError) as exc:
            sys.stderr.write(
                f"Installazione FFmpeg tramite winget non riuscita: {exc}\n"
            )

    # 4. HTTPS download
    return _download_and_install_ffmpeg(local_ffmpeg)


def _fetch_ollama_models(base_url: str, timeout: float = 5.0) -> list[str] | None:
    """Interroga GET /api/tags su Ollama e restituisce i nomi dei modelli installati."""
    url = f"{base_url.rstrip('/')}/api/tags"
    request = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            status = getattr(response, "status", 200)
            if status != 200:
                return None
            raw_bytes = response.read()
    except (urllib.error.URLError, OSError, ValueError):
        return None

    try:
        payload = json.loads(raw_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None

    if not isinstance(payload, dict):
        return None

    models_raw = payload.get("models", [])
    if not isinstance(models_raw, list):
        return None

    names: list[str] = []
    for entry in models_raw:
        if isinstance(entry, dict):
            name = entry.get("name") or entry.get("model")
            if isinstance(name, str) and name.strip():
                names.append(name.strip())
    return names


def _find_ollama_executable() -> str | None:
    """Cerca l'eseguibile ollama.exe nel PATH o nelle directory standard di Windows."""
    which_ollama = shutil.which("ollama") or shutil.which("ollama.exe")
    if which_ollama:
        return which_ollama

    candidates: list[Path] = []
    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        candidates.append(
            Path(local_app_data) / "Programs" / "Ollama" / "ollama.exe"
        )
    program_files = os.environ.get("ProgramFiles")
    if program_files:
        candidates.append(Path(program_files) / "Ollama" / "ollama.exe")

    for candidate in candidates:
        if candidate.is_file():
            return str(candidate)
    return None


def _is_model_available(installed_models: list[str], model_name: str) -> bool:
    """Verifica se il modello richiesto è presente nella lista restituita da Ollama."""
    target = model_name.strip()
    for installed in installed_models:
        if installed == target:
            return True
        if ":" not in target and installed == f"{target}:latest":
            return True
    return False


# Variabili d'ambiente passate a "ollama serve" quando è l'app ad avviarlo. Se il server è avviato
# dall'app tray di Ollama valgono solo le variabili utente (setx OLLAMA_IGPU_ENABLE 1 + riavvio).
OLLAMA_SERVE_ENV_OVERRIDES: dict[str, str] = {
    "OLLAMA_IGPU_ENABLE": "1",
}
OLLAMA_IGPU_DROPPED_MARKER: str = "dropping integrated gpu"
OLLAMA_IGPU_REMEDY: str = (
    "Ollama scarta la GPU integrata: eseguire 'setx OLLAMA_IGPU_ENABLE 1' "
    "e riavviare l'app di Ollama (icona nella tray)."
)


def _default_ollama_server_log() -> Path | None:
    local_app_data = os.environ.get("LOCALAPPDATA")
    if not local_app_data:
        return None
    return Path(local_app_data) / "Ollama" / "server.log"


def detect_ollama_igpu_dropped(server_log: Path | None = None, tail_bytes: int = 262144) -> bool:
    """Verifica se l'ultimo avvio del server Ollama ha scartato la GPU integrata."""
    log_path = server_log if server_log is not None else _default_ollama_server_log()
    if log_path is None or not log_path.is_file():
        return False
    try:
        with log_path.open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            handle.seek(max(0, size - tail_bytes))
            tail = handle.read().decode("utf-8", errors="replace").lower()
    except OSError:
        return False
    # Considera solo l'ultimo avvio del server: il marker deve comparire dopo l'ultima "server config"
    last_start = tail.rfind("server config")
    segment = tail[last_start:] if last_start >= 0 else tail
    return OLLAMA_IGPU_DROPPED_MARKER in segment


def probe_ollama_gpu(
    base_url: str = OLLAMA_BASE_URL,
    model_name: str | None = None,
    timeout: float = 3.0,
) -> dict[str, object]:
    """Interroga GET /api/ps e calcola la quota del modello caricata in VRAM (size_vram/size).

    Restituisce un dizionario con: reachable, loaded (modello in memoria), gpu_percent
    (None se nessun modello è caricato), models (dettaglio per modello) e hint (rimedio
    suggerito se la iGPU risulta scartata dal server).
    """
    result: dict[str, object] = {
        "reachable": False,
        "loaded": False,
        "gpu_percent": None,
        "models": [],
        "hint": "",
    }
    url = f"{base_url.rstrip('/')}/api/ps"
    request = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw_bytes = response.read()
        payload = json.loads(raw_bytes.decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError, UnicodeDecodeError):
        return result

    result["reachable"] = True
    models_raw = payload.get("models", []) if isinstance(payload, dict) else []
    details: list[dict[str, object]] = []
    selected: dict[str, object] | None = None
    if isinstance(models_raw, list):
        for entry in models_raw:
            if not isinstance(entry, dict):
                continue
            name = str(entry.get("name") or entry.get("model") or "")
            try:
                size = int(entry.get("size") or 0)
                size_vram = int(entry.get("size_vram") or 0)
            except (TypeError, ValueError):
                size, size_vram = 0, 0
            percent = round(100.0 * size_vram / size, 1) if size > 0 else 0.0
            detail: dict[str, object] = {
                "name": name,
                "size": size,
                "size_vram": size_vram,
                "gpu_percent": percent,
            }
            details.append(detail)
            if model_name and _is_model_available([name], model_name):
                selected = detail
    result["models"] = details
    if selected is None and details:
        selected = max(details, key=lambda d: float(d["gpu_percent"]))  # type: ignore[arg-type]
    if selected is not None:
        result["loaded"] = True
        result["gpu_percent"] = selected["gpu_percent"]
        if float(selected["gpu_percent"]) <= 0.0 and detect_ollama_igpu_dropped():  # type: ignore[arg-type]
            result["hint"] = OLLAMA_IGPU_REMEDY
    elif detect_ollama_igpu_dropped():
        result["hint"] = OLLAMA_IGPU_REMEDY
    return result


def is_tkinter_available() -> bool:
    """Verifica che tkinter sia importabile (necessario per il cruscotto --dashboard)."""
    try:
        spec = importlib.util.find_spec("tkinter")
    except (ImportError, ValueError):
        return False
    return spec is not None


def ensure_ollama_ready(base_url: str, model_name: str) -> None:
    """Verifica che il server Ollama risponda e che il modello configurato sia presente."""
    models = _fetch_ollama_models(base_url)
    ollama_exe = _find_ollama_executable()

    if models is None:
        if not ollama_exe:
            sys.stderr.write(
                f"\n[ERRORE DIPENDENZA] Ollama non è raggiungibile su {base_url} e 'ollama.exe' non è stato trovato nel sistema.\n"
                "Verbalia opera al 100% in locale e necessita di Ollama per la sintesi dei verbali.\n\n"
                "Istruzioni di installazione e avvio:\n"
                "  1. Scarica e installa Ollama da: https://ollama.com/download\n"
                "     (oppure da PowerShell: winget install Ollama.Ollama)\n"
                "  2. Avvia Ollama e scarica il modello configurato:\n"
                f"     ollama pull {model_name}\n\n"
                "Dopo aver avviato Ollama, riesegui il comando desiderato.\n"
            )
            raise SystemExit(EXIT_MISSING_DEPENDENCY)

        LOG_DIR.mkdir(parents=True, exist_ok=True)
        ollama_log_path = LOG_DIR / "ollama_serve.log"
        creation_flags = (
            _windows_creation_flags()
            | int(getattr(subprocess, "DETACHED_PROCESS", 0))
            | int(getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
        )
        serve_env = dict(os.environ)
        serve_env.update(OLLAMA_SERVE_ENV_OVERRIDES)
        with ollama_log_path.open("a", encoding="utf-8", newline="\n") as log_fh:
            subprocess.Popen(
                [ollama_exe, "serve"],
                stdin=subprocess.DEVNULL,
                stdout=log_fh,
                stderr=subprocess.STDOUT,
                creationflags=creation_flags,
                close_fds=True,
                env=serve_env,
            )

        for _ in range(OLLAMA_STARTUP_POLL_ATTEMPTS):
            time.sleep(OLLAMA_STARTUP_POLL_INTERVAL_SEC)
            models = _fetch_ollama_models(base_url)
            if models is not None:
                break

        if models is None:
            sys.stderr.write(
                f"\n[ERRORE DIPENDENZA] Il server Ollama è stato avviato ma non ha risposto su {base_url} entro il timeout.\n"
                f"Consultare il log '{ollama_log_path}' per dettagli.\n"
            )
            raise SystemExit(EXIT_MISSING_DEPENDENCY)

    if not _is_model_available(models, model_name):
        executable_to_use = ollama_exe or "ollama"
        try:
            subprocess.run(
                [
                    executable_to_use,
                    "pull",
                    model_name,
                ],
                check=True,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            sys.stderr.write(
                f"\n[ERRORE DIPENDENZA] Impossibile scaricare il modello Ollama '{model_name}': {exc}\n"
                f"Eseguire manualmente: ollama pull {model_name}\n"
            )
            raise SystemExit(EXIT_MISSING_DEPENDENCY) from None


def probe_whisper_configuration(
    *,
    setup_only: bool = False,
) -> tuple[str, str, str]:
    """Valida e seleziona la configurazione Whisper (GPU con fallback CPU in setup-only)."""
    if not setup_only:
        return (WHISPER_GPU_MODEL, "cuda", WHISPER_GPU_COMPUTE_TYPE)

    try:
        fw_module = importlib.import_module("faster_whisper")
        whisper_model_cls = getattr(fw_module, "WhisperModel")
    except (ImportError, AttributeError) as exc:
        raise RuntimeError(
            f"Impossibile importare faster_whisper.WhisperModel: {exc}"
        ) from exc

    gpu_instance: object | None = None
    try:
        gpu_instance = whisper_model_cls(
            WHISPER_GPU_MODEL,
            device="cuda",
            compute_type=WHISPER_GPU_COMPUTE_TYPE,
        )
        return (WHISPER_GPU_MODEL, "cuda", WHISPER_GPU_COMPUTE_TYPE)
    except Exception as gpu_exc:
        sys.stderr.write(
            f"Inizializzazione Whisper GPU fallita ({gpu_exc}); "
            "tentativo con fallback CPU int8.\n"
        )
    finally:
        if gpu_instance is not None:
            del gpu_instance
        importlib.import_module("gc").collect()

    cpu_instance: object | None = None
    try:
        cpu_instance = whisper_model_cls(
            WHISPER_CPU_MODEL,
            device="cpu",
            compute_type=WHISPER_CPU_COMPUTE_TYPE,
        )
        return (WHISPER_CPU_MODEL, "cpu", WHISPER_CPU_COMPUTE_TYPE)
    except Exception as cpu_exc:
        raise RuntimeError(
            f"Inizializzazione Whisper fallita sia su GPU che su CPU: {cpu_exc}"
        ) from cpu_exc
    finally:
        if cpu_instance is not None:
            del cpu_instance
        importlib.import_module("gc").collect()


def write_env_marker(
    marker_path: Path,
    payload: dict[str, object],
) -> None:
    """Scrive in modo atomico il file .env_ready.json."""
    marker_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = marker_path.with_suffix(marker_path.suffix + ".tmp")
    serialized = json.dumps(
        payload,
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
    )
    with temp_path.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(serialized)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temp_path, marker_path)


def _format_missing_imports_for_stderr(missing: list[str]) -> str:
    """Formatta l'elenco dei moduli e pacchetti mancanti per stderr."""
    formatted: list[str] = []
    for mod in missing:
        pkg = REQUIRED_IMPORTS.get(mod, mod)
        if pkg != mod:
            formatted.append(f"{mod} ({pkg})")
        else:
            formatted.append(mod)
    return ", ".join(formatted)


def ensure_environment(
    *,
    setup_only: bool | None = None,
) -> None:
    """Esegue la sequenza di bootstrap idempotente dell'ambiente applicativo."""
    is_setup_only = (
        bool(setup_only)
        if setup_only is not None
        else ("--setup-only" in sys.argv)
    )

    # 1. PROJECT_ROOT è già risolto a livello di modulo
    # 2. Verifica Python
    check_python_version()
    # 3. Verifica Windows
    check_windows_platform()
    # 4. Creazione directory runtime
    _ensure_directories()

    if os.environ.get("TEAMS_SCRIBE_SKIP_BOOTSTRAP") == "1":
        return

    # 5. Calcolo hash requirements.txt
    if REQUIREMENTS_PATH.is_file():
        req_sha256 = compute_requirements_sha256(REQUIREMENTS_PATH)
    else:
        req_sha256 = hashlib.sha256(b"").hexdigest()

    # 6. Lettura marker
    marker = load_env_marker(ENV_MARKER_PATH)

    # 7. Verifica import
    missing_imports = find_missing_imports(REQUIRED_IMPORTS)

    if (
        marker is not None
        and is_marker_valid(marker, req_sha256)
        and not missing_imports
    ):
        return

    # 8-10. Installazione dipendenze, invalidazione cache e protezione restart loop
    if missing_imports:
        if os.environ.get("TEAMS_SCRIBE_BOOTSTRAP_RESTARTED") == "1":
            sys.stderr.write(
                "Bootstrap fallito: import ancora mancanti dopo il riavvio: "
                f"{_format_missing_imports_for_stderr(missing_imports)}\n"
            )
            raise SystemExit(EXIT_BOOTSTRAP_FAILED)

        install_requirements(REQUIREMENTS_PATH)
        importlib.invalidate_caches()
        os.environ["TEAMS_SCRIBE_BOOTSTRAP_RESTARTED"] = "1"

        still_missing = find_missing_imports(REQUIRED_IMPORTS)
        if still_missing:
            sys.stderr.write(
                "Bootstrap fallito: import mancanti dopo l'installazione: "
                f"{_format_missing_imports_for_stderr(still_missing)}\n"
            )
            raise SystemExit(EXIT_BOOTSTRAP_FAILED)

    # 11. Risoluzione FFmpeg
    ffmpeg_path, ffmpeg_version = resolve_ffmpeg(BIN_DIR)

    # 12-15. Verifica e avvio Ollama + verifica e pull modello
    ensure_ollama_ready(OLLAMA_BASE_URL, OLLAMA_MODEL)

    # 16. Validazione Whisper in setup-only
    if is_setup_only:
        whisper_model, whisper_device, whisper_compute_type = (
            probe_whisper_configuration(setup_only=True)
        )
    elif (
        marker is not None
        and isinstance(marker.get("whisper_model"), str)
        and isinstance(marker.get("whisper_device"), str)
        and isinstance(marker.get("whisper_compute_type"), str)
    ):
        whisper_model = str(marker["whisper_model"])
        whisper_device = str(marker["whisper_device"])
        whisper_compute_type = str(marker["whisper_compute_type"])
    else:
        whisper_model, whisper_device, whisper_compute_type = (
            probe_whisper_configuration(setup_only=False)
        )

    # 17. Scrittura marker
    marker_payload: dict[str, object] = {
        "schema_version": STATE_SCHEMA_VERSION,
        "app_version": APP_VERSION,
        "python_version": platform.python_version(),
        "platform": platform.system(),
        "architecture": platform.machine(),
        "requirements_sha256": req_sha256,
        "ffmpeg_path": str(ffmpeg_path),
        "ffmpeg_version": ffmpeg_version,
        "ollama_model": OLLAMA_MODEL,
        "whisper_model": whisper_model,
        "whisper_device": whisper_device,
        "whisper_compute_type": whisper_compute_type,
        "completed_at": _iso_now(),
    }
    write_env_marker(ENV_MARKER_PATH, marker_payload)
