"""Browser session acquisition, separate from retrieval and normalization."""
import json
import math
import os
from pathlib import Path
from . import sites
from .models import ArchiveError
from .osdep import open_exclusive, replace_file, sync_directory


def browser_api(preferred="auto"):
    if preferred == "qt":
        # The desktop app's own engine; Playwright and its browsers are not needed for it.
        from .qt_engine import Driver
        return Driver
    try:
        from playwright.sync_api import sync_playwright
        return sync_playwright
    except ImportError:
        raise ArchiveError("Install the browser extra and run: playwright install chromium") from None


def browser_failure_reason(error):
    """Classify startup diagnostics without echoing exception text or credentials."""
    message = str(error).lower()
    if "built-in engine" in message:
        return "the app's built-in engine is unavailable here; it runs inside the downloader with the desktop extra installed"
    if "executable doesn't exist" in message:
        return "browser binary is missing; run .venv/bin/playwright install chromium firefox"
    if "missing x server" in message or "cannot open display" in message or "no display" in message:
        return "desktop display is unavailable; run from your desktop terminal"
    if "operation not permitted" in message or "permission denied" in message:
        return "operating system denied browser startup or socket access"
    if "host system is missing dependencies" in message or "error while loading shared libraries" in message:
        return "required system libraries are missing"
    if any(signal in message for signal in ("sigsegv", "sigtrap", "sigabrt")):
        return "browser process crashed during startup"
    return "browser startup failed; no account/session data was loaded"


def launch_browser(driver, preferred="auto", headless=False):
    if preferred not in {"auto", "chromium", "firefox", "qt"}:
        raise ArchiveError("Browser must be auto, chromium, firefox or qt.")
    engines = ("chromium", "firefox") if preferred == "auto" else (preferred,)
    failures = []
    for engine in engines:
        try:
            return getattr(driver, engine).launch(headless=headless, timeout=15000)
        except Exception as error:
            failures.append(f"{engine}: {browser_failure_reason(error)}")
            if engine != engines[-1]:
                print(f"{engine.capitalize()} could not start; trying Firefox.")
    raise ArchiveError("Cannot start a browser. " + "; ".join(failures)) from None


def doctor(preferred="auto", headless=False):
    """Test a fresh browser without loading cookies or making site requests."""
    with browser_api(preferred)() as driver:
        browser = launch_browser(driver, preferred, headless=headless)
        try:
            context = browser.new_context()
            context.new_page()
            print("Browser startup and a fresh context succeeded. No account data was loaded.")
        finally:
            browser.close()


def cookie_expiry_seconds(value):
    """Normalize Firefox seconds/milliseconds to Playwright Unix seconds."""
    if type(value) not in (int, float) or not math.isfinite(value):
        raise ArchiveError("Invalid cookie expiry timestamp; credential details are suppressed.")
    if value in (0, -1):
        return -1
    # Contemporary millisecond timestamps have 13 digits; normal second-based
    # cookie dates are far below this threshold. Reject unsupported ranges.
    if value >= 100_000_000_000:
        value /= 1000
    if value < 0 or value > 253_402_300_799:
        raise ArchiveError("Unsupported cookie expiry timestamp; credential details are suppressed.")
    return value


def load_session(directory):
    """Also repair the in-memory representation of older imported state files."""
    try:
        state = json.loads((directory / "state.json").read_text())
        for cookie in state["cookies"]:
            cookie["expires"] = cookie_expiry_seconds(cookie["expires"])
        return state
    except (OSError, ValueError, TypeError, KeyError):
        raise ArchiveError("Saved session is unreadable or malformed. Re-import Firefox cookies; credential details are suppressed.") from None


def save_session(context, directory: Path):
    store_session(context.storage_state(), directory)


def store_session(state, directory: Path, site=sites.DEFAULT):
    """Write a browser storage state privately, however it was collected, with the site it belongs to."""
    if any(p.is_symlink() for p in (directory, *directory.parents)):
        raise ArchiveError("Refusing a symlink in the session directory.")
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    directory.chmod(0o700)
    # Keep only the site's own cookies/local storage; exclude OAuth provider sessions.
    state["cookies"] = [c for c in state["cookies"] if site.keeps_cookie(c["domain"])]
    state["origins"] = [o for o in state["origins"] if o["origin"] == site.home]
    temporary = directory / "state.tmp"
    if any(path.is_symlink() for path in (temporary, directory / "state.json", directory / "site.tmp", directory / "site")):
        raise ArchiveError("Refusing a symlink in session storage.")
    # The site first: the downloader never finds a sign-in without knowing whose it is.
    _private_write(directory / "site.tmp", site.key)
    replace_file(directory / "site.tmp", directory / "site")
    sync_directory(directory)
    _private_write(temporary, json.dumps(state))
    replace_file(temporary, directory / "state.json")
    sync_directory(directory)


def _private_write(path, text):
    with os.fdopen(open_exclusive(path), "w") as f:
        f.write(text)
        # On disk before it takes the old file's place, so a crash leaves one or the other whole.
        f.flush()
        os.fsync(f.fileno())


