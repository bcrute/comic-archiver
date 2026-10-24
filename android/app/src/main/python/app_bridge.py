"""Versioned JSON bridge between the Kotlin app and the embedded Python runtime.

Contract: docs/android-beta-contracts.md §2. One entry point, dispatch(request) -> response;
requests are {"v":1,"op":name,"args":{...}}; responses are {"v":1,"ok":true,"result":...} or
{"v":1,"ok":false,"code":...,"message":...} with public-safe messages. The bridge validates
envelope and argument shapes and refuses unknown ops. Downloads run on the Python worker
thread (comic_archive.worker_host). Synchronous local-reader calls verify/read bounded
content on the native reader's dedicated I/O thread; never call them on the UI thread.

The bridge module stays standalone: comic_archive is imported inside handlers only, so ping
and the selfcheck answer even before the core is importable. Core refusals (ArchiveError)
carry their public-safe words across the envelope as code "refused"; raw exceptions never
reach the Kotlin side.

The library and export listings walk the workspace by the core library's own offering
rules (see local_library._exports): a series is offered with a whole sidecar beside every
archive or not at all, nothing that is a link or has vanished is named, and export digests
are computed over the original files as they stand.
"""
import hashlib
import json
import os
import platform
import re
import threading
import time
from pathlib import Path
from urllib.parse import urlencode

VERSION = 1

OPS = frozenset({"ping", "prepare_workspace", "worker_status", "worker_start", "worker_stop",
                 "go", "pause", "download_status", "enqueue_sync", "library_listing", "export_manifest",
                 "export_status", "signin_probe", "signin_restore", "signout_record",
                 "settings", "configure_settings", "file_catalog", "file_open", "cloud_status", "cloud_action",
                 "signin_attempt", "signin_commit", "portable_catalog",
                 "reader_open", "reader_page", "reader_save", "reader_reload", "reader_close"})
# worker_stop, signin_probe and signout_record are native-line extensions of the frozen §2
# op list, flagged for review: the drain, the settle-time probe and the sign-out record leg
# have no §2 name yet, and the beta cannot run without them.

# The one active workspace, and the WebView cookie seams the Kotlin side selects
# after confirmed drain through set_cookie_seams (Chaquopy hands the same store
# object to the sign-in engine, so there is one cookie authority).
_workspace = {"root": None, "base": None, "site_key": "tapas"}
_workspace_lock = threading.RLock()
_signin_lock = threading.RLock()
_signin_attempts = {}
_seams = {"store": None, "cookie_source": None, "cookie_sink": None, "site_key": None}
_browser = {"factory": None}
# The export summary's stat identity, so export_status polling does not re-digest a
# library whose files have not changed. Keyed to one root: the beta has exactly one.
_export_cache = {"root": None, "facts": None, "summary": None}
_catalog_cache = {"root": None, "registry": None}
_catalog_lock = threading.Lock()
_cloud_cache = {"root": None, "controller": None}
_cloud_lock = threading.Lock()
_reader_host = None
_reader_lock = threading.RLock()
_reader_epoch = 0


def enable_android_cloud():
    """Native bootstrap only: select in-process B2 without enabling any connection."""
    from comic_archive.cloud_runtime import install_android
    install_android()


class _BridgeError(Exception):
    """An envelope-level failure; the message is always public-safe."""

    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
        self.message = message


def set_browser_seam(factory):
    """Native bootstrap only; this authority is never accepted through JSON dispatch."""
    methods = ('available', 'open', 'cookieStore', 'interrupt', 'closeAll')
    if factory is not None and not all(callable(getattr(factory, name, None)) for name in methods):
        raise _BridgeError('bad_request', 'The native reading browser is not connected.')
    with _workspace_lock:
        from comic_archive import worker_host
        from comic_archive.state_paths import session_path
        current = _workspace['root']
        if current is not None and worker_host.status(session_path(current))['running']:
            raise _BridgeError('refused', 'Stop the downloader before replacing its browser.')
        if _browser['factory'] is not factory:
            _close_browser()
        _browser['factory'] = factory


def _close_browser():
    if _browser['factory'] is not None:
        from comic_archive.android_browser import close_contexts
        close_contexts(_browser['factory'])


def _interrupt_browser():
    if _browser['factory'] is not None:
        from comic_archive.android_browser import interrupt_contexts
        interrupt_contexts(_browser['factory'])


def set_cookie_seams(cookie_source, cookie_sink=None, site_key=None):
    """Register the app's live cookie seams once at startup (frozen contract §4).

    The Kotlin cookie store object is registered as itself. Chaquopy performs no
    snake_case conversion (its rule is a trailing underscore for Python reserved words
    only), so the store is recognised by its frozen camelCase methods, headerFor and
    apply, and is refused here when either is missing: a half-wired store must fail
    loudly at registration, never answer later requests as an empty one. Plain callables
    (the tests' seams) pass through as the seams themselves. The core's own
    WebViewCookieSource wraps the store at use time; the store stays with Kotlin, which
    keeps it the single cookie authority.
    """
    if site_key is not None:
        from comic_archive import sites
        if not isinstance(site_key, str) or sites.find(site_key) is None:
            raise _BridgeError("bad_request", "Choose a supported sign-in store.")
    if not callable(cookie_source):
        header = next((getattr(cookie_source, name) for name in ("headerFor", "header_for")
                       if callable(getattr(cookie_source, name, None))), None)
        if header is None or not callable(getattr(cookie_source, "apply", None)):
            raise _BridgeError("bad_request",
                               "The cookie seams must be the app's sign-in store or its callables.")
        _replace_cookie_seams({"store": cookie_source, "cookie_source": None, "cookie_sink": None,
                              "site_key": site_key})
        return
    if cookie_sink is not None and not callable(cookie_sink):
        raise _BridgeError("bad_request",
                           "The cookie seams must be the app's sign-in store or its callables.")
    _replace_cookie_seams({"store": None, "cookie_source": cookie_source, "cookie_sink": cookie_sink,
                          "site_key": site_key})


