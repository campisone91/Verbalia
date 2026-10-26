from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

import bootstrap


def test_env_marker_valid_corrupt_and_obsolete(tmp_path: Path) -> None:
    """Verifica AC01: caricamento marker valido, quarantena marker corrotto e rilevamento marker obsoleto."""
    marker_path = tmp_path / ".env_ready.json"
    fake_ffmpeg = tmp_path / "ffmpeg.exe"
    fake_ffmpeg.write_bytes(b"MZ")

    valid_payload: dict[str, object] = {
        "schema_version": bootstrap.STATE_SCHEMA_VERSION,
        "app_version": bootstrap.APP_VERSION,
        "python_version": bootstrap.platform.python_version(),
        "platform": "Windows",
        "architecture": "AMD64",
        "requirements_sha256": "abc123sha",
        "ffmpeg_path": str(fake_ffmpeg),
        "ffmpeg_version": "ffmpeg version 7.0",
        "ollama_model": bootstrap.OLLAMA_MODEL,
        "whisper_model": "large-v3-turbo",
        "whisper_device": "cuda",
        "whisper_compute_type": "float16",
        "completed_at": "2026-10-23T14:00:00+02:00",
    }
    bootstrap.write_env_marker(marker_path, valid_payload)

    loaded = bootstrap.load_env_marker(marker_path)
    assert loaded is not None
    assert bootstrap.is_marker_valid(loaded, "abc123sha") is True
    assert bootstrap.is_marker_valid(loaded, "different_sha") is False

    # Marker corrotto -> rinominato in .env_ready.corrupt.<ts>.json
    marker_path.write_text("{corrupt_json", encoding="utf-8")
    assert bootstrap.load_env_marker(marker_path) is None
    assert not marker_path.exists()
    corrupt_files = list(tmp_path.glob(".env_ready.corrupt.*.json"))
    assert len(corrupt_files) == 1


def test_bootstrap_restart_loop_protection(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verifica che il bootstrap impedisca loop infiniti se gli import mancano dopo il riavvio."""
    monkeypatch.setenv("TEAMS_SCRIBE_SKIP_BOOTSTRAP", "0")
    monkeypatch.setenv("TEAMS_SCRIBE_BOOTSTRAP_RESTARTED", "1")
    monkeypatch.setattr(bootstrap, "load_env_marker", lambda _p: None)
    monkeypatch.setattr(bootstrap, "find_missing_imports", lambda _r=None: ["faster_whisper"])

    with pytest.raises(SystemExit) as exc_info:
        bootstrap.ensure_environment(setup_only=False)
    assert exc_info.value.code == bootstrap.EXIT_BOOTSTRAP_FAILED


def test_resolve_ffmpeg_local_and_zip_traversal_guard(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verifica risoluzione FFmpeg locale, guardia contro Zip Slip e checksum errato."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    local_ffmpeg = bin_dir / "ffmpeg.exe"
    local_ffmpeg.write_bytes(b"MZ")

    monkeypatch.setattr(bootstrap.shutil, "which", lambda _name: None)
    monkeypatch.setattr(
        bootstrap,
        "_validate_ffmpeg_binary",
        lambda p: "ffmpeg version 7.1" if Path(p) == local_ffmpeg else None,
    )

    resolved_path, version = bootstrap.resolve_ffmpeg(bin_dir)
    assert resolved_path == local_ffmpeg.resolve()
    assert "7.1" in version

    # Guardia contro Zip Slip (path traversal in archivio ZIP)
    malicious_zip = tmp_path / "malicious.zip"
    with zipfile.ZipFile(malicious_zip, "w") as zf:
        zf.writestr("../evil.exe", b"evil")

    with pytest.raises(RuntimeError, match="traversal"):
        bootstrap._safe_extract_zip(malicious_zip, tmp_path / "extract")


def test_ensure_ollama_ready_starts_server_and_pulls_missing_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verifica avvio server Ollama quando non raggiungibile e pull quando il modello manca."""
    poll_states: list[list[str] | None] = [None, ["altro_modello:latest"]]
    popen_calls: list[list[str]] = []
    pull_calls: list[list[str]] = []

    monkeypatch.setattr(
        bootstrap,
        "_fetch_ollama_models",
        lambda _url, timeout=5.0: poll_states.pop(0) if poll_states else ["llama3.1:8b"],
    )
    monkeypatch.setattr(bootstrap, "_find_ollama_executable", lambda: "C:\\Ollama\\ollama.exe")
    monkeypatch.setattr(bootstrap.time, "sleep", lambda _s: None)

    class _DummyPopen:
        def __init__(self, args: list[str], **_kwargs: object) -> None:
            popen_calls.append(args)

    monkeypatch.setattr(bootstrap.subprocess, "Popen", _DummyPopen)
    monkeypatch.setattr(
        bootstrap.subprocess,
        "run",
        lambda args, **_kw: pull_calls.append(args),
    )

    bootstrap.ensure_ollama_ready("http://localhost:11434", "llama3.1:8b")
    assert len(popen_calls) == 1
    assert popen_calls[0][1] == "serve"
    assert len(pull_calls) == 1
    assert pull_calls[0][1:] == ["pull", "llama3.1:8b"]


def test_bootstrap_checks_and_utilities(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Verifica check_python_version, compute_requirements_sha256 e find_missing_imports."""
    bootstrap.check_python_version()

    class _FakeVersion:
        major = 3
        minor = 9

    monkeypatch.setattr(bootstrap.sys, "version_info", _FakeVersion())
    with pytest.raises(SystemExit):
        bootstrap.check_python_version()

    # compute_requirements_sha256
    req_file = tmp_path / "req.txt"
    req_file.write_text("numpy\nrequests\n", encoding="utf-8")
    h = bootstrap.compute_requirements_sha256(req_file)
    assert len(h) == 64

    # find_missing_imports
    missing = bootstrap.find_missing_imports({"os": "os", "modulo_inventato_xyz": "xyz"})
    assert "modulo_inventato_xyz" in missing
    assert "os" not in missing

