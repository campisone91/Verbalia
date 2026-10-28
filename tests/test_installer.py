from __future__ import annotations

import io
import json
import wave
from pathlib import Path

import pytest

import bootstrap
from tools import installer
from tools.installer import (
    ALL_COMPONENTS,
    ComponentAudit,
    ComponentId,
    InstallerTUI,
    StepExecutionResult,
    StepStatus,
    audit_ollama_and_llm,
    audit_python_libs,
    audit_whisper_and_deps,
    ensure_sample_audio_wav,
    execute_step_ollama_and_llm,
    execute_step_python_libs,
    execute_step_sample_audio_test,
    execute_step_whisper_and_deps,
    parse_component_list,
)


def _make_fake_audits(*, all_installed: bool = True) -> dict[ComponentId, ComponentAudit]:
    return {
        ComponentId.PYTHON_LIBS: ComponentAudit(
            ComponentId.PYTHON_LIBS,
            installed=all_installed,
            summary="11/11 librerie pronte" if all_installed else "Mancanti 2 librerie",
        ),
        ComponentId.WHISPER_AND_DEPS: ComponentAudit(
            ComponentId.WHISPER_AND_DEPS,
            installed=all_installed,
            summary="FFmpeg OK • Modello 'medium' in cache" if all_installed else "FFmpeg mancante",
            details={
                "ffmpeg_ok": all_installed,
                "ffmpeg_path": "C:\\ffmpeg.exe" if all_installed else None,
                "ffmpeg_version": "ffmpeg 9.0" if all_installed else None,
                "libs_ok": all_installed,
                "whisper_model": "medium",
                "whisper_device": "cpu",
                "whisper_compute_type": "int8",
                "model_cached": all_installed,
            },
        ),
        ComponentId.OLLAMA_AND_LLM: ComponentAudit(
            ComponentId.OLLAMA_AND_LLM,
            installed=all_installed,
            summary="Ollama OK • llama3.1:8b presente" if all_installed else "Ollama assente",
            details={
                "ollama_installed": all_installed,
                "server_running": all_installed,
                "model_present": all_installed,
                "model_name": "llama3.1:8b",
            },
        ),
        ComponentId.SAMPLE_AUDIO_TEST: ComponentAudit(
            ComponentId.SAMPLE_AUDIO_TEST,
            installed=all_installed,
            summary="Campione pronto",
        ),
        ComponentId.DESKTOP_SHORTCUT: ComponentAudit(
            ComponentId.DESKTOP_SHORTCUT,
            installed=all_installed,
            summary="Presente" if all_installed else "Assente",
        ),
    }


def test_preflight_audit_detects_installed_and_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verifica che il check iniziale rilevi correttamente componenti presenti e mancanti."""
    monkeypatch.setattr(bootstrap, "find_missing_imports", lambda _req=None: [])
    py_audit = audit_python_libs()
    assert py_audit.installed is True

    monkeypatch.setattr(bootstrap, "find_missing_imports", lambda _req=None: ["faster_whisper"])
    py_missing = audit_python_libs()
    assert py_missing.installed is False
    assert "faster-whisper" in py_missing.summary

    # Whisper audit: presente vs mancante
    monkeypatch.setattr(installer, "_check_existing_ffmpeg", lambda: (Path("C:/ffmpeg.exe"), "ffmpeg 9.0"))
    monkeypatch.setattr(bootstrap, "find_missing_imports", lambda _req=None: [])
    monkeypatch.setattr(installer, "_detect_preferred_whisper_target", lambda: ("medium", "cpu", "int8"))
    monkeypatch.setattr(bootstrap, "check_whisper_model_cached", lambda _m: True)
    w_audit = audit_whisper_and_deps()
    assert w_audit.installed is True

    monkeypatch.setattr(bootstrap, "check_whisper_model_cached", lambda _m: False)
    w_not_cached = audit_whisper_and_deps()
    assert w_not_cached.installed is False
    assert "da scaricare" in w_not_cached.summary

    # Ollama audit
    monkeypatch.setattr(bootstrap, "_find_ollama_executable", lambda: "C:\\Ollama\\ollama.exe")
    monkeypatch.setattr(bootstrap, "_fetch_ollama_models", lambda _u, timeout=2.5: ["llama3.1:8b"])
    o_audit = audit_ollama_and_llm()
    assert o_audit.installed is True

    monkeypatch.setattr(bootstrap, "_find_ollama_executable", lambda: None)
    monkeypatch.setattr(bootstrap, "_fetch_ollama_models", lambda _u, timeout=2.5: None)
    o_missing = audit_ollama_and_llm()
    assert o_missing.installed is False


def test_steps_skip_when_already_installed_and_run_when_forced(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verifica che i componenti già installati vengano saltati (SKIPPED) salvo force=True."""
    audits = _make_fake_audits(all_installed=True)
    logs: list[str] = []

    res_py = execute_step_python_libs(audits[ComponentId.PYTHON_LIBS], force=False, log=logs.append)
    assert res_py.status == StepStatus.SKIPPED

    res_wh = execute_step_whisper_and_deps(audits[ComponentId.WHISPER_AND_DEPS], force=False, log=logs.append)
    assert res_wh.status == StepStatus.SKIPPED

    res_ol = execute_step_ollama_and_llm(audits[ComponentId.OLLAMA_AND_LLM], force=False, log=logs.append)
    assert res_ol.status == StepStatus.SKIPPED

    # Con force=True esegue effettivamente l'installazione delle librerie Python
    install_called = False

    def _fake_install(_p: Path) -> None:
        nonlocal install_called
        install_called = True

    monkeypatch.setattr(bootstrap, "install_requirements", _fake_install)
    monkeypatch.setattr(bootstrap, "find_missing_imports", lambda _req=None: [])
    res_py_forced = execute_step_python_libs(
        audits[ComponentId.PYTHON_LIBS], force=True, log=logs.append
    )
    assert res_py_forced.status == StepStatus.COMPLETED
    assert install_called is True