def _replace_cookie_seams(value):
    with _workspace_lock:
        from comic_archive import worker_host
        from comic_archive.state_paths import session_path
        current = _workspace["root"]
        if current is not None and worker_host.status(session_path(current))["running"]:
            raise _BridgeError("refused", "Stop the downloader before changing site sign-in.")
        if current is not None and value["site_key"] not in {None, _workspace["site_key"]}:
            raise _BridgeError("refused", "The sign-in store belongs to another site.")
        _close_browser()
        _seams.update(value)


def deactivate_cookie_seams():
    """Native bootstrap only: offline library access has no substitute cookie authority."""
    _replace_cookie_seams({"store": None, "cookie_source": None, "cookie_sink": None, "site_key": None})


def _cookie_seams():
    """The registered seams as the core wants them: the store itself when the app
    registered the Kotlin object — android_adapter then takes its AndroidTransport route,
    the one request path with the asset reader the metadata pipeline needs — else the
    registered callables. A missing registration is an honest refusal, never an empty
    header standing in for the live store."""
    if _seams["store"] is not None:
        return _seams["store"], None
    if _seams["cookie_source"] is None:
        raise _BridgeError("cookie_seams_missing", "The WebView cookie store is not connected yet.")
    return _seams["cookie_source"], _seams["cookie_sink"]


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
    except ImportError:
        # A broken core module inside the action crosses as the app's public "core
        # missing" refusal, never as a raw traceback over the bridge.
        raise _BridgeError("core_missing", "The archiver core is not available.") from None


def _path_arg(args, name):
    value = args.get(name)
    if not isinstance(value, str) or not value.startswith("/") or "\x00" in value:
        raise _BridgeError("bad_request", f"{name} must be an absolute filesystem path.")
    path = Path(value)
    # The app's storage path is systematically symlinked in Android's per-user mount
    # view (the platform's own links, not an attacker's), so the core's parent-symlink
    # defenses would refuse every real workspace. Resolve at the boundary: the final
    # path itself must never be a link (that could redirect outside app storage —
    # observed live as a permanent workspace refusal on the phone), and the resolved
    # canonical path the core then sees has no symlinked components at all.
    if path.is_symlink():
        raise _BridgeError("bad_request", f"{name} must not be a symlink.")
    return path.resolve()


def _ping(args):
    return {"python": platform.python_version(), "bridge_version": VERSION}


def working_library_prepare(captured, payload, ticket, source_key, control, recover=False):
    """Native IO-only entry, deliberately absent from JSON dispatch and provider JS.

    This can take minutes and must not block the UI/fast command bridge. The
    native coordinator supplies its reserved staging paths and cancellation
    authority. No active workspace, session, browser or durable Go state changes.
    """
    import_error = None
    try:
        from dataclasses import asdict
        import re
        from comic_archive.working_library import (LibraryImportError, RECEIPT,
                                                   prepare_snapshot, prepared_snapshot)
        import_error = LibraryImportError
        captured = _path_arg({'captured': captured}, 'captured')
        payload = _path_arg({'payload': payload}, 'payload')
        if (not isinstance(ticket, str) or not re.fullmatch(
                r'[a-f0-9]{8}-[a-f0-9]{4}-4[a-f0-9]{3}-[89ab][a-f0-9]{3}-[a-f0-9]{12}', ticket)
                or not isinstance(source_key, str) or not re.fullmatch(r'[a-f0-9]{64}', source_key)
                or type(recover) is not bool or not callable(getattr(control, 'isCancelled', None))):
            raise _BridgeError('bad_request', 'The library copy request is unavailable.')
        stage = captured.parent.parent
        staged = payload == stage / 'payload'
        renamed = recover and payload == stage.parent.parent / ticket
        if (stage.name != ticket or stage.parent.name != '.staging' or stage.parent.parent.name != 'working-libraries'
                or captured != stage / 'capture/workspace' or not (staged or renamed)):
            raise _BridgeError('bad_request', 'Choose this pending library copy.')
        with _workspace_lock:
            active = _workspace['root']
            if active is not None and any(bound == active or bound in active.parents or active in bound.parents for bound in (stage, payload)):
                raise _BridgeError('refused', 'The active library cannot be replaced by a pending copy.')

        def check():
            if control.isCancelled():
                raise LibraryImportError('cancelled')

        check()
        if recover and not (payload / RECEIPT).exists():
            return _response(ok=True, result=None)
        result = (prepared_snapshot(payload, check=check, ticket=ticket, source_key=source_key) if recover
                  else prepare_snapshot(captured, payload, check=check))
        check()
        return _response(ok=True, result={'digest': result.digest, 'sites': [asdict(site) for site in result.sites]})
    except _BridgeError as error:
        return _response(ok=False, code=error.code, message=error.message)
    except Exception as error:
        # Keep cancellation typed, while raw Java/Python/filesystem errors stay private.
        if import_error is not None and isinstance(error, import_error):
            return _response(ok=False, code='library_import_' + error.code, message=str(error))
        return _response(ok=False, code='library_import_invalid',
                         message='The library copy could not be verified. The previous library is retained.')


