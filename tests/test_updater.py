from __future__ import annotations

import io
import json
import urllib.error
import zipfile
from pathlib import Path
from typing import Any

import pytest

import config as _config
from modules.contracts import Stage
from modules.job_registry import JobRegistry
from modules.updater import (
    UpdateInfo,
    UpdateResult,
    check_for_updates,
    download_and_apply_update,
    extract_changelog_section,
    is_newer_version,
    normalize_version_string,
    parse_version,
    spawn_restarted_processes,
)


def _create_fake_release_zip(
    zip_path: Path,
    *,
    version: str = "1.0.3",
    wrapper_dir: str = "campisone91-Verbalia-abc1234",
    extra_files: dict[str, str] | None = None,
    malicious_member: str | None = None,
) -> Path:
    prefix = f"{wrapper_dir}/" if wrapper_dir else ""
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(f"{prefix}main.py", f'# main v{version}\nAPP_VERSION = "{version}"\n')
        zf.writestr(f"{prefix}config.py", f'# config v{version}\nAPP_VERSION: str = "{version}"\n')
        zf.writestr(f"{prefix}bootstrap.py", f'# bootstrap v{version}\nAPP_VERSION: str = "{version}"\n')
        zf.writestr(f"{prefix}modules/__init__.py", f'# modules v{version}\n')
        zf.writestr(f"{prefix}requirements.txt", "requests>=2.31.0,<3.0.0\n")
        if extra_files:
            for rel, content in extra_files.items():
                zf.writestr(f"{prefix}{rel}", content)
        if malicious_member:
            zf.writestr(malicious_member, "evil")
    return zip_path


def test_version_parsing_and_comparison() -> None:
    assert normalize_version_string(" v1.0.2 ") == "1.0.2"
    assert normalize_version_string("V2.1.0") == "2.1.0"
    assert parse_version("v1.0.2") == ((1, 0, 2), "")
    assert parse_version("1.2.0-beta.1") == ((1, 2, 0), "beta.1")

    assert is_newer_version("1.0.3", "1.0.2") is True
    assert is_newer_version("v1.1.0", "1.0.9") is True
    assert is_newer_version("2.0.0", "1.9.9") is True
    assert is_newer_version("1.0.2", "1.0.2") is False
    assert is_newer_version("1.0.1", "1.0.2") is False
    assert is_newer_version("1.0.2", "1.0.2-rc1") is True
    assert is_newer_version("1.0.2-rc1", "1.0.2") is False


def test_extract_changelog_section() -> None:
    sample_cl = (
        "# Changelog\n\n"
        "## [1.0.3] - 2026-10-30\n\n"
        "### Added\n"
        "- Nuovo sistema di auto-update dal pannello.\n\n"
        "## [1.0.2] - 2026-10-29\n\n"
        "### Fixed\n"
        "- Fix sincronizzazione storico.\n"
    )
    sec_103 = extract_changelog_section(sample_cl, "v1.0.3")
    assert "auto-update dal pannello" in sec_103
    assert "Fix sincronizzazione" not in sec_103

    sec_102 = extract_changelog_section(sample_cl, "1.0.2")
    assert "Fix sincronizzazione" in sec_102


def test_check_for_updates_from_latest_release(monkeypatch: pytest.MonkeyPatch) -> None:
    def _fake_http_json(url: str, timeout: float) -> object:
        assert "releases/latest" in url
        return {
            "tag_name": "v1.0.5",
            "html_url": "https://github.com/campisone91/Verbalia/releases/tag/v1.0.5",
            "zipball_url": "https://api.github.com/repos/campisone91/Verbalia/zipball/v1.0.5",
            "body": "### Novità\n- Miglioramenti generali",
            "published_at": "2026-10-30T10:00:00Z",
        }

    monkeypatch.setattr("modules.updater._http_get_json", _fake_http_json)
    info = check_for_updates(current_version="1.0.2")
    assert info.update_available is True
    assert info.current_version == "1.0.2"
    assert info.latest_version == "1.0.5"
    assert "Miglioramenti generali" in info.release_notes
    assert info.error is None

    # Roundtrip serializzazione
    reconstructed = UpdateInfo.from_dict(info.to_dict())
    assert reconstructed.latest_version == "1.0.5"
    assert reconstructed.update_available is True


