import argparse
import os
import sys
import traceback
from pathlib import Path
from .models import ArchiveError


def parser():
    from .state_paths import SESSION_NAME
    p = argparse.ArgumentParser(prog="comic-archive", description="Comic Archiver: personal comic CBZ and novel EPUB preservation")
    # A relative literal: each command checks the folder when it uses it, so a bad folder never breaks --help.
    p.add_argument("--session-dir", type=Path, default=Path(SESSION_NAME))
    p.add_argument("--browser", choices=("auto", "chromium", "firefox", "qt"), default="auto",
                   help="Browser engine; auto falls back from Chromium to Firefox, qt is the desktop app's built-in engine")
    p.add_argument("--headless", action="store_true", help="Run authenticated imports without a browser window")
    p.add_argument("--request-interval", type=float, default=1.0, help="Seconds between image requests (0.25–60; default 1; metadata pacing is separate)")
    sub = p.add_subparsers(dest="command", required=True)
    sub.add_parser("doctor", help="Test browser startup without account data or network requests")
    sub.add_parser("browser-worker", help="Run the desktop app's background downloader")
    return p


def run(args):
    if args.command == "browser-worker":
        from .worker import run_worker
        run_worker(args.session_dir, args.browser, args.headless, args.request_interval)
        return 0
    from .session import doctor
    doctor(args.browser, args.headless)
    return 0


def run_beside_engine(args):
    """The app's built-in engine needs the main thread for Qt, so with it the command runs on a second one."""
    if args.browser != 'qt':
        return run(args)
    from .qt_engine import serve
    return serve(lambda: run(args))


def main():
    args = parser().parse_args()
    try:
        code = run_beside_engine(args)
    except ArchiveError as e:
        print(f"Error: {e}", file=sys.stderr)
        code = 1
    except KeyboardInterrupt:
        print("Interrupted; completed pages remain cached for resume.", file=sys.stderr)
        code = 130
    except EOFError:
        print("Interactive browser step needs a terminal.", file=sys.stderr)
        code = 1
    except BrokenPipeError:
        # The reader went away (for example `| head`); end quietly without a second error at exit.
        try:
            os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
        except (OSError, ValueError):
            pass
        code = 141
    except Exception:
        # Browser/http exceptions can include signed URLs or headers. Never echo them unless asked.
        if os.environ.get('COMIC_ARCHIVE_DEBUG') == '1':
            traceback.print_exc()
        print("Operation failed. Check browser installation, input files and output permissions. Raw exceptions are suppressed to protect session data; set COMIC_ARCHIVE_DEBUG=1 to show them.", file=sys.stderr)
        code = 1
    raise SystemExit(code)
