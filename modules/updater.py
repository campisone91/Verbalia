from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
import zipfile
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Final

import config as _config
from modules.contracts import UiCommandType, atomic_write_json, now_tz, replace_with_retry
from modules.job_registry import JobRegistry, parse_iso
from modules.logging_setup import get_logger
from modules.single_instance import APP_MUTEX_NAME, SingleInstance

logger = get_logger("updater")

# File e directory nella radice del progetto che NON devono mai essere sovrascritti o rimossi
PROTECTED_TOP_LEVEL_NAMES: Final[frozenset[str]] = frozenset(
    {
        ".git",
        ".venv",
        "venv",
        "env",
        "data",
        "output",
        "temp",
        "logs",
        "bin",
        "models",
        ".cache",
        "__pycache__",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
        ".vscode",
        ".idea",
        "settings.json",
        ".env_ready.json",
        ".user_consent.json",
        ".env",
        ".coverage",
    }
)

# File core che devono esistere nell'archivio estratto per considerarlo un aggiornamento valido
REQUIRED_PACKAGE_FILES: Final[tuple[str, ...]] = (
    "main.py",
    "config.py",
    "bootstrap.py",
    "modules/__init__.py",
)

_SEMVER_RE: Final[re.Pattern[str]] = re.compile(
    r"^\s*[vV]?(\d+(?:\.\d+)*)(?:[-+]([0-9A-Za-z.-]+))?\s*$"
)


def normalize_version_string(version_str: str) -> str:
    """Pulisce una stringa di versione rimuovendo spazi e prefisso 'v'/'V'."""
    cleaned = str(version_str or "").strip()
    if cleaned.lower().startswith("v"):
        cleaned = cleaned[1:].strip()
    return cleaned


def parse_version(version_str: str) -> tuple[tuple[int, ...], str]:
    """Estrae la tupla numerica (major, minor, patch, ...) e l'eventuale suffisso pre-release."""
    raw = str(version_str or "").strip()
    match = _SEMVER_RE.match(raw)
    if not match:
        digits = re.findall(r"\d+", raw)
        if not digits:
            return ((0, 0, 0), "")
        nums = tuple(int(d) for d in digits[:4])
        return (nums, "")
    num_part = match.group(1)
    prerelease = (match.group(2) or "").strip()
    nums = tuple(int(part) for part in num_part.split("."))
    return (nums, prerelease)


def is_newer_version(candidate: str, current: str) -> bool:
    """Restituisce True se `candidate` è semanticamente più recente di `current`."""
    cand_nums, cand_pre = parse_version(candidate)
    curr_nums, curr_pre = parse_version(current)

    max_len = max(len(cand_nums), len(curr_nums), 3)
    cand_padded = cand_nums + (0,) * (max_len - len(cand_nums))
    curr_padded = curr_nums + (0,) * (max_len - len(curr_nums))

    if cand_padded != curr_padded:
        return cand_padded > curr_padded

    # A parità di numeri: una release stabile (senza pre-release) è più recente di una pre-release
    if curr_pre and not cand_pre:
        return True
    if not curr_pre and cand_pre:
        return False
    if curr_pre and cand_pre:
        return cand_pre > curr_pre
    return False


def extract_changelog_section(changelog_text: str, version: str) -> str:
    """Estrae le note di rilascio per una specifica versione da un testo in formato Keep a Changelog."""
    target = normalize_version_string(version)
    lines = changelog_text.splitlines()
    collecting = False
    collected: list[str] = []

    header_re = re.compile(r"^##\s+\[?[vV]?([0-9]+(?:\.[0-9]+)*[^\]]*)\]?")
    for line in lines:
        m = header_re.match(line.strip())
        if m:
            found_ver = normalize_version_string(m.group(1).split()[0])
            if collecting:
                break
            if not target or found_ver == target:
                collecting = True
                continue
        elif collecting:
            collected.append(line)

    result = "\n".join(collected).strip()
    return result