def _prepare_workspace(args):
    """Make the one workspace's folders, hold any saved download for Go, and set the cwd.

    The core's queue paths are workspace-relative (Library, .comic-session), so the process
    cwd is the workspace root — set here, once, only while no downloader is running, per the
    reviewed roadmap's one-active-workspace rule.
    """
    root = _path_arg(args, "root")
    base = _path_arg(args, "base_root") if "base_root" in args else root
    key = args.get("site_key", "tapas")
    schema = args.get("network_schema")
    if schema is not None and (type(schema) is not int or schema != 1):
        raise _BridgeError("bad_request", "Choose a supported connection report.")

    def build(ArchiveError):
        global _reader_epoch
        from comic_archive import account_ui, android_session, network_android, shared, sites, worker_host
        from comic_archive.archive_paths import directory
        from comic_archive.state_paths import private_folder, session_path
        from comic_archive.workspace import Workspace
        site = sites.find(key) if isinstance(key, str) else None
        if site is None or sites.folder(base, site) != root:
            raise ArchiveError("Choose this site's own workspace.")
        with _workspace_lock:
            previous = _workspace["root"]
            for candidate in {root, previous} - {None}:
                if worker_host.status(session_path(candidate))["running"]:
                    raise ArchiveError("Stop the downloader before switching workspaces.")
            controller = _cloud_cache["controller"]
            if previous is not None and previous != root:
                old = Workspace(id="default", root=previous)
                if account_ui.status(old).get("status") == "running":
                    raise ArchiveError("Pause saving before switching sites.")
                if controller is not None and controller.busy:
                    raise ArchiveError("Finish the cloud activity before switching sites.")
            _close_browser()
            if previous != root:
                _reader_epoch += 1
                with _reader_lock:
                    if _reader_host is not None:
                        _reader_host.close_all()
            library = directory(root / "Library")
            session = private_folder(root, "session")
            android_session.prepare_site(session, site)
            shared.link(library, private_folder(base, "shared"))
            before_cwd = Path.cwd()
            try:
                os.chdir(root)
                held = account_ui.reopen(Workspace(id="default", root=root), idle=True)
                _cloud_controller(root)  # Recover held cloud tasks before Go admission.
            except Exception:
                os.chdir(before_cwd)
                raise
            with _signin_lock:
                if previous is not None and previous != root:
                    old_ticket = _signin_attempts.get(session_path(previous))
                    if old_ticket is not None:
                        old_ticket.update(active=False, verified=False)
                _workspace.update(root=root, base=base, site_key=site.key)
                network_android.configure(root / "net.json", require_fresh=schema == 1)
            return {"root": str(root), "library": str(library), "session": str(session),
                    "site_key": site.key, "held": bool(held)}

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

    def start(ArchiveError):
        from comic_archive import worker_host
        with _workspace_lock:
            _active_root(session.parent, ArchiveError)
            if _seams["site_key"] not in {None, _workspace["site_key"]}:
                raise ArchiveError("The sign-in store belongs to another site.")
            source, sink = _cookie_seams()
            options = {'browser_factory': _browser['factory']} if _browser['factory'] is not None else {}
            return worker_host.start_in_process(session, library, source, sink, status_path, **options)

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
        result = worker_host.request_stop(session, wait_seconds=wait)
        if not result['running']:
            _close_browser()
        return result

    return _core(stop)


def _account_ui_op(name):
    def handler(args):
        root = _path_arg(args, "root")

        def call(ArchiveError):
            from comic_archive import account_ui
            from comic_archive.workspace import Workspace
            workspace = Workspace(id="default", root=root)
            if name == "start":
                from comic_archive import android_download
                from comic_archive.locking import LibraryLock
                with _workspace_lock:
                    _active_root(root, ArchiveError)
                    with LibraryLock(workspace.session / ".account-ui"):
                        cloud = _cloud_cache["controller"] if _cloud_cache["root"] == str(root) else None
                        if cloud is not None and cloud.busy:
                            raise ArchiveError("Finish the cloud activity before pressing Go.")
                        options = {'browser_factory': _browser['factory']} if _browser['factory'] is not None else {}
                        android_download.start(workspace, **options)
                return {}
            if name == "status":
                from comic_archive import android_download
                return android_download.status(workspace)
            if name == 'pause':
                with _workspace_lock:
                    _active_root(root, ArchiveError)
                    account_ui.pause(workspace, on_stop_requested=_interrupt_browser)
                return {}
            getattr(account_ui, name)(workspace)
            return {}

        return _core(call)
    return handler


