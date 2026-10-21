"""Versioned JSON bridge between the Kotlin app and the embedded Python runtime.

Contract: docs/android-beta-contracts.md §2. One entry point, dispatch(request) -> response;
requests are {"v":1,"op":name,"args":{...}}; responses are {"v":1,"ok":true,"result":...} or
{"v":1,"ok":false,"code":...,"message":...} with public-safe messages. The bridge validates
envelope and argument shapes and refuses unknown ops. Long work runs on the Python worker
thread (comic_archive.worker_host); dispatch itself returns promptly.

The bridge module stays standalone: comic_archive is imported inside handlers only, so ping
and the selfcheck answer even before the core is importable. Core refusals (ArchiveError)
carry their public-safe words across the envelope as code "refused"; raw exceptions never
reach the Kotlin side.
"""
import json
import os
import platform
from pathlib import Path

VERSION = 1

OPS = frozenset({"ping", "prepare_workspace", "worker_status", "worker_start", "worker_stop",
                 "go", "pause", "enqueue_sync", "library_listing", "export_manifest", "export_status"})

# The one workspace the beta app runs, and the WebView cookie seams the Kotlin side
# registers once at startup through set_cookie_seams (Chaquopy hands the same store
# object to the sign-in engine, so there is one cookie authority).
_workspace = {"root": None}
_seams = {"cookie_source": None, "cookie_sink": None}


class _BridgeError(Exception):
    """An envelope-level failure; the message is always public-safe."""

    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
        self.message = message


def _store_header_source(store):
    """Adapt the Kotlin cookie store to the callable the core Transport asks for.

    Chaquopy exposes the store's cookieHeader/setSessionCookies as snake_cased methods; the
    store itself already scopes answers to the site's own hosts and bounds its wait. A store
    that cannot answer gets the same fixed, credential-free answer as an empty one, so a
    refused or slow store can never leak or wedge a request.
    """
    def cookie_header(url):
        try:
            return str(store.cookie_header(url))
        except Exception:
            return ""
    return cookie_header


def _store_cookie_sink(store):
    def apply(url, values):
        try:
            store.set_session_cookies(url, list(values))
        except Exception:
            pass  # A refused forward is dropped; the store stays the sole authority.
    return apply


def set_cookie_seams(cookie_source, cookie_sink=None):
    """Register the app's live cookie seams once at startup (frozen contract §4).

    Accepts either Python callables or the Kotlin cookie store object (whose
    cookieHeader/setSessionCookies methods Chaquopy exposes as cookie_header and
    set_session_cookies); the store itself stays with Kotlin, which keeps it the single
    cookie authority — this only holds the adapters the worker's Transport calls.
    """
    if not callable(cookie_source):
        if not hasattr(cookie_source, "cookie_header"):
            raise _BridgeError("bad_request", "The cookie seams must be callables from the cookie store.")
        cookie_source = _store_header_source(cookie_source)
    if cookie_sink is not None and not callable(cookie_sink):
        if not hasattr(cookie_sink, "set_session_cookies"):
            raise _BridgeError("bad_request", "The cookie seams must be callables from the cookie store.")
        cookie_sink = _store_cookie_sink(cookie_sink)
    _seams["cookie_source"] = cookie_source
    _seams["cookie_sink"] = cookie_sink


def _core(action):
    """Run one comic_archive call, letting its public-safe refusals cross the envelope."""
    try:
        from comic_archive.models import ArchiveError
    except ImportError:  # pragma: no cover - the app without the core installed
        raise _BridgeError("core_missing", "The archiver core is not available.") from None
    try:
        return action(ArchiveError)
    except ArchiveError as error:
        raise _BridgeError("refused", str(error)) from None


def _path_arg(args, name):
    value = args.get(name)
    if not isinstance(value, str) or not value.startswith("/") or "\x00" in value:
        raise _BridgeError("bad_request", f"{name} must be an absolute filesystem path.")
    return Path(value)


def _ping(args):
    return {"python": platform.python_version(), "bridge_version": VERSION}