def test_install_ollama_silently_winget_and_https_fallback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verifica l'installazione silenziosa di Ollama sia tramite winget che tramite fallback HTTPS."""
    # Caso 1: winget ha successo
    calls: list[str] = []

    def _find_after_winget() -> str | None:
        return "C:\\Ollama\\ollama.exe" if calls else None

    monkeypatch.setattr(bootstrap, "_find_ollama_executable", _find_after_winget)
    monkeypatch.setattr(bootstrap.shutil, "which", lambda _name: "C:\\Windows\\winget.exe")

    class _DummyCompleted:
        returncode = 0
        stdout = "Installed"
        stderr = ""

    def _fake_run(args: list[str], **_kw: object) -> _DummyCompleted:
        calls.append(" ".join(args))
        return _DummyCompleted()

    monkeypatch.setattr(bootstrap.subprocess, "run", _fake_run)
    exe = bootstrap.install_ollama_silently(tmp_path)
    assert exe == "C:\\Ollama\\ollama.exe"
    assert any("Ollama.Ollama" in c for c in calls)

    # Caso 2: winget assente -> fallback download HTTPS di OllamaSetup.exe con /VERYSILENT
    calls.clear()
    monkeypatch.setattr(bootstrap.shutil, "which", lambda _name: None)

    class _DummyResponse(io.BytesIO):
        def __enter__(self) -> _DummyResponse:
            return self

        def __exit__(self, *_args: object) -> None:
            pass

    monkeypatch.setattr(
        bootstrap.urllib.request,
        "urlopen",
        lambda *_a, **_kw: _DummyResponse(b"MZ_FAKE_OLLAMA_SETUP"),
    )
    exe2 = bootstrap.install_ollama_silently(tmp_path)
    assert exe2 == "C:\\Ollama\\ollama.exe"
    assert any("/VERYSILENT" in c for c in calls)