def _signin_probe(args, network_guard=None):
    """The settle-time truth op (native line; §4 wording): the core's own who-am-i through
    the live seams decides, and only a verified answer writes the sign-in record that
    worker_start gates on. Tagged native probes only prepare verification; the current
    main-thread engine must separately commit it. Cookie presence authenticates nothing."""
    session = _path_arg(args, "session_root")

    def probe(ArchiveError):
        source, sink = _cookie_seams()
        from comic_archive import android_adapter, android_readers, android_session, sites
        key = args.get("site_key", sites.DEFAULT.key)
        site = sites.find(key) if isinstance(key, str) else None
        if site is None:
            raise ArchiveError("Choose a supported site before signing in.")
        token = args.get("attempt")
        with _signin_lock:
            ticket = _signin_attempts.get(session)
            if token is not None:
                if not _current_attempt(session, site.key, token, ticket):
                    return {"signed_in": False, "verified": False, "reason": "attempt_ended"}
            elif ticket is not None:
                # A registered native session cannot be downgraded to an untagged write.
                return {"signed_in": False, "verified": False, "reason": "attempt_ended"}
        kwargs = {"network_guard": network_guard} if network_guard is not None else {}
        answer = (android_adapter.signed_in_probe(cookie_source=source, cookie_sink=sink, **kwargs)
                  if site == sites.DEFAULT else
                  android_readers.signed_in_probe(site.key, source, sink, **kwargs))
        with _signin_lock:
            if token is not None:
                if not _current_attempt(session, site.key, token, ticket):
                    return {"signed_in": False, "verified": False, "reason": "attempt_ended"}
                ticket["verified"] = bool(answer["signed_in"] and answer["verified"])
            elif _signin_attempts.get(session) is not None:
                return {"signed_in": False, "verified": False, "reason": "attempt_ended"}
            elif answer["signed_in"]:
                # Compatibility for unregistered v1 callers; the native client always
                # uses the tagged prepare/commit protocol below.
                android_session.mark_signed_in(session, site=site, compatibility_state=True)
        return answer

    return _core(probe)


def _active_root(root, ArchiveError):
    if _workspace["root"] is not None and _workspace["root"] != root:
        raise ArchiveError("Open this site's workspace before starting work.")


def _current_attempt(session, site_key, token, ticket):
    return (type(token) is int and 0 < token < 2**63 and ticket is not None
            and _signin_attempts.get(session) is ticket and ticket["token"] == token
            and ticket["site_key"] == site_key and ticket["active"]
            and time.monotonic() < ticket["deadline"]
            and (_workspace["root"] is None or
                 _workspace["root"] == session.parent and _workspace["site_key"] == site_key))


def _signin_attempt(args):
    """Begin or invalidate a bounded native attempt; performs no provider request."""
    session = _path_arg(args, "session_root")
    token, key, active = args.get("attempt"), args.get("site_key"), args.get("active")
    duration = args.get("timeout_ms", 900_000)
    if (type(token) is not int or not 0 < token < 2**63 or type(active) is not bool
            or type(duration) is not int or not 1 <= duration <= 900_000):
        raise _BridgeError("bad_request", "Choose a valid sign-in attempt.")

    def change(ArchiveError):
        from comic_archive import sites
        if not isinstance(key, str) or sites.find(key) is None:
            raise ArchiveError("Choose a supported site before signing in.")
        with _signin_lock:
            previous = _signin_attempts.get(session)
            if not active:
                if previous is not None and previous["token"] == token:
                    previous.update(active=False, verified=False)
                return {}
            if previous is not None and token <= previous["token"]:
                raise ArchiveError("Start a new sign-in attempt.")
            if _workspace["root"] != session.parent or _workspace["site_key"] != key:
                raise ArchiveError("Open this site's workspace before signing in.")
            if _seams["site_key"] not in {None, key}:
                raise ArchiveError("The sign-in store belongs to another site.")
            _signin_attempts[session] = {"token": token, "site_key": key, "active": True,
                                         "verified": False, "deadline": time.monotonic() + duration / 1000}
        return {}

    return _core(change)


def _signin_commit(args):
    """Commit only a still-current prepared verification; no cookies are copied."""
    session = _path_arg(args, "session_root")
    token, key = args.get("attempt"), args.get("site_key")

    def commit(ArchiveError):
        from comic_archive import android_session, sites
        with _signin_lock:
            ticket = _signin_attempts.get(session)
            if not _current_attempt(session, key, token, ticket) or not ticket["verified"]:
                raise ArchiveError("The sign-in attempt ended. Sign in again.")
            android_session.mark_signed_in(session, site=sites.find(key), compatibility_state=True)
            ticket["verified"] = False  # A prepared answer can be committed only once.
        return {"signed_in": True, "verified": True, "reason": None}

    return _core(commit)


def _signout_record(args):
    """The last leg of the sign-out order (drain, then the store's own clear, then this):
    remove the session record so a restart can never resume a cleared sign-in."""
    session = _path_arg(args, "session_root")

    def forget(ArchiveError):
        from comic_archive import android_session, sites
        with _signin_lock:
            if session in _signin_attempts:
                _signin_attempts[session].update(active=False, verified=False)
            else:
                _signin_attempts[session] = {"token": 0, "active": False, "verified": False,
                                             "site_key": "tapas", "deadline": 0}
            android_session.forget(session)
            if _workspace["root"] == session.parent:
                android_session.prepare_site(session, sites.find(_workspace["site_key"]))
        return {}

    return _core(forget)


