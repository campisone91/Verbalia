from __future__ import annotations

from bootstrap import ensure_environment

ensure_environment()

import argparse  # noqa: E402
import os  # noqa: E402
import sys  # noqa: E402
from collections.abc import Sequence  # noqa: E402
from pathlib import Path  # noqa: E402

from config import (  # noqa: E402
    APP_NAME,
    APP_VERSION,
    EXIT_APP_ERROR,
    EXIT_BOOTSTRAP_FAILED,
    EXIT_CLEANUP_FAILED,
    EXIT_INVALID_ARGS,
    EXIT_MISSING_DEPENDENCY,
    EXIT_SUCCESS,
    EXIT_UNRECOVERABLE_SESSION,
)
from modules.contracts import (  # noqa: E402
    BootstrapError,
    ResumeError,
    ScribeError,
    StateValidationError,
)
from modules.orchestrator import MeetingScribeApplication  # noqa: E402
from modules.single_instance import (  # noqa: E402
    APP_MUTEX_NAME,
    SingleInstance,
    show_message_box,
)

if sys.platform == "win32":
    try:
        import ctypes

        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(f"{APP_NAME}.MeetingScribe.{APP_VERSION}")
    except Exception:
        pass


class _ScribeArgumentParser(argparse.ArgumentParser):
    """ArgumentParser che converte gli errori di sintassi CLI in exit code 2 deterministico."""

    def error(self, message: str) -> None:  # type: ignore[override]
        self.print_usage(sys.stderr)
        sys.stderr.write(f"{self.prog}: error: {message}\n")
        raise SystemExit(EXIT_INVALID_ARGS)


def build_arg_parser() -> argparse.ArgumentParser:
    """Costruisce il parser CLI con gruppo mutuamente esclusivo (Sezione 29)."""
    parser = _ScribeArgumentParser(
        prog="main.py",
        description=(
            f"{APP_NAME} v{APP_VERSION} - Registrazione dual-track, trascrizione faster-whisper "
            "e verbalizzazione Ollama locale per call Microsoft Teams."
        ),
    )
    mode_group = parser.add_mutually_exclusive_group(required=False)
    mode_group.add_argument(
        "--auto",
        action="store_true",
        help="Avvia in background con System Tray e rilevamento automatico call Teams (default).",
    )
    mode_group.add_argument(
        "--setup-only",
        action="store_true",
        help="Esegue esclusivamente il bootstrap e la validazione dell'ambiente locale.",
    )
    mode_group.add_argument(
        "--record-now",
        action="store_true",
        help="Avvia immediatamente una sessione di registrazione manuale.",
    )
    mode_group.add_argument(
        "--resume",
        type=str,
        metavar="SESSION_DIR",
        help="Riprende una sessione interrotta dalla directory temporanea specificata.",
    )
    mode_group.add_argument(
        "--test-pipeline",
        type=str,
        metavar="AUDIO_FILE",
        help="Esegue l'intera pipeline su una copia del file audio specificato.",
    )
    mode_group.add_argument(
        "--dry-run",
        action="store_true",
        help="Esegue un test sintetico completo senza hardware audio né modelli AI.",
    )
    mode_group.add_argument(
        "--diagnostics",
        action="store_true",
        help="Mostra il report diagnostico completo dell'ambiente locale.",
    )
    mode_group.add_argument(
        "--dashboard",
        action="store_true",
        help="Apre il cruscotto (stato, storico e impostazioni) in una finestra separata.",
    )
    parser.add_argument(
        "--tab",
        choices=("cruscotto", "storico", "impostazioni"),
        default=None,
        help="Scheda iniziale del cruscotto (solo con --dashboard).",
    )
    return parser


def _acquire_app_instance() -> SingleInstance | None:
    """Una sola istanza registrante per volta: due watcher registrerebbero la stessa call."""
    instance = SingleInstance(APP_MUTEX_NAME)
    if instance.acquire():
        return instance
    message = (
        f"{APP_NAME} è già in esecuzione (icona nella System Tray). "
        "Usare il menu della tray o il cruscotto per controllarlo."
    )
    sys.stderr.write(message + "\n")
    if os.environ.get("TEAMS_SCRIBE_HEADLESS_TRAY") != "1":
        show_message_box(APP_NAME, message)
    return None


