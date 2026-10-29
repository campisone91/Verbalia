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
from modules.single_instance import (  # noqa: E402
    APP_MUTEX_NAME,
    SingleInstance,
    show_message_box,
)


def _create_app(**kwargs: object):
    from modules.orchestrator import MeetingScribeApplication

    return MeetingScribeApplication(**kwargs)  # type: ignore[arg-type]

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
        "--import-file",
        type=Path,
        default=None,
        metavar="FILE",
        help="Importa ed elabora un file audio/video esterno (.mp3, .wav, .m4a, .mp4, .mkv, ecc.).",
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
        "--title",
        type=str,
        default=None,
        metavar="TITLE",
        help="Titolo opzionale della riunione per --import-file.",
    )
    parser.add_argument(
        "--tab",
        choices=("cruscotto", "storico", "impostazioni"),
        default=None,
        help="Scheda iniziale del cruscotto (solo con --dashboard).",
    )
    parser.add_argument(
        "--incognito",
        action="store_true",
        help="Avvia Verbalia in modalità in incognito (nessun popup o notifica toast in nessuna fase).",
    )
    return parser


def _acquire_app_instance(incognito_active: bool = False) -> SingleInstance | None:
    """Una sola istanza registrante per volta: se già attiva, apre direttamente il Cruscotto."""
    instance = SingleInstance(APP_MUTEX_NAME)
    if instance.acquire():
        return instance
    message = (
        f"{APP_NAME} è già in esecuzione (icona nella System Tray). "
        "Apertura del cruscotto..."
    )
    sys.stderr.write(message + "\n")
    if os.environ.get("TEAMS_SCRIBE_HEADLESS_TRAY") != "1":
        try:
            from modules.dashboard_ui import run_dashboard

            run_dashboard()
        except Exception:
            if not incognito_active:
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

    from modules.settings import apply_settings

    settings = apply_settings()
    incognito_active = (
        bool(getattr(args, "incognito", False))
        or os.environ.get("VERBALIA_INCOGNITO", "").strip() in ("1", "true", "True")
        or bool(settings.detection.incognito_mode)
    )
    if incognito_active:
        os.environ["VERBALIA_INCOGNITO"] = "1"
        import config as _cfg

        _cfg.INCOGNITO_MODE = True

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
        app = _create_app()
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
        app = _create_app()
        return app.run_test_pipeline(audio_path)

    if args.import_file is not None:
        raw_import = str(args.import_file).strip()
        if not raw_import:
            sys.stderr.write("Errore: specificare un file valido per --import-file.\n")
            return EXIT_INVALID_ARGS
        import_path = Path(raw_import).resolve()
        if not import_path.exists():
            sys.stderr.write(f"Errore: il file specificato non esiste: {import_path}\n")
            return EXIT_INVALID_ARGS
        if import_path.is_dir():
            sys.stderr.write(
                f"Errore: il percorso indicato per --import-file è una directory: {import_path}\n"
            )
            return EXIT_INVALID_ARGS
        try:
            app = _create_app(headless=True)
            docx_path = app.run_import_pipeline(import_path, title=args.title)
            print(f"File importato ed elaborato con successo: {docx_path}")
            return EXIT_SUCCESS
        except Exception as exc:
            sys.stderr.write(f"Errore durante l'importazione di {import_path}: {exc}\n")
            return EXIT_APP_ERROR

    if args.dry_run:
        app = _create_app(headless=True)
        return app.run_dry_run()

    if args.diagnostics:
        app = _create_app(headless=True)
        return app.run_diagnostics()

    if args.dashboard:
        from modules.dashboard_ui import run_dashboard

        return run_dashboard(initial_tab=args.tab)

    instance = _acquire_app_instance(incognito_active=incognito_active)
    if instance is None:
        return 0

    if args.record_now:
        try:
            app = _create_app()
            return app.run_record_now()
        finally:
            instance.release()

    try:
        app = _create_app()
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