def test_tui_mode_1_all_package_and_mode_2_custom_selection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verifica che la TUI supporti sia '1. Tutto il pacchetto' sia '2. Solo alcune cose'."""
    executed_plans: list[tuple[list[ComponentId], bool]] = []

    def _fake_run_plan(
        selected: list[ComponentId],
        audits: dict[ComponentId, ComponentAudit] | None = None,
        *,
        force: bool = False,
        on_progress: object = None,
    ) -> list[StepExecutionResult]:
        _ = (audits, on_progress)
        executed_plans.append((list(selected), force))
        return [
            StepExecutionResult(c, StepStatus.COMPLETED, "OK") for c in selected
        ]

    monkeypatch.setattr(installer, "audit_all_components", lambda: _make_fake_audits(all_installed=True))
    monkeypatch.setattr(installer, "run_installation_plan", _fake_run_plan)

    # Test Opzione 1 (Tutto il pacchetto): tasto '1' nel menu principale
    keys_mode_1 = iter(["1"])
    tui1 = InstallerTUI(key_reader=lambda: next(keys_mode_1))
    rc1 = tui1.run_interactive()
    assert rc1 == 0
    assert len(executed_plans) == 1
    assert executed_plans[0][0] == list(ALL_COMPONENTS)
    assert executed_plans[0][1] is False

    # Test Opzione 2 (Solo alcune cose):
    # '2' entra nel menu custom -> 'N' deseleziona tutti -> '1' seleziona Python -> '3' seleziona Ollama -> 'F' attiva force -> 'ENTER' avvia
    executed_plans.clear()
    keys_mode_2 = iter(["2", "N", "1", "3", "F", "ENTER"])
    tui2 = InstallerTUI(key_reader=lambda: next(keys_mode_2))
    rc2 = tui2.run_interactive()
    assert rc2 == 0
    assert len(executed_plans) == 1
    assert executed_plans[0][0] == [ComponentId.PYTHON_LIBS, ComponentId.OLLAMA_AND_LLM]
    assert executed_plans[0][1] is True


def test_sample_audio_generation_and_end_to_end_verification(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verifica generazione del campione WAV e test end-to-end (FFmpeg + Whisper + Ollama + DOCX)."""
    sample_wav = tmp_path / "sample_test.wav"
    monkeypatch.setattr(installer, "_synthesize_speech_wav_windows", lambda _p, _t: False)

    generated = ensure_sample_audio_wav(sample_wav)
    assert generated.is_file()
    with wave.open(str(generated), "rb") as wf:
        assert wf.getnchannels() == 1
        assert wf.getsampwidth() == 2
        assert wf.getframerate() == 16000
        assert wf.getnframes() >= 16000

    # Mock per execute_step_sample_audio_test isolato
    monkeypatch.setattr(installer, "SAMPLE_AUDIO_PATH", sample_wav)
    monkeypatch.setattr(bootstrap, "TEMP_DIR", tmp_path)
    monkeypatch.setattr(bootstrap, "ENV_MARKER_PATH", tmp_path / ".env_ready.json")
    monkeypatch.setattr(
        bootstrap,
        "resolve_ffmpeg",
        lambda _b=None: (Path("C:/ffmpeg.exe"), "ffmpeg version 9.0"),
    )
    monkeypatch.setattr(
        "modules.audio_utils.normalize_wav_with_ffmpeg",
        lambda _ff, src, dst, delete_raw_on_success=False: Path(dst).write_bytes(
            Path(src).read_bytes()
        ),
    )
    monkeypatch.setattr(
        "modules.audio_utils.is_valid_normalized_wav",
        lambda _p: True,
    )
    monkeypatch.setattr(
        installer,
        "_detect_preferred_whisper_target",
        lambda: ("medium", "cpu", "int8"),
    )

    class _FakeSeg:
        text = "Buongiorno, riunione di test Verbalia completata."

    class _FakeWhisperModel:
        def __init__(self, *_a: object, **_kw: object) -> None:
            pass

        def transcribe(self, *_a: object, **_kw: object) -> tuple[list[_FakeSeg], object]:
            return ([_FakeSeg()], object())

    monkeypatch.setattr("faster_whisper.WhisperModel", _FakeWhisperModel)
    monkeypatch.setattr(bootstrap, "ensure_ollama_ready", lambda *_a, **_kw: None)

    class _FakeOllamaResp(io.BytesIO):
        def __enter__(self) -> _FakeOllamaResp:
            return self

        def __exit__(self, *_a: object) -> None:
            pass

    monkeypatch.setattr(
        installer.urllib.request,
        "urlopen",
        lambda *_a, **_kw: _FakeOllamaResp(
            json.dumps({"response": "Sintesi riunione di verifica installazione OK."}).encode(
                "utf-8"
            )
        ),
    )

    logs: list[str] = []
    audit = ComponentAudit(ComponentId.SAMPLE_AUDIO_TEST, installed=False, summary="Da testare")
    res = execute_step_sample_audio_test(audit, force=False, log=logs.append)
    assert res.status == StepStatus.COMPLETED
    assert (tmp_path / ".env_ready.json").is_file()
    assert any("Test 3/3 [Ollama + DOCX]: OK" in line for line in logs)


def test_parse_component_list_and_run_plan() -> None:
    """Verifica il parsing della lista componenti CLI (--only)."""
    parsed = parse_component_list("1,whisper,ollama,4")
    assert parsed == [
        ComponentId.PYTHON_LIBS,
        ComponentId.WHISPER_AND_DEPS,
        ComponentId.OLLAMA_AND_LLM,
        ComponentId.SAMPLE_AUDIO_TEST,
    ]
    with pytest.raises(ValueError, match="non valido"):
        parse_component_list("componente_inesistente")