@dataclass(slots=True)
class UpdateInfo:
    """Stato e metadati relativi al controllo aggiornamenti su GitHub."""

    current_version: str
    latest_version: str
    update_available: bool
    release_url: str = ""
    download_url: str = ""
    release_notes: str = ""
    published_at: str = ""
    checked_at: str = ""
    error: str | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "current_version": self.current_version,
            "latest_version": self.latest_version,
            "update_available": self.update_available,
            "release_url": self.release_url,
            "download_url": self.download_url,
            "release_notes": self.release_notes,
            "published_at": self.published_at,
            "checked_at": self.checked_at,
            "error": self.error,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, object]) -> UpdateInfo:
        curr = str(data.get("current_version") or _config.APP_VERSION)
        latest = str(data.get("latest_version") or curr)
        avail_raw = data.get("update_available")
        avail = bool(avail_raw) if isinstance(avail_raw, bool) else is_newer_version(latest, curr)
        err_raw = data.get("error")
        return cls(
            current_version=curr,
            latest_version=latest,
            update_available=avail,
            release_url=str(data.get("release_url") or ""),
            download_url=str(data.get("download_url") or ""),
            release_notes=str(data.get("release_notes") or ""),
            published_at=str(data.get("published_at") or ""),
            checked_at=str(data.get("checked_at") or ""),
            error=str(err_raw) if isinstance(err_raw, str) and err_raw else None,
        )


@dataclass(slots=True)
class UpdateResult:
    """Risultato dell'operazione di download e installazione di un aggiornamento."""

    success: bool
    installed_version: str
    files_updated: int = 0
    pip_updated: bool = False
    message: str = ""
    error: str | None = None


def _http_get_json(url: str, timeout: float) -> object:
    """Esegue una richiesta HTTP GET alle API GitHub e decodifica il payload JSON."""
    if not url.lower().startswith("https://"):
        raise ValueError(f"L'URL di controllo aggiornamento deve usare HTTPS: {url}")
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": f"{_config.APP_NAME}/{_config.APP_VERSION}",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    req = urllib.request.Request(url, headers=headers, method="GET")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read().decode("utf-8", errors="replace")
    return json.loads(raw)


def _http_get_text(url: str, timeout: float) -> str | None:
    """Scarica un file di testo via HTTPS (es. CHANGELOG.md); restituisce None in caso di errore."""
    if not url.lower().startswith("https://"):
        return None
    headers = {"User-Agent": f"{_config.APP_NAME}/{_config.APP_VERSION}"}
    req = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read().decode("utf-8", errors="replace")
    except Exception:
        return None


def _check_via_atom_and_raw(
    slug: str,
    curr_ver: str,
    checked_iso: str,
    timeout: float,
) -> UpdateInfo | None:
    """Fallback immune al rate limit di `api.github.com` (60 req/ora su IP aziendali condivisi).

    Legge il feed pubblico `https://github.com/<slug>/tags.atom` e il file `pyproject.toml`
    su `raw.githubusercontent.com`, utilizzando gli archivi ZIP diretti `github.com/<slug>/archive/...`.
    """
    html_base = f"https://github.com/{slug}"
    tags_atom_url = f"{html_base}/tags.atom"
    raw_pyproject_url = f"https://raw.githubusercontent.com/{slug}/main/pyproject.toml"

    best_tag: str = ""
    best_ver: str = ""
    published_at: str = ""
    notes: str = ""

    atom_xml = _http_get_text(tags_atom_url, timeout=timeout)
    if atom_xml:
        try:
            root = ET.fromstring(atom_xml)
            ns = {"atom": "http://www.w3.org/2005/Atom"}
            entries = root.findall("atom:entry", ns) or root.findall("entry")
            for entry in entries:
                title_el = entry.find("atom:title", ns)
                if title_el is None:
                    title_el = entry.find("title")
                updated_el = entry.find("atom:updated", ns)
                if updated_el is None:
                    updated_el = entry.find("updated")
                content_el = entry.find("atom:content", ns)
                if content_el is None:
                    content_el = entry.find("content")

                cand_tag = (title_el.text or "").strip() if title_el is not None else ""
                if not cand_tag:
                    continue
                cand_ver = normalize_version_string(cand_tag)
                if not best_ver or is_newer_version(cand_ver, best_ver):
                    best_tag = cand_tag
                    best_ver = cand_ver
                    published_at = (
                        (updated_el.text or "").strip() if updated_el is not None else ""
                    )
                    raw_content = (
                        (content_el.text or "").strip() if content_el is not None else ""
                    )
                    if raw_content:
                        notes = re.sub(r"<[^>]+>", "", raw_content).strip()
        except Exception as xml_exc:
            logger.debug("Parsing tags.atom non riuscito: %s", xml_exc)

    # Verifica anche pyproject.toml su branch main (nel caso in cui sia stata rilasciata una versione su main)
    raw_pyproject = _http_get_text(raw_pyproject_url, timeout=timeout)
    main_ver = ""
    if raw_pyproject:
        m = re.search(r'(?m)^\s*version\s*=\s*"([^"]+)"', raw_pyproject)
        if m:
            main_ver = normalize_version_string(m.group(1))

    use_main_branch = False
    if main_ver and (not best_ver or is_newer_version(main_ver, best_ver)):
        best_ver = main_ver
        best_tag = f"v{main_ver}"
        use_main_branch = True

    if not best_ver:
        return None

    ref_for_changelog = "main" if use_main_branch else best_tag
    if not notes:
        raw_cl = _http_get_text(
            f"https://raw.githubusercontent.com/{slug}/{ref_for_changelog}/CHANGELOG.md",
            timeout=timeout,
        )
        if raw_cl:
            notes = extract_changelog_section(raw_cl, best_ver)

    if use_main_branch:
        download_url = f"{html_base}/archive/refs/heads/main.zip"
        release_url = html_base
    else:
        download_url = f"{html_base}/archive/refs/tags/{best_tag}.zip"
        release_url = f"{html_base}/releases/tag/{best_tag}"

    newer = is_newer_version(best_ver, curr_ver)
    return UpdateInfo(
        current_version=curr_ver,
        latest_version=best_ver,
        update_available=newer,
        release_url=release_url,
        download_url=download_url,
        release_notes=notes,
        published_at=published_at,
        checked_at=checked_iso,
        error=None,
    )