def test_check_for_updates_fallback_to_tags_on_404(monkeypatch: pytest.MonkeyPatch) -> None:
    def _fake_http_json(url: str, timeout: float) -> object:
        if "releases/latest" in url:
            raise urllib.error.HTTPError(url, 404, "Not Found", {}, None)  # type: ignore[arg-type]
        if "/tags" in url:
            return [
                {"name": "v1.0.1", "zipball_url": "https://api.github.com/zipball/v1.0.1"},
                {"name": "v1.0.4", "zipball_url": "https://api.github.com/zipball/v1.0.4"},
                {"name": "v1.0.2", "zipball_url": "https://api.github.com/zipball/v1.0.2"},
            ]
        raise AssertionError(f"Unexpected URL: {url}")

    monkeypatch.setattr("modules.updater._http_get_json", _fake_http_json)
    monkeypatch.setattr(
        "modules.updater._http_get_text",
        lambda url, timeout: "## [1.0.4] - 2026-10-30\n- Note da CHANGELOG.md\n",
    )

    info = check_for_updates(current_version="1.0.2")
    assert info.update_available is True
    assert info.latest_version == "1.0.4"
    assert info.download_url == "https://api.github.com/zipball/v1.0.4"
    assert "Note da CHANGELOG.md" in info.release_notes
    assert info.error is None


def test_check_for_updates_fallback_to_atom_on_403_rate_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _fake_http_json(url: str, timeout: float) -> object:
        raise urllib.error.HTTPError(url, 403, "rate limit exceeded", {}, None)  # type: ignore[arg-type]

    sample_atom = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <entry>
    <title>v1.0.6</title>
    <updated>2026-10-30T12:00:00Z</updated>
    <content></content>
  </entry>
  <entry>
    <title>v1.0.2</title>
    <updated>2026-10-29T14:25:15Z</updated>
    <content></content>
  </entry>