def _signin_restore(args):
    """A saved record permits a fresh verification, never an authentication claim.

    A first install or signed-out session does not issue background source requests.
    No worker is started and no saved plan is admitted by this operation.
    """
    session = _path_arg(args, "session_root")

    def saved(ArchiveError):
        from comic_archive import android_session
        site = android_session.signed_in_site(session)
        return site.key if site is not None else None

    site_key = _core(saved)
    if site_key is None:
        return {"signed_in": False, "verified": False, "reason": "no_saved_session"}
    # Restore is automatic, unlike opening the site's sign-in page explicitly. Honor
    # the same stored connection choice before making its account request.
    if not _core(lambda error: _settings_value(session.parent))["allowed"]:
        return {"signed_in": False, "verified": False, "reason": "network_held"}
    from comic_archive.network import NetworkGuard
    return _signin_probe(args | {"site_key": site_key}, network_guard=NetworkGuard(session.parent / "Library"))


def _settings_value(root):
    from comic_archive.models import ArchiveError
    with _workspace_lock:
        _active_root(root, ArchiveError)
        return _settings_unlocked(root)


def _speed_choices(site_key):
    from comic_archive import account_ui, android_download, dashboard_model
    return [{"key": key, "label": dashboard_model.SPEED_NAMES[key],
             "available": android_download.speed_available(site_key, key, _browser['factory'])}
            for key in account_ui.SPEEDS]


def _other_site_settings():
    from comic_archive import android_download, sites
    from comic_archive.models import ArchiveError
    from comic_archive.workspace import Workspace
    if _workspace['root'] is None or _workspace['base'] is None:
        return []
    result = []
    for site in sites.SITES:
        if not site.available or site.key == _workspace['site_key']:
            continue
        row = {"key": site.key, "name": site.name, "speeds": _speed_choices(site.key),
               "enabled": True, "notice": None}
        try:
            workspace = Workspace(id="default", root=sites.folder(_workspace['base'], site))
            if any(path.exists() and not path.is_dir() for path in (workspace.root, workspace.session)):
                raise ArchiveError("This site's working folder is unavailable.")
            row['speed'] = android_download.selected_speed(workspace)
        except (ArchiveError, OSError):
            row.update(speed=None, enabled=False, notice="This site's working folder is unavailable. Choose an available working library.")
        result.append(row)
    return result


def _settings_unlocked(root):
    from comic_archive import android_download, network, network_android
    from comic_archive.workspace import Workspace
    network_android.configure(root / "net.json")
    workspace = Workspace(id="default", root=root)
    saved = android_download.selected_speed(workspace)
    return network.status(workspace.library) | {
        "speed": saved, "speeds": _speed_choices(_workspace['site_key']),
        "other_sites": _other_site_settings()}


def _settings(args):
    root = _path_arg(args, "root")
    return _core(lambda error: _settings_value(root))


def _configure_settings(args):
    root = _path_arg(args, "root")
    other_site = "site_key" in args
    if other_site and (set(args) != {"root", "site_key", "speed"} or not isinstance(args['site_key'], str)):
        raise _BridgeError("bad_request", "Choose another site's speed without changing the connection choice.")
    if set(args) - {"root", "wifi_only", "speed", "site_key"} or not ({"wifi_only", "speed"} & set(args)):
        raise _BridgeError("bad_request", "Choose a download setting to change.")
    if "wifi_only" in args and type(args["wifi_only"]) is not bool:
        raise _BridgeError("bad_request", "The mobile data choice must be explicit.")

    def configure(ArchiveError):
        from comic_archive import account_ui, android_download, network
        from comic_archive.workspace import Workspace
        workspace = Workspace(id="default", root=root)
        from comic_archive.locking import LibraryLock
        with _workspace_lock:
            _active_root(root, ArchiveError)
            if other_site:
                from comic_archive import android_session, sites, worker_host
                from comic_archive.state_paths import private_folder
                if _workspace['root'] != root or _workspace['base'] is None:
                    raise ArchiveError("Open the working library before changing another site's speed.")
                site = sites.find(args['site_key'])
                value = args['speed']
                if site is None or site.key == _workspace['site_key']:
                    raise ArchiveError("Choose another supported site's speed.")
                if not isinstance(value, str) or not android_download.speed_available(site.key, value, _browser['factory']):
                    raise ArchiveError("That speed is not available in this Android build.")
                target = Workspace(id="default", root=sites.folder(_workspace['base'], site))
                with LibraryLock(target.session / ".account-ui"):
                    if account_ui.status(target).get('status') == 'running' or worker_host.status(target.session)['running']:
                        raise ArchiveError("Pause saving on that site before changing its speed.")
                    android_session.prepare_site(private_folder(target.root, 'session'), site)
                    account_ui.choose_speed(target, value)
                return _settings_unlocked(root)
            with LibraryLock(workspace.session / ".account-ui"):
                # Validate the entire request before writing either setting.
                if "speed" in args:
                    value = args["speed"]
                    if (not isinstance(value, str) or value not in account_ui.SPEEDS
                            or not android_download.speed_available(_workspace['site_key'], value, _browser['factory'])):
                        raise ArchiveError("That speed is not available in this Android build.")
                    if account_ui.status(workspace).get("status") == "running":
                        raise ArchiveError("Pause saving to change the speed.")
                if "wifi_only" in args:
                    network.configure(workspace.library, args["wifi_only"])
                if "speed" in args:
                    account_ui.choose_speed(workspace, args["speed"])
            return _settings_unlocked(root)

    return _core(configure)