def check_for_updates(
    current_version: str | None = None,
    repo_slug: str | None = None,
    timeout: float | None = None,
) -> UpdateInfo:
    """Controlla su GitHub se esiste una versione più recente rispetto a quella in esecuzione.

    Interroga prima `/releases/latest`, poi `/tags` e, in caso di rate limit HTTP 403/429
    su `api.github.com` (frequente su reti aziendali con IP condiviso), utilizza
    automaticamente il feed diretto `github.com/<slug>/tags.atom` e `raw.githubusercontent.com`.
    """
    curr_ver = normalize_version_string(current_version or _config.APP_VERSION)
    slug = (repo_slug or _config.GITHUB_REPO_SLUG).strip("/")
    eff_timeout = float(timeout if timeout is not None else _config.UPDATE_CHECK_TIMEOUT_SEC)
    checked_iso = now_tz().isoformat()

    release_url_api = f"https://api.github.com/repos/{slug}/releases/latest"
    tags_url_api = f"https://api.github.com/repos/{slug}/tags"
    html_base = f"https://github.com/{slug}"
    api_http_code: int | None = None
    last_error_msg: str | None = None

    try:
        payload = _http_get_json(release_url_api, timeout=eff_timeout)
        if isinstance(payload, dict) and payload.get("tag_name"):
            raw_tag = str(payload["tag_name"]).strip()
            latest_ver = normalize_version_string(raw_tag)
            html_url = str(payload.get("html_url") or f"{html_base}/releases/tag/{raw_tag}")
            zipball = str(
                payload.get("zipball_url")
                or f"https://github.com/{slug}/archive/refs/tags/{raw_tag}.zip"
            )
            notes = str(payload.get("body") or "").strip()
            published_at = str(payload.get("published_at") or "")
            if not notes:
                raw_cl = _http_get_text(
                    f"https://raw.githubusercontent.com/{slug}/{raw_tag}/CHANGELOG.md",
                    timeout=eff_timeout,
                )
                if raw_cl:
                    notes = extract_changelog_section(raw_cl, latest_ver)
            newer = is_newer_version(latest_ver, curr_ver)
            return UpdateInfo(
                current_version=curr_ver,
                latest_version=latest_ver,
                update_available=newer,
                release_url=html_url,
                download_url=zipball,
                release_notes=notes,
                published_at=published_at,
                checked_at=checked_iso,
                error=None,
            )
    except urllib.error.HTTPError as http_exc:
        api_http_code = http_exc.code
        last_error_msg = f"Errore API GitHub (HTTP {http_exc.code})"
        logger.debug(
            "Endpoint releases/latest ha restituito HTTP %s, avvio fallback: %s",
            http_exc.code,
            http_exc,
        )
    except Exception as exc:
        last_error_msg = f"Impossibile contattare GitHub: {exc}"
        logger.debug("Controllo su releases/latest non riuscito, provo fallback: %s", exc)

    # Fallback 1: API /tags (solo se non siamo già in rate-limit 403/429 su api.github.com)
    if api_http_code == 404:
        try:
            tags_payload = _http_get_json(tags_url_api, timeout=eff_timeout)
            if isinstance(tags_payload, list) and tags_payload:
                valid_tags: list[tuple[str, dict[str, object]]] = []
                for entry in tags_payload:
                    if isinstance(entry, dict) and isinstance(entry.get("name"), str):
                        tag_name = str(entry["name"]).strip()
                        if tag_name:
                            valid_tags.append((tag_name, entry))
                if valid_tags:
                    best_tag, best_entry = valid_tags[0]
                    for cand_tag, cand_entry in valid_tags[1:]:
                        if is_newer_version(cand_tag, best_tag):
                            best_tag, best_entry = cand_tag, cand_entry

                    latest_ver = normalize_version_string(best_tag)
                    zipball = str(
                        best_entry.get("zipball_url")
                        or f"https://github.com/{slug}/archive/refs/tags/{best_tag}.zip"
                    )
                    html_url = f"{html_base}/releases/tag/{best_tag}"
                    notes = ""
                    raw_cl = _http_get_text(
                        f"https://raw.githubusercontent.com/{slug}/{best_tag}/CHANGELOG.md",
                        timeout=eff_timeout,
                    )
                    if raw_cl:
                        notes = extract_changelog_section(raw_cl, latest_ver)
                    newer = is_newer_version(latest_ver, curr_ver)
                    return UpdateInfo(
                        current_version=curr_ver,
                        latest_version=latest_ver,
                        update_available=newer,
                        release_url=html_url,
                        download_url=zipball,
                        release_notes=notes,
                        published_at="",
                        checked_at=checked_iso,
                        error=None,
                    )
        except Exception as exc:
            last_error_msg = f"Impossibile verificare i tag su GitHub: {exc}"
            logger.debug("Fallback /tags GitHub non riuscito: %s", exc)

    # Fallback 2 (universale e immune a HTTP 403 rate limit di api.github.com):
    # lettura diretta di github.com/<slug>/tags.atom + raw.githubusercontent.com
    direct_info = _check_via_atom_and_raw(slug, curr_ver, checked_iso, timeout=eff_timeout)
    if direct_info is not None:
        return direct_info

    if api_http_code == 404:
        return UpdateInfo(
            current_version=curr_ver,
            latest_version=curr_ver,
            update_available=False,
            release_url=html_base,
            checked_at=checked_iso,
            error=None,
        )

    return UpdateInfo(
        current_version=curr_ver,
        latest_version=curr_ver,
        update_available=False,
        release_url=html_base,
        checked_at=checked_iso,
        error=last_error_msg or "Impossibile verificare la presenza di aggiornamenti su GitHub.",
    )