def _prepare_workspace(args):
    """Make the one workspace's folders, hold any saved download for Go, and set the cwd.

    The core's queue paths are workspace-relative (Library, .comic-session), so the process
    cwd is the workspace root — set here, once, only while no downloader is running, per the
    reviewed roadmap's one-active-workspace rule.
    """
    root = _path_arg(args, "root")

    def build(ArchiveError):
        from comic_archive import account_ui, worker_host
        from comic_archive.archive_paths import directory
        from comic_archive.state_paths import private_folder
        from comic_archive.workspace import Workspace
        if root.is_symlink() or any(p.is_symlink() for p in root.parents):
            raise ArchiveError("Refusing a symlinked workspace root.")
        library = directory(root / "Library")
        session = private_folder(root, "session")
        if worker_host.status(session)["running"]:
            raise ArchiveError("Stop the downloader before switching workspaces.")
        os.chdir(root)
        _workspace["root"] = root
        held = account_ui.reopen(Workspace(id="default", root=root), idle=True)
        return {"root": str(root), "library": str(library), "session": str(session),
                "held": bool(held)}

    return _core(build)


def _worker_status(args):
    session = _path_arg(args, "session_root")

    def read(ArchiveError):
        from comic_archive import worker_host
        return worker_host.status(session)

    return _core(read)


def _worker_start(args):
    session = _path_arg(args, "session_root")
    library = _path_arg(args, "library_root")
    status_path = _path_arg(args, "network_status_path")
    if _seams["cookie_source"] is None:
        raise _BridgeError("cookie_seams_missing", "The WebView cookie store is not connected yet.")

    def start(ArchiveError):
        from comic_archive import worker_host
        return worker_host.start_in_process(session, library, _seams["cookie_source"],
                                            _seams["cookie_sink"], status_path)

    return _core(start)


def _worker_stop(args):
    """The service's checkpoint path (contract §3): pause, then drain through the existing
    stop protocol. Native-line extension of the §2 op list, flagged for review."""
    session = _path_arg(args, "session_root")
    wait = args.get("wait_seconds", 0)
    if type(wait) not in (int, float) or not 0 <= wait <= 120:
        raise _BridgeError("bad_request", "wait_seconds must be a small number of seconds.")

    def stop(ArchiveError):
        from comic_archive import worker_host
        return worker_host.request_stop(session, wait_seconds=wait)

    return _core(stop)


def _account_ui_op(name):
    def handler(args):
        root = _path_arg(args, "root")

        def call(ArchiveError):
            from comic_archive import account_ui
            from comic_archive.workspace import Workspace
            getattr(account_ui, name)(Workspace(id="default", root=root))
            return {}

        return _core(call)
    return handler


def _native_line(op):
    def handler(args):
        raise _BridgeError("not_implemented",
                           "This operation is implemented by the native line before the integration build.")
    return handler


HANDLERS = {"ping": _ping,
            "prepare_workspace": _prepare_workspace,
            "worker_status": _worker_status,
            "worker_start": _worker_start,
            "worker_stop": _worker_stop,
            "go": _account_ui_op("start"),
            "pause": _account_ui_op("pause")}
HANDLERS.update({op: _native_line(op) for op in OPS if op not in HANDLERS})


def dispatch(request):
    """Answer one bridge request with its response envelope as JSON text."""
    try:
        envelope = json.loads(request) if isinstance(request, (str, bytes, bytearray)) else None
    except ValueError:
        envelope = None
    if not isinstance(envelope, dict) or envelope.get("v") != VERSION:
        return _response(ok=False, code="bad_request", message="The request is not a v1 bridge call.")
    if envelope.get("op") not in OPS:
        return _response(ok=False, code="unknown_op", message="The requested operation does not exist.")
    if not isinstance(envelope.get("args"), dict):
        return _response(ok=False, code="bad_request", message="Bridge arguments must be a JSON object.")
    try:
        return _response(ok=True, result=HANDLERS[envelope["op"]](envelope["args"]))
    except _BridgeError as error:
        return _response(ok=False, code=error.code, message=error.message)


def _response(*, ok, code=None, message=None, result=None):
    value = {"v": VERSION, "ok": ok}
    if ok:
        value["result"] = result
    else:
        value["code"] = code
        value["message"] = message
    return json.dumps(value)