def main(argv: Sequence[str] | None = None) -> int:
    """Entrypoint principale CLI con gestione rigorosa degli exit code (0..6)."""
    parser = build_arg_parser()
    try:
        args = parser.parse_args(list(argv) if argv is not None else None)
    except SystemExit as exc:
        code = exc.code if isinstance(exc.code, int) else EXIT_INVALID_ARGS
        return code

    if args.setup_only:
        try:
            ensure_environment(setup_only=True)
            return EXIT_SUCCESS
        except SystemExit as exc:
            return int(exc.code) if isinstance(exc.code, int) else EXIT_BOOTSTRAP_FAILED
        except (BootstrapError, RuntimeError, FileNotFoundError) as exc:
            sys.stderr.write(f"Errore bootstrap (--setup-only): {exc}\n")
            return EXIT_BOOTSTRAP_FAILED

    if args.resume is not None:
        raw_resume = str(args.resume).strip()
        if not raw_resume:
            sys.stderr.write("Errore: specificare un percorso valido per --resume.\n")
            return EXIT_INVALID_ARGS
        resume_dir = Path(raw_resume).resolve()
        if not resume_dir.exists() or not resume_dir.is_dir():
            sys.stderr.write(
                f"Errore: la directory di sessione non esiste o non è valida: {resume_dir}\n"
            )
            return EXIT_INVALID_ARGS
        app = MeetingScribeApplication()
        return app.run_resume(resume_dir)

    if args.test_pipeline is not None:
        raw_audio = str(args.test_pipeline).strip()
        if not raw_audio:
            sys.stderr.write("Errore: specificare un file audio valido per --test-pipeline.\n")
            return EXIT_INVALID_ARGS
        audio_path = Path(raw_audio).resolve()
        if not audio_path.exists():
            sys.stderr.write(f"Errore: il file audio specificato non esiste: {audio_path}\n")
            return EXIT_INVALID_ARGS
        if audio_path.is_dir():
            sys.stderr.write(
                f"Errore: il percorso indicato per --test-pipeline è una directory: {audio_path}\n"
            )
            return EXIT_INVALID_ARGS
        app = MeetingScribeApplication()
        return app.run_test_pipeline(audio_path)

    if args.dry_run:
        app = MeetingScribeApplication(headless=True)
        return app.run_dry_run()

    if args.diagnostics:
        app = MeetingScribeApplication(headless=True)
        return app.run_diagnostics()

    if args.dashboard:
        from modules.dashboard_ui import run_dashboard

        return run_dashboard(initial_tab=args.tab)

    instance = _acquire_app_instance()
    if instance is None:
        return EXIT_APP_ERROR

    if args.record_now:
        try:
            app = MeetingScribeApplication()
            return app.run_record_now()
        finally:
            instance.release()

    try:
        app = MeetingScribeApplication()
        return app.run_auto()
    except BootstrapError:
        return EXIT_BOOTSTRAP_FAILED
    except (ResumeError, StateValidationError):
        return EXIT_UNRECOVERABLE_SESSION
    except ScribeError as exc:
        exit_code = int(getattr(exc, "exit_code", EXIT_APP_ERROR))
        if exit_code in (
            EXIT_SUCCESS,
            EXIT_APP_ERROR,
            EXIT_INVALID_ARGS,
            EXIT_BOOTSTRAP_FAILED,
            EXIT_MISSING_DEPENDENCY,
            EXIT_UNRECOVERABLE_SESSION,
            EXIT_CLEANUP_FAILED,
        ):
            return exit_code
        return EXIT_APP_ERROR
    except Exception as exc:
        sys.stderr.write(f"Errore applicativo imprevisto: {exc}\n")
        return EXIT_APP_ERROR
    finally:
        instance.release()


if __name__ == "__main__":
    raise SystemExit(main())
