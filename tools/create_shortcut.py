"""Crea un collegamento sul Desktop per Verbalia."""
from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path


def get_desktop_dir() -> Path:
    userprofile = os.environ.get("USERPROFILE", "")
    if userprofile:
        # Controlla prima Desktop in OneDrive
        onedrive_desktop = Path(userprofile) / "OneDrive" / "Desktop"
        if onedrive_desktop.is_dir():
            return onedrive_desktop
        desktop = Path(userprofile) / "Desktop"
        if desktop.is_dir():
            return desktop
    return Path.home() / "Desktop"


def create_desktop_shortcut(
    project_root: Path | None = None,
    shortcut_name: str = "Verbalia.lnk",
) -> Path:
    """Crea il collegamento .lnk con icona sul Desktop per avviare Verbalia senza console."""
    if project_root is None:
        project_root = Path(__file__).resolve().parent.parent

    desktop = get_desktop_dir()
    icon_path = project_root / "assets" / "icon.ico"
    main_script = project_root / "main.py"
    shortcut_path = desktop / shortcut_name

    pythonw_path = shutil.which("pythonw")
    if not pythonw_path:
        candidate = Path(sys.executable).parent / "pythonw.exe"
        pythonw_path = str(candidate) if candidate.exists() else sys.executable

    try:
        import win32com.client

        shell = win32com.client.Dispatch("WScript.Shell")
        shortcut = shell.CreateShortCut(str(shortcut_path))
        shortcut.TargetPath = pythonw_path
        shortcut.Arguments = f'"{main_script}"'
        shortcut.WorkingDirectory = str(project_root)
        if icon_path.exists():
            shortcut.IconLocation = f"{icon_path},0"
        shortcut.Description = "Verbalia - Registrazione e verbalizzazione automatica riunioni"
        shortcut.Save()
        return shortcut_path
    except Exception:
        import subprocess

        ps_script = (
            f'$WshShell = New-Object -ComObject WScript.Shell; '
            f'$Shortcut = $WshShell.CreateShortcut("{shortcut_path}"); '
            f'$Shortcut.TargetPath = "{pythonw_path}"; '
            f'$Shortcut.Arguments = \'"{main_script}"\'; '
            f'$Shortcut.WorkingDirectory = "{project_root}"; '
            f'$Shortcut.IconLocation = "{icon_path},0"; '
            f'$Shortcut.Description = "Verbalia - Registrazione e verbalizzazione automatica riunioni"; '
            f'$Shortcut.Save()'
        )
        subprocess.run(["powershell", "-NoProfile", "-Command", ps_script], check=True)
        return shortcut_path


if __name__ == "__main__":
    path = create_desktop_shortcut()
    print(f"Collegamento creato con successo in: {path}")