def _catalog_registry(root):
    from comic_archive.local_library import LocalWorkspaceRegistry
    with _catalog_lock:
        if _catalog_cache["root"] != str(root) or _catalog_cache["registry"] is None:
            registry = LocalWorkspaceRegistry(root)
            _catalog_cache.update(root=str(root), registry=registry)
        return _catalog_cache["registry"]


def _catalog_options(args):
    from comic_archive import saved_library
    options = args.get("options", {})
    if not isinstance(options, dict) or set(options) - {"query", "location", "kind", "sort", "offset", "limit", "refresh"}:
        raise _BridgeError("bad_request", "Choose supported saved-file filters.")
    values = {}
    for name, value in options.items():
        expected = bool if name == "refresh" else int if name in {"offset", "limit"} else str
        if type(value) is not expected:
            raise _BridgeError("bad_request", "Choose supported saved-file filters.")
        values["q" if name == "query" else name] = ("1" if value else "0") if expected is bool else value

    return saved_library.options(urlencode({"kind": "episodes"} | values))


def _catalog_projection(result, checked, *, portable=False):
    from comic_archive import library_model
    from datetime import datetime
    displayed = library_model.rows(result, False)
    for raw, row in zip(result["files"], displayed, strict=True):
        try:
            row["saved_epoch"] = datetime.fromisoformat(raw["saved_at"]).timestamp() if raw["saved_at"] else None
        except (ValueError, TypeError):
            row["saved_epoch"] = None
        row["row_id"] = raw.get("id")
        row["b2_copy"] = re.fullmatch(r"b2(?:-[a-f0-9]{16})?", raw.get("location", "")) is not None
    notice = library_model.notice(result).replace("Use Dashboard to start.", "Use Saving to start.")
    if portable and not result["file_count"]:
        notice = "No CBZ, EPUB or portable library files were found. Choose a Library folder or exported series folder."
    return {"rows": displayed, "total": result["total"], "offset": result["offset"], "limit": result["limit"],
            "summary": library_model.summary(result, False), "range": library_model.range_text(result),
            "notice": notice, "locations": library_model.places(result, False, checked["location"]),
            "kinds": [{"key": key, "label": label} for key, label in library_model.KINDS],
            "sorts": [{"key": key, "label": label} for key, label in library_model.SORTS],
            "page_sizes": list(library_model.PAGE_SIZES)}


def _file_catalog(args):
    root = _path_arg(args, "root")

    def listing(ArchiveError):
        from comic_archive.local_library import LOCAL_OWNER
        checked = _catalog_options(args)
        registry = _catalog_registry(root)
        result = registry.saved_files(LOCAL_OWNER, registry.workspace_id, **checked)
        return _catalog_projection(result, checked)

    return _core(listing)


def _portable_catalog(args):
    manifest_path = _path_arg(args, "manifest_path")

    def listing(ArchiveError):
        from comic_archive import portable_catalog, saved_library
        checked = _catalog_options(args)
        manifest = saved_library.metadata(manifest_path, portable_catalog.MAX_MANIFEST_BYTES)
        if manifest is None:
            raise ArchiveError("The folder catalog is unavailable. Refresh files or choose the folder again.")
        result = portable_catalog.page(manifest, **{key: value for key, value in checked.items() if key != "refresh"})
        return _catalog_projection(result, checked, portable=True)

    return _core(listing)


def _file_open(args):
    root = _path_arg(args, "root")
    identifier = args.get("id")

    def select(ArchiveError):
        from comic_archive.local_library import LOCAL_OWNER
        registry = _catalog_registry(root)
        path, kind = registry.resolve_export(LOCAL_OWNER, registry.workspace_id, identifier)
        if kind not in {"cbz", "epub"}:
            raise ArchiveError("Only a finished comic or novel can be opened in a reader.")
        return {"relpath": path.relative_to(root / "Library").as_posix(), "kind": kind}

    return _core(select)


def _reader(args, operation, expected_epoch=None):
    """Only native callers can reach dispatch; no reader authority enters a WebView."""
    allowed = {'root', 'reader_id'} | {
        'open': {'id'}, 'page': {'unit'}, 'save': {'position', 'bookmark', 'preferences'},
        'reload': set(), 'close': set()}[operation]
    if set(args) - allowed or not {'root', 'reader_id'} <= set(args):
        raise _BridgeError('bad_request', 'Choose supported reading controls.')
    root = _path_arg(args, 'root')

    def active():
        # No workspace lock while the reader owns its I/O lock. Preparation
        # increments the epoch before retiring a pending reader, including ABA.
        return expected_epoch is None or (_workspace['root'] == root and _reader_epoch == expected_epoch)

    def action(ArchiveError):
        global _reader_host
        from comic_archive.android_local_reader import ReaderHost
        with _reader_lock:
            if _reader_host is None:
                _reader_host = ReaderHost()
            host = _reader_host
        token = args['reader_id']
        if not active():
            raise ArchiveError('The working library changed. Choose Read here again.')
        if operation == 'open':
            value = host.open(root, _catalog_registry(root), args.get('id'), token, active=active)
        elif operation == 'page':
            value = host.page(root, token, args.get('unit'))
        elif operation == 'save':
            value = host.save(root, token, args.get('position'), bookmark=args.get('bookmark', False),
                              preferences=args.get('preferences'))
        elif operation == 'reload':
            value = host.reload(root, token)
        else:
            value = host.close(root, token)
        if not active():
            raise ArchiveError('The working library changed. Choose Read here again.')
        return value

    return _core(action)