def _is_protected_relative_path(rel_path: Path) -> bool:
    """Verifica se un percorso relativo appartiene ai dati utente/runtime protetti."""
    parts = rel_path.parts
    if not parts:
        return True
    top = parts[0]
    if top in PROTECTED_TOP_LEVEL_NAMES:
        return True
    if top.startswith(".env_ready"):
        return True
    if len(parts) == 1 and top.upper().startswith("ROADMAP") and top.lower().endswith(".md"):
        return True
    if any(p == "__pycache__" or p.endswith(".pyc") for p in parts):
        return True
    return False


def _safe_extract_zip(archive_path: Path, extract_dir: Path) -> None:
    """Estrae un archivio ZIP verificando che nessun membro contenga path traversal (ZipSlip)."""
    base_resolved = extract_dir.resolve()
    with zipfile.ZipFile(archive_path, "r") as zf:
        for member in zf.infolist():
            member_path = Path(member.filename)
            if member_path.is_absolute() or ".." in member_path.parts:
                raise RuntimeError(
                    f"Rilevato path traversal nell'archivio di aggiornamento: {member.filename}"
                )
            target_resolved = (base_resolved / member_path).resolve()
            if (
                target_resolved != base_resolved
                and base_resolved not in target_resolved.parents
            ):
                raise RuntimeError(
                    f"Destinazione ZIP esterna alla directory consentita: {member.filename}"
                )
        zf.extractall(base_resolved)