</feed>"""

    def _fake_http_text(url: str, timeout: float) -> str | None:
        if url.endswith("/tags.atom"):
            return sample_atom
        if url.endswith("pyproject.toml"):
            return '[project]\nname = "verbalia"\nversion = "1.0.6"\n'
        if url.endswith("CHANGELOG.md"):
            return "## [1.0.6] - 2026-10-30\n- Fix HTTP 403 rate limit con fallback tags.atom\n"
        return None

    monkeypatch.setattr("modules.updater._http_get_json", _fake_http_json)
    monkeypatch.setattr("modules.updater._http_get_text", _fake_http_text)

    info = check_for_updates(current_version="1.0.2")
    assert info.error is None
    assert info.update_available is True
    assert info.latest_version == "1.0.6"
    assert info.download_url == "https://github.com/campisone91/Verbalia/archive/refs/tags/v1.0.6.zip"
    assert "Fix HTTP 403 rate limit" in info.release_notes


def test_download_and_apply_update_preserves_user_data_and_updates_code(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_dir = tmp_path / "verbalia_install"
    project_dir.mkdir(parents=True)
    (project_dir / "modules").mkdir()
    (project_dir / "data").mkdir()
    (project_dir / "output").mkdir()
    (project_dir / "bin").mkdir()

    # File iniziali v1.0.2 e file utente protetti
    (project_dir / "main.py").write_text("# old main 1.0.2\n", encoding="utf-8")
    (project_dir / "config.py").write_text('APP_VERSION: str = "1.0.2"\n', encoding="utf-8")
    (project_dir / "bootstrap.py").write_text('APP_VERSION: str = "1.0.2"\n', encoding="utf-8")
    (project_dir / "modules" / "__init__.py").write_text("# old init\n", encoding="utf-8")
    (project_dir / "requirements.txt").write_text("requests>=2.31.0,<3.0.0\n", encoding="utf-8")

    # File protetti che NON devono essere sovrascritti
    (project_dir / "settings.json").write_text('{"custom": "user_value"}', encoding="utf-8")
    (project_dir / "data" / "history.db").write_bytes(b"sqlite_user_history")
    (project_dir / "output" / "verbale_importante.docx").write_bytes(b"docx_content")
    (project_dir / "bin" / "ffmpeg.exe").write_bytes(b"ffmpeg_binary")
    (project_dir / "ROADMAP_EVOLUTIVE.md").write_text("Appunti privati utente", encoding="utf-8")
    (project_dir / ".env_ready.json").write_text(
        json.dumps({"app_version": "1.0.2", "requirements_sha256": "old"}),
        encoding="utf-8",
    )

    zip_file = _create_fake_release_zip(
        tmp_path / "release_103.zip",
        version="1.0.3",
        extra_files={
            "settings.json": '{"custom": "OVERWRITTEN_BAD"}',
            "data/history.db": "OVERWRITTEN_BAD",
            "ROADMAP_EVOLUTIVE.md": "OVERWRITTEN_BAD",
            "modules/new_feature.py": "# nuovo modulo 1.0.3\n",
        },
    )
    zip_bytes = zip_file.read_bytes()

    class _FakeHttpResponse(io.BytesIO):
        def __init__(self, data: bytes) -> None:
            super().__init__(data)
            self.headers = {"Content-Length": str(len(data))}

        def __enter__(self) -> _FakeHttpResponse:
            return self

        def __exit__(self, *args: object) -> None:
            self.close()

    monkeypatch.setattr(
        "urllib.request.urlopen",
        lambda req, timeout=120: _FakeHttpResponse(zip_bytes),
    )
    saved_ver = _config.APP_VERSION
    try:
        progress_steps: list[tuple[int, str]] = []
        info = UpdateInfo(
            current_version="1.0.2",
            latest_version="1.0.3",
            update_available=True,
            download_url="https://api.github.com/repos/campisone91/Verbalia/zipball/v1.0.3",
        )
        res = download_and_apply_update(
            info,
            project_root=project_dir,
            progress_callback=lambda pct, msg: progress_steps.append((pct, msg)),
            install_deps=False,
        )
        assert res.success is True
        assert res.installed_version == "1.0.3"
        assert res.files_updated >= 5
        assert progress_steps and progress_steps[-1][0] == 100

        # I file di codice sono stati aggiornati alla 1.0.3
        assert "1.0.3" in (project_dir / "main.py").read_text(encoding="utf-8")
        assert (project_dir / "modules" / "new_feature.py").is_file()

        # I file utente e runtime sono rimasti intatti!
        assert (project_dir / "settings.json").read_text(encoding="utf-8") == '{"custom": "user_value"}'
        assert (project_dir / "data" / "history.db").read_bytes() == b"sqlite_user_history"
        assert (project_dir / "output" / "verbale_importante.docx").read_bytes() == b"docx_content"
        assert (project_dir / "bin" / "ffmpeg.exe").read_bytes() == b"ffmpeg_binary"
        assert (project_dir / "ROADMAP_EVOLUTIVE.md").read_text(encoding="utf-8") == "Appunti privati utente"

        # Il marker .env_ready.json ha ora app_version == 1.0.3
        marker = json.loads((project_dir / ".env_ready.json").read_text(encoding="utf-8"))
        assert marker["app_version"] == "1.0.3"
    finally:
        _config.APP_VERSION = saved_ver


def test_download_and_apply_update_rejects_zip_slip_and_rolls_back_on_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_dir = tmp_path / "verbalia_sec"
    project_dir.mkdir(parents=True)
    (project_dir / "main.py").write_text("# original main\n", encoding="utf-8")

    # 1. Archivio con path traversal (ZipSlip)
    bad_zip = _create_fake_release_zip(
        tmp_path / "bad.zip",
        version="1.0.3",
        malicious_member="../escaped.txt",
    )
    bad_bytes = bad_zip.read_bytes()

    class _FakeResp(io.BytesIO):
        def __init__(self, data: bytes) -> None:
            super().__init__(data)
            self.headers = {"Content-Length": str(len(data))}

        def __enter__(self) -> _FakeResp:
            return self

        def __exit__(self, *args: object) -> None:
            self.close()

    monkeypatch.setattr("urllib.request.urlopen", lambda req, timeout=120: _FakeResp(bad_bytes))

    info = UpdateInfo(
        current_version="1.0.2",
        latest_version="1.0.3",
        update_available=True,
        download_url="https://api.github.com/repos/campisone91/Verbalia/zipball/v1.0.3",
    )
    res = download_and_apply_update(info, project_root=project_dir, install_deps=False)
    assert res.success is False
    assert "path traversal" in (res.error or "").lower()
    assert (project_dir / "main.py").read_text(encoding="utf-8") == "# original main\n"

    # 2. Rollback automatico se replace_with_retry fallisce a metà copia
    good_zip = _create_fake_release_zip(tmp_path / "good.zip", version="1.0.3")
    good_bytes = good_zip.read_bytes()
    monkeypatch.setattr("urllib.request.urlopen", lambda req, timeout=120: _FakeResp(good_bytes))

    call_count = 0
    orig_replace = __import__("modules.updater", fromlist=["replace_with_retry"]).replace_with_retry

    def _failing_replace(src: Path, dst: Path, **kw: Any) -> None:
        nonlocal call_count
        call_count += 1
        if call_count == 2:
            raise OSError("Simulated disk write failure")
        orig_replace(src, dst, **kw)

    monkeypatch.setattr("modules.updater.replace_with_retry", _failing_replace)

    res2 = download_and_apply_update(info, project_root=project_dir, install_deps=False)
    assert res2.success is False
    assert (project_dir / "main.py").read_text(encoding="utf-8") == "# original main\n"


def test_dashboard_panel_displays_version_and_handles_update_and_restart(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from modules.dashboard_ui import DashboardApp

    registry = JobRegistry(tmp_path / "dash_upd.db")
    app = DashboardApp(None, registry, initial_tab="cruscotto")
    try:
        # 1. Verifica che la versione attuale sia visibile come testo nel pannello
        assert hasattr(app, "var_version")
        assert f"v{_config.APP_VERSION}" in str(app.var_version.get())
        assert "Versione:" in app.lbl_version.text()
        assert app.update_banner.isHidden() is True

        # 2. Simulazione rilevamento nuova versione v1.0.9
        fake_info = UpdateInfo(
            current_version=_config.APP_VERSION,
            latest_version="1.0.9",
            update_available=True,
            release_url="https://github.com/campisone91/Verbalia/releases/tag/v1.0.9",
            download_url="https://api.github.com/repos/campisone91/Verbalia/zipball/v1.0.9",
            release_notes="### Added\n- Aggiornamento rapido dal pannello",
        )
        confirmed_dialogs: list[str] = []

        def _confirm_record_no(title: str, message: str) -> bool:
            confirmed_dialogs.append(title)
            return False

        monkeypatch.setattr(app, "_confirm", _confirm_record_no)

        app._apply_update_info(fake_info, show_up_to_date_dialog=True)
        assert app.update_banner.isHidden() is False
        assert "v1.0.9" in str(app.var_update_banner.get())
        assert "disponibile v1.0.9" in str(app.var_version.get())
        assert confirmed_dialogs == ["Aggiornamento Disponibile"]

        # 3. Simulazione installazione completata e passaggio allo stato "Riavvia ora"
        monkeypatch.setattr(
            "modules.dashboard_ui._run_in_background",
            lambda _owner, work, done: done(work()),
        )
        monkeypatch.setattr(
            "modules.dashboard_ui.download_and_apply_update",
            lambda *_a, **_kw: UpdateResult(
                success=True,
                installed_version="1.0.9",
                files_updated=12,
                message="Verbalia aggiornato alla versione v1.0.9.",
            ),
        )
        restarted_calls: list[bool] = []
        monkeypatch.setattr(
            "modules.dashboard_ui.stop_background_instance",
            lambda *_a, **_kw: True,
        )
        monkeypatch.setattr(
            "modules.dashboard_ui.spawn_restarted_processes",
            lambda **_kw: restarted_calls.append(True),
        )

        # L'utente installa ma sceglie di non riavviare subito nella finestra di conferma
        monkeypatch.setattr(app, "_confirm", lambda title, message: False)
        app._cmd_install_update()
        assert app._update_installed_version == "1.0.9"
        assert app.btn_install_update.isHidden() is True
        assert app.btn_restart_after_update.isHidden() is False
        assert "v1.0.9" in str(app.var_version.get())

        # 4. Verifica protezione registrazione attiva durante il riavvio
        app._active_jobs = {
            "live_1": {
                "session_id": "live_1",
                "status": "recording",
                "stage": Stage.RECORDING.value,
            }
        }
        warn_titles: list[str] = []

        def _confirm_warn_no(title: str, message: str) -> bool:
            warn_titles.append(title)
            return False

        monkeypatch.setattr(app, "_confirm", _confirm_warn_no)
        app._cmd_restart_app(skip_confirm=True)
        assert warn_titles == ["Registrazione in corso"]
        assert restarted_calls == []

        # 5. Quando l'utente conferma il riavvio, ferma il background e rilancia i processi
        monkeypatch.setattr(app, "_confirm", lambda title, message: True)
        app._cmd_restart_app(skip_confirm=True)
        assert restarted_calls == [True]
        app._setting_vars.clear()
    finally:
        app.close()


def test_spawn_restarted_processes(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    launched_cmds: list[list[str]] = []

    def _fake_popen(cmd, **kwargs):
        launched_cmds.append(cmd)
        return None

    monkeypatch.setattr("subprocess.Popen", _fake_popen)
    spawn_restarted_processes(
        project_root=tmp_path,
        start_background=True,
        reopen_dashboard=True,
    )
    assert len(launched_cmds) == 1
    launcher_script = launched_cmds[0][2]
    assert "main.py" in launcher_script


def test_orchestrator_run_auto_launches_dashboard(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from modules.orchestrator import MeetingScribeApplication
    from modules.settings import default_settings

    launched: list[bool] = []
    app = MeetingScribeApplication(
        settings=default_settings(),
        output_dir=tmp_path / "output",
        temp_dir=tmp_path / "temp",
        headless=False,
    )
    def _fake_launch(tab=None):
        launched.append(True)
        app.shutdown_event.set()

    monkeypatch.setattr(app, "launch_dashboard", _fake_launch)
    monkeypatch.setattr(app, "_start_services", lambda: None)
    monkeypatch.setattr(app.tray, "set_recording_status", lambda *a, **k: None)
    monkeypatch.setattr(app, "_recover_pending_sessions", lambda: None)

    class _FakeWorker:
        def start(self) -> None:
            pass

    monkeypatch.setattr(app, "_make_worker", lambda: _FakeWorker())
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.delenv("TEAMS_SCRIBE_HEADLESS_TRAY", raising=False)
    monkeypatch.delenv("VERBALIA_NO_INITIAL_DASHBOARD", raising=False)

    class _FakeWatcher:
        is_call_active = False
        def step(self) -> None:
            pass

    monkeypatch.setattr("modules.orchestrator.TeamsCallWatcher", lambda **k: _FakeWatcher())
    monkeypatch.setattr(app, "_perform_coordinated_shutdown", lambda: None)

    ret = app.run_auto()
    assert ret == 0
    assert launched == [True]