def _active_reader(args, operation):
    root = _path_arg(args, 'root')

    def call(ArchiveError):
        with _workspace_lock:
            _active_root(root, ArchiveError)
            epoch = _reader_epoch
        return _reader(args, operation, epoch)

    return _core(call)


def _cloud_controller(root):
    from comic_archive.android_cloud import AndroidCloud
    with _cloud_lock:
        if _cloud_cache["root"] != str(root) or _cloud_cache["controller"] is None:
            previous = _cloud_cache["controller"]
            if previous is not None:
                previous.registry.cloud_signin.close()
            _cloud_cache.update(root=str(root), controller=AndroidCloud(_catalog_registry(root)))
        return _cloud_cache["controller"]


def _cloud_status(args):
    root = _path_arg(args, "root")
    if set(args) != {"root"}:
        raise _BridgeError("bad_request", "Choose supported cloud settings.")
    return _core(lambda error: _cloud_controller(root).snapshot())


def _cloud_action(args):
    root = _path_arg(args, "root")
    action = args.get("action")
    if not isinstance(action, str) or action not in {"connect", "cancel", "test", "retry", "cleanup", "sharing", "destination", "resume"}:
        raise _BridgeError("bad_request", "Choose a supported cloud action.")
    required = {"root", "action"} | ({"credentials"} if action == "connect" else
                                     {"value"} if action in {"cleanup", "sharing", "destination"} else set())
    if set(args) != required or ("value" in args and not isinstance(args["value"], str)):
        raise _BridgeError("bad_request", "Choose supported cloud settings.")
    credentials = args.get("credentials")
    if action == "connect":
        if (not isinstance(credentials, dict) or set(credentials) != {"bucket", "key_id", "application_key"}
                or any(not isinstance(value, str) or len(value) > 256 for value in credentials.values())):
            raise _BridgeError("bad_request", "Enter the bucket name, keyID and applicationKey.")

    def act(ArchiveError):
        def prepare_worker():
            result = _worker_start({"session_root": str(root / ".comic-session"),
                                   "library_root": str(root / "Library"), "network_status_path": str(root / "net.json")})
            if not result.get("running") or result.get("stop_requested"):
                raise ArchiveError("The downloader is not ready for this cloud task. Try again.")
        return _cloud_controller(root).act(action, credentials=credentials, value=args.get("value"),
                                           start_worker=prepare_worker)
    try:
        return _core(act)
    finally:
        if isinstance(credentials, dict):
            credentials.clear()


def _enqueue_sync(args):
    """Queue the fixed library-sync job on the running worker; fixed words only (§2)."""
    session = _path_arg(args, "session_root")

    def enqueue(ArchiveError):
        from comic_archive import android_session, sites, worker_host
        from comic_archive.queue_client import submit_job
        if not android_session.is_signed_in(session):
            raise ArchiveError("Sign in before syncing the library.")
        site = android_session.signed_in_site(session)
        if site is None or sites.find(site.key) != site:
            raise ArchiveError("Choose a supported site before syncing the library.")
        with _workspace_lock:
            _active_root(session.parent, ArchiveError)
            if not worker_host.status(session)["running"]:
                raise ArchiveError("Start the downloader before syncing the library.")
            task = submit_job(session, "sync", {})
            return {"task_id": task["task_id"], "status": task["status"]}

    return _core(enqueue)


def _series_folders(root):
    """The workspace's exported series by the core library's offering rules: a series
    folder with at least one archive whose whole sidecar exists, or not at all."""
    from comic_archive.archive import episode_archives, sidecar_path

    def safe(path):
        return path.is_file() and not any(p.is_symlink() for p in (path, *path.parents))

    library = root / "Library"
    if not library.is_dir() or library.is_symlink():
        return []
    series = []
    for folder in sorted(p for p in library.iterdir()
                         if p.is_dir() and not p.is_symlink() and not p.name.startswith(".")):
        episodes = []
        for archive in episode_archives(folder):
            sidecar = sidecar_path(archive)
            if not (safe(archive) and safe(sidecar)):
                continue
            try:
                metadata = json.loads(sidecar.read_text())
            except (OSError, ValueError):
                continue  # An episode is offered with its details or not at all.
            if not isinstance(metadata, dict):
                continue
            episodes.append((archive, metadata))
        if not episodes:
            continue
        meta = {}
        if safe(folder / "series.json"):
            try:
                value = json.loads((folder / "series.json").read_text())
                if isinstance(value, dict):
                    meta = value
            except (OSError, ValueError):
                meta = {}
        series.append((folder, meta, episodes))
    return series


def _series_identity(folder, meta, episodes):
    """The series' public identity: the recorded source id, else the episodes' one
    consistent id, else the folder name — never a guess about a different series."""
    for key in ("source_series_id", "library_series_id"):
        value = meta.get(key)
        if isinstance(value, str) and value:
            return value
    ids = {m.get("source_series_id") for _, m in episodes
           if isinstance(m.get("source_series_id"), str) and m.get("source_series_id")}
    return ids.pop() if len(ids) == 1 else folder.name