def _locate_extracted_package_root(extract_dir: Path) -> Path:
    """Individua la directory radice del codice estratto (gestisce la cartella wrapper di GitHub)."""
    if all((extract_dir / req).is_file() for req in REQUIRED_PACKAGE_FILES):
        return extract_dir

    subdirs = [d for d in extract_dir.iterdir() if d.is_dir()]
    if len(subdirs) == 1 and all((subdirs[0] / req).is_file() for req in REQUIRED_PACKAGE_FILES):
        return subdirs[0]

    for candidate in subdirs:
        if all((candidate / req).is_file() for req in REQUIRED_PACKAGE_FILES):
            return candidate

    raise RuntimeError(
        "L'archivio scaricato non contiene la struttura valida di Verbalia "
        f"(file richiesti mancanti: {', '.join(REQUIRED_PACKAGE_FILES)})."
    )


def _sha256_file_if_exists(path: Path) -> str:
    if not path.is_file():
        return ""
    digest = hashlib.sha256()
    with path.open("rb") as f:
        while True:
            chunk = f.read(65536)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _update_env_marker_version(
    project_root: Path,
    new_version: str,
    new_req_sha256: str,
) -> None:
    """Allinea `.env_ready.json` alla nuova versione installata per evitare falsi mismatch al riavvio."""
    marker_path = project_root / ".env_ready.json"
    if not marker_path.is_file():
        return
    try:
        payload = json.loads(marker_path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            return
        payload["app_version"] = normalize_version_string(new_version)
        if new_req_sha256:
            payload["requirements_sha256"] = new_req_sha256
        atomic_write_json(marker_path, payload)
    except Exception as exc:
        logger.debug("Aggiornamento .env_ready.json post-update non riuscito: %s", exc)


def download_and_apply_update(
    info: UpdateInfo,
    project_root: Path | None = None,
    *,
    progress_callback: Callable[[int, str], None] | None = None,
    install_deps: bool = True,
    timeout: int | None = None,
) -> UpdateResult:
    """Scarica l'archivio della release da GitHub, valida e applica l'aggiornamento con backup/rollback.

    Preserva rigorosamente `settings.json`, `data/`, `output/`, `temp/`, `logs/`, `bin/` e `.git/`.
    """
    target_root = (project_root if project_root is not None else _config.PROJECT_ROOT).resolve()
    target_ver = normalize_version_string(info.latest_version or _config.APP_VERSION)
    download_url = info.download_url.strip()
    eff_timeout = int(timeout if timeout is not None else _config.UPDATE_DOWNLOAD_TIMEOUT_SEC)

    def _report(pct: int, msg: str) -> None:
        if progress_callback is not None:
            try:
                progress_callback(max(0, min(100, int(pct))), msg)
            except Exception:
                pass

    if not download_url or not download_url.lower().startswith("https://"):
        err = f"URL di download non valido o non sicuro: {download_url or '<vuoto>'}"
        return UpdateResult(
            success=False,
            installed_version=_config.APP_VERSION,
            message=err,
            error=err,
        )

    temp_base = target_root / "temp"
    temp_base.mkdir(parents=True, exist_ok=True)
    staging_dir = Path(tempfile.mkdtemp(prefix="update_staging_", dir=str(temp_base)))
    part_path = staging_dir / "release.zip.part"
    zip_path = staging_dir / "release.zip"
    extract_dir = staging_dir / "extracted"
    backup_dir = staging_dir / "backup"

    try:
        _report(5, f"Download di Verbalia v{target_ver} da GitHub in corso...")
        headers = {
            "User-Agent": f"{_config.APP_NAME}/{_config.APP_VERSION}",
            "Accept": "application/vnd.github+json, application/zip, */*",
        }
        req = urllib.request.Request(download_url, headers=headers, method="GET")
        with (
            urllib.request.urlopen(req, timeout=eff_timeout) as resp,
            part_path.open("wb") as handle,
        ):
            total_bytes_hdr = resp.headers.get("Content-Length") if hasattr(resp, "headers") else None
            total_bytes = int(total_bytes_hdr) if total_bytes_hdr and total_bytes_hdr.isdigit() else 0
            downloaded = 0
            while True:
                chunk = resp.read(65536)
                if not chunk:
                    break
                handle.write(chunk)
                downloaded += len(chunk)
                if total_bytes > 0:
                    dl_pct = 5 + int(40 * min(1.0, downloaded / total_bytes))
                    _report(dl_pct, f"Scaricati {downloaded // 1024} KB / {total_bytes // 1024} KB...")
            handle.flush()
            os.fsync(handle.fileno())

        os.replace(part_path, zip_path)
        _report(48, "Verifica ed estrazione dell'archivio...")

        extract_dir.mkdir(parents=True, exist_ok=True)
        _safe_extract_zip(zip_path, extract_dir)
        pkg_root = _locate_extracted_package_root(extract_dir)

        old_req_sha256 = _sha256_file_if_exists(target_root / "requirements.txt")

        # Raccolta dei file da aggiornare
        files_to_copy: list[tuple[Path, Path]] = []
        for src_file in pkg_root.rglob("*"):
            if not src_file.is_file():
                continue
            rel_path = src_file.relative_to(pkg_root)
            if _is_protected_relative_path(rel_path):
                continue
            files_to_copy.append((src_file, rel_path))

        if not files_to_copy:
            raise RuntimeError("Nessun file aggiornabile trovato nel pacchetto scaricato.")

        _report(60, f"Installazione di {len(files_to_copy)} file in corso...")
        backup_dir.mkdir(parents=True, exist_ok=True)
        backed_up: list[tuple[Path, Path]] = []
        newly_created: list[Path] = []
        updated_count = 0

        try:
            for idx, (src_file, rel_path) in enumerate(files_to_copy):
                dest_file = target_root / rel_path
                dest_file.parent.mkdir(parents=True, exist_ok=True)

                if dest_file.is_file():
                    bkp_file = backup_dir / rel_path
                    bkp_file.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(dest_file, bkp_file)
                    backed_up.append((bkp_file, dest_file))
                else:
                    newly_created.append(dest_file)

                tmp_dest = dest_file.with_name(f".{dest_file.name}.update_{os.getpid()}.tmp")
                shutil.copy2(src_file, tmp_dest)
                replace_with_retry(tmp_dest, dest_file)
                updated_count += 1

                if len(files_to_copy) > 0 and idx % 5 == 0:
                    step_pct = 60 + int(25 * (idx + 1) / len(files_to_copy))
                    _report(step_pct, f"Aggiornamento file ({idx + 1}/{len(files_to_copy)})...")
        except Exception as copy_exc:
            logger.error("Errore durante la copia dei file di aggiornamento, avvio rollback: %s", copy_exc)
            for new_f in newly_created:
                try:
                    if new_f.is_file():
                        new_f.unlink()
                except OSError:
                    pass
            for bkp_file, orig_dest in backed_up:
                try:
                    if bkp_file.is_file():
                        replace_with_retry(bkp_file, orig_dest)
                except OSError:
                    pass
            raise RuntimeError(
                f"Aggiornamento interrotto e ripristinato alla versione precedente: {copy_exc}"
            ) from copy_exc

        new_req_path = target_root / "requirements.txt"
        new_req_sha256 = _sha256_file_if_exists(new_req_path)
        pip_updated = False

        if install_deps and new_req_sha256 and new_req_sha256 != old_req_sha256:
            _report(88, "Aggiornamento dipendenze Python (requirements.txt)...")
            subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "pip",
                    "install",
                    "-r",
                    str(new_req_path),
                ],
                check=True,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                timeout=eff_timeout,
                creationflags=_config.windows_creation_flags(),
            )
            pip_updated = True

        _update_env_marker_version(target_root, target_ver, new_req_sha256)
        _config.APP_VERSION = target_ver

        _report(100, f"Aggiornamento a Verbalia v{target_ver} completato con successo!")
        logger.info(
            "Aggiornamento applicato con successo: v%s (%d file aggiornati, pip_updated=%s)",
            target_ver,
            updated_count,
            pip_updated,
        )
        return UpdateResult(
            success=True,
            installed_version=target_ver,
            files_updated=updated_count,
            pip_updated=pip_updated,
            message=f"Verbalia aggiornato alla versione v{target_ver} ({updated_count} file aggiornati).",
            error=None,
        )
    except Exception as exc:
        err_str = str(exc)
        logger.error("Download o installazione dell'aggiornamento fallita: %s", err_str)
        return UpdateResult(
            success=False,
            installed_version=_config.APP_VERSION,
            files_updated=0,
            pip_updated=False,
            message=f"Aggiornamento non riuscito: {err_str}",
            error=err_str,
        )
    finally:
        if staging_dir.exists():
            shutil.rmtree(staging_dir, ignore_errors=True)