def _library_listing(args):
    root = _path_arg(args, "root")

    def listing(ArchiveError):
        result = []
        for folder, meta, episodes in _series_folders(root):
            count = meta.get("episode_count")
            kinds = {m.get("content_type") for _, m in episodes}
            result.append({
                "seriesId": _series_identity(folder, meta, episodes),
                "title": meta.get("title") if isinstance(meta.get("title"), str) else folder.name,
                # The site's own two shelves: novels when every offered episode is one.
                "kind": "novels" if kinds == {"novel"} else "comic",
                "episodeCount": count if isinstance(count, int) and count >= 0 else len(episodes),
                "savedCount": len(episodes),
            })
        return result

    return _core(listing)


def _digest(path):
    value = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            value.update(block)
    return value.hexdigest()


def _manifest(series, library):
    """The exportable manifest (contract §5: CBZ and EPUB only), digests computed over the
    original files as they stand — nothing is re-derived from metadata."""
    entries = []
    for folder, meta, episodes in series:
        identity = _series_identity(folder, meta, episodes)
        for archive, sidecar in episodes:
            if archive.name.endswith(".tar.zst"):
                continue
            episode = sidecar.get("source_episode_id")
            if not (isinstance(episode, str) and episode):
                episode = archive.name  # A local identity for an export the core itself made.
            entries.append({
                "seriesId": identity,
                "episodeId": episode,
                "kind": "EPUB" if archive.suffix == ".epub" else "CBZ",
                "filename": archive.name,
                "bytes": archive.stat().st_size,
                "sha256": _digest(archive),
                "relpath": archive.relative_to(library).as_posix(),
            })
    return entries


def _stat_facts(series, library):
    """The cheap identity of the offered files: relpath, size and mtime of each."""
    facts = []
    for _folder, _meta, episodes in series:
        for archive, _sidecar in episodes:
            if archive.name.endswith(".tar.zst"):
                continue
            observed = archive.stat()
            facts.append((archive.relative_to(library).as_posix(), observed.st_size,
                          observed.st_mtime_ns))
    return tuple(facts)


def _summary(entries):
    canonical = json.dumps(entries, sort_keys=True, separators=(",", ":")).encode()
    return {"entries": len(entries), "bytes": sum(entry["bytes"] for entry in entries),
            "manifest_sha256": hashlib.sha256(canonical).hexdigest()}


def _export_manifest(args):
    root = _path_arg(args, "root")

    def manifest(ArchiveError):
        library = root / "Library"
        series = _series_folders(root)
        entries = _manifest(series, library)
        _export_cache.update(root=str(root), facts=_stat_facts(series, library),
                             summary=_summary(entries))
        return {"entries": entries,
                "manifest_sha256": _export_cache["summary"]["manifest_sha256"]}

    return _core(manifest)


def _export_status(args):
    """The polling answer over the same manifest: the cached summary while the offered
    files are unchanged, real digests again the moment they are not."""
    root = _path_arg(args, "root")

    def status(ArchiveError):
        library = root / "Library"
        series = _series_folders(root)
        facts = _stat_facts(series, library)
        if (_export_cache["root"] != str(root) or _export_cache["facts"] != facts
                or _export_cache["summary"] is None):
            _export_cache.update(root=str(root), facts=facts,
                                 summary=_summary(_manifest(series, library)))
        return _export_cache["summary"]

    return _core(status)


def _in_active_workspace(handler):
    """Serialize context-sensitive policy actions before controller/account locks."""
    def call(args):
        root = _path_arg(args, "root")

        def active(ArchiveError):
            with _workspace_lock:
                _active_root(root, ArchiveError)
                return handler(args)

        return _core(active)
    return call


HANDLERS = {"ping": _ping,
            "prepare_workspace": _prepare_workspace,
            "worker_status": _worker_status,
            "worker_start": _worker_start,
            "worker_stop": _worker_stop,
            "go": _account_ui_op("start"),
            "pause": _account_ui_op("pause"),
            "download_status": _account_ui_op("status"),
            "signin_probe": _signin_probe,
            "signin_restore": _signin_restore,
            "signin_attempt": _signin_attempt,
            "signin_commit": _signin_commit,
            "settings": _in_active_workspace(_settings),
            "configure_settings": _in_active_workspace(_configure_settings),
            "file_catalog": _file_catalog,
            "portable_catalog": _portable_catalog,
            "file_open": _file_open,
            "reader_open": lambda args: _active_reader(args, 'open'),
            "reader_page": lambda args: _active_reader(args, 'page'),
            "reader_save": lambda args: _active_reader(args, 'save'),
            "reader_reload": lambda args: _active_reader(args, 'reload'),
            # An old ticket may close after context changed. It can retire only
            # its own retained session and cannot close a newer reader.
            "reader_close": lambda args: _reader(args, 'close'),
            "cloud_status": _in_active_workspace(_cloud_status),
            "cloud_action": _in_active_workspace(_cloud_action),
            "signout_record": _signout_record,
            "enqueue_sync": _enqueue_sync,
            "library_listing": _library_listing,
            "export_manifest": _export_manifest,
            "export_status": _export_status}


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