def _is_pid_running(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        import psutil

        return bool(psutil.pid_exists(pid))
    except Exception:
        return False


def _resolve_python_gui_executable() -> str:
    """Individua `pythonw.exe` dello stesso ambiente Python corrente (con fallback a `sys.executable`)."""
    exe_path = Path(sys.executable).resolve()
    pythonw = exe_path.with_name("pythonw.exe")
    if pythonw.is_file():
        return str(pythonw)
    which_pw = shutil.which("pythonw") or shutil.which("pythonw.exe")
    if which_pw:
        return which_pw
    return str(exe_path)


def stop_background_instance(
    registry: JobRegistry | None = None,
    timeout_sec: float = 8.0,
) -> bool:
    """Invia il comando di uscita al processo background e attende il rilascio del mutex."""
    bg_pid = 0
    if registry is not None:
        try:
            status = registry.get_status()
            heartbeat = parse_iso(status.get("heartbeat"))
            alive = heartbeat is not None and (now_tz() - heartbeat).total_seconds() < 12.0
            raw_pid = status.get("pid")
            if isinstance(raw_pid, int) and not isinstance(raw_pid, bool):
                bg_pid = raw_pid
            if alive or bg_pid > 0:
                registry.enqueue_command(UiCommandType.EXIT.value)
        except Exception as exc:
            logger.debug("Invio comando EXIT al background non riuscito: %s", exc)

    deadline = time.monotonic() + max(1.0, timeout_sec)
    while time.monotonic() < deadline:
        probe = SingleInstance(APP_MUTEX_NAME)
        if probe.acquire():
            probe.release()
            if bg_pid <= 0 or not _is_pid_running(bg_pid):
                return True
        time.sleep(0.2)

    if bg_pid > 0 and _is_pid_running(bg_pid):
        try:
            import psutil

            proc = psutil.Process(bg_pid)
            proc.terminate()
            proc.wait(timeout=3.0)
            return True
        except Exception as exc:
            logger.debug("Terminazione forzata PID background %d non riuscita: %s", bg_pid, exc)

    return False


def spawn_restarted_processes(
    project_root: Path | None = None,
    *,
    start_background: bool = True,
    reopen_dashboard: bool = True,
    delay_sec: float = 1.0,
) -> None:
    """Avvia in un processo indipendente il rilancio di Verbalia (Background e/o Cruscotto)
    dopo un breve delay che consente alla finestra corrente di rilasciare il proprio mutex."""
    root = (project_root if project_root is not None else _config.PROJECT_ROOT).resolve()
    main_py = str(root / "main.py")
    gui_exe = _resolve_python_gui_executable()

    commands_to_launch: list[list[str]] = []
    env_setup = ""
    if start_background:
        commands_to_launch.append([gui_exe, main_py])
        if not reopen_dashboard:
            env_setup = "import os\nos.environ['VERBALIA_NO_INITIAL_DASHBOARD'] = '1'\n"
    elif reopen_dashboard:
        commands_to_launch.append([gui_exe, main_py, "--dashboard"])

    if not commands_to_launch:
        return

    launcher_code = (
        f"{env_setup}"
        "import subprocess, time\n"
        f"time.sleep({max(0.2, float(delay_sec)):.2f})\n"
        f"cwd = {str(root)!r}\n"
        f"flags = {int(_config.windows_creation_flags())}\n"
        f"for cmd in {commands_to_launch!r}:\n"
        "    subprocess.Popen(cmd, cwd=cwd, stdin=subprocess.DEVNULL, "
        "stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=flags)\n"
    )

    flags = int(_config.windows_creation_flags())
    detached_flag = int(getattr(subprocess, "DETACHED_PROCESS", 0))
    new_group_flag = int(getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))

    subprocess.Popen(
        [sys.executable, "-c", launcher_code],
        cwd=str(root),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=flags | detached_flag | new_group_flag,
    )
