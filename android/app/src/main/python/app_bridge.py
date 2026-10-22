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

The library and export listings walk the workspace by the core library's own offering
rules (see local_library._exports): a series is offered with a whole sidecar beside every
archive or not at all, nothing that is a link or has vanished is named, and export digests
are computed over the original files as they stand.
"""
import hashlib
import json
import os
import platform
from pathlib import Path

VERSION = 1

OPS = frozenset({"ping", "prepare_workspace", "worker_status", "worker_start", "worker_stop",
                 "go", "pause", "download_status", "enqueue_sync", "library_listing", "export_manifest",
                 "export_status", "signin_probe", "signin_restore", "signout_record",
                 "settings", "configure_settings"})
# worker_stop, signin_probe and signout_record are native-line extensions of the frozen §2
# op list, flagged for review: the drain, the settle-time probe and the sign-out record leg
# have no §2 name yet, and the beta cannot run without them.

# The one workspace the beta app runs, and the WebView cookie seams the Kotlin side
# registers once at startup through set_cookie_seams (Chaquopy hands the same store
# object to the sign-in engine, so there is one cookie authority).
_workspace = {"root": None}
_seams = {"store": None, "cookie_source": None, "cookie_sink": None}
# The export summary's stat identity, so export_status polling does not re-digest a
# library whose files have not changed. Keyed to one root: the beta has exactly one.
_export_cache = {"root": None, "facts": None, "summary": None}


class _BridgeError(Exception):
    """An envelope-level failure; the message is always public-safe."""

    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
        self.message = message


def set_cookie_seams(cookie_source, cookie_sink=None):
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
    if not callable(cookie_source):
        header = next((getattr(cookie_source, name) for name in ("headerFor", "header_for")
                       if callable(getattr(cookie_source, name, None))), None)
        if header is None or not callable(getattr(cookie_source, "apply", None)):
            raise _BridgeError("bad_request",
                               "The cookie seams must be the app's sign-in store or its callables.")
        _seams.update(store=cookie_source, cookie_source=None, cookie_sink=None)
        return
    if cookie_sink is not None and not callable(cookie_sink):
        raise _BridgeError("bad_request",
                           "The cookie seams must be the app's sign-in store or its callables.")
    _seams.update(store=None, cookie_source=cookie_source, cookie_sink=cookie_sink)


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

    def start(ArchiveError):
        from comic_archive import worker_host
        source, sink = _cookie_seams()
        return worker_host.start_in_process(session, library, source, sink, status_path)

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
            workspace = Workspace(id="default", root=root)
            if name == "start":
                from comic_archive import android_download
                from comic_archive.locking import LibraryLock
                with LibraryLock(workspace.session / ".account-ui"):
                    android_download.start(workspace)
                return {}
            if name == "status":
                from comic_archive import android_download
                return android_download.status(workspace)
            getattr(account_ui, name)(workspace)
            return {}

        return _core(call)
    return handler


def _signin_probe(args):
    """The settle-time truth op (native line; §4 wording): the core's own who-am-i through
    the live seams decides, and only a verified answer writes the sign-in record that
    worker_start gates on. Cookie presence alone can never produce signed_in here."""
    session = _path_arg(args, "session_root")

    def probe(ArchiveError):
        source, sink = _cookie_seams()
        from comic_archive import android_adapter, android_session
        answer = android_adapter.signed_in_probe(cookie_source=source, cookie_sink=sink)
        if answer["signed_in"]:
            # Written only after the verified answer; the compatibility state is empty on
            # purpose — no cookie ever leaves the store.
            android_session.mark_signed_in(session, compatibility_state=True)
        return answer

    return _core(probe)


def _signout_record(args):
    """The last leg of the sign-out order (drain, then the store's own clear, then this):
    remove the session record so a restart can never resume a cleared sign-in."""
    session = _path_arg(args, "session_root")

    def forget(ArchiveError):
        from comic_archive import android_session
        android_session.forget(session)
        return {}

    return _core(forget)


def _signin_restore(args):
    """A saved record permits a fresh verification, never an authentication claim.

    A first install or signed-out session does not issue background source requests.
    No worker is started and no saved plan is admitted by this operation.
    """
    session = _path_arg(args, "session_root")

    def saved(ArchiveError):
        from comic_archive import android_session, sites
        site = android_session.signed_in_site(session)
        if site is not None and site.key != sites.DEFAULT.key:
            raise ArchiveError("This app cannot restore that site's sign-in yet.")
        return site is not None

    if not _core(saved):
        return {"signed_in": False, "verified": False, "reason": "no_saved_session"}
    # Restore is automatic, unlike opening the site's sign-in page explicitly. Honor
    # the same stored connection choice before making its account request.
    if not _core(lambda error: _settings_value(session.parent))["allowed"]:
        return {"signed_in": False, "verified": False, "reason": "network_held"}
    return _signin_probe(args)


def _settings_value(root):
    from comic_archive import account_ui, dashboard_model, network, network_android
    from comic_archive.workspace import Workspace
    network_android.configure(root / "net.json")
    workspace = Workspace(id="default", root=root)
    speeds = [{"key": key, "label": dashboard_model.SPEED_NAMES[key],
               "available": pacing["speed"] == "normal"}
              for key, pacing in account_ui.SPEEDS.items()]
    supported = {item["key"] for item in speeds if item["available"]}
    saved = account_ui.speed(workspace)
    return network.status(workspace.library) | {
        "speed": saved if saved in supported else "medium", "speeds": speeds}


def _settings(args):
    root = _path_arg(args, "root")
    return _core(lambda error: _settings_value(root))


def _configure_settings(args):
    root = _path_arg(args, "root")
    if set(args) - {"root", "wifi_only", "speed"} or not ({"wifi_only", "speed"} & set(args)):
        raise _BridgeError("bad_request", "Choose a download setting to change.")
    if "wifi_only" in args and type(args["wifi_only"]) is not bool:
        raise _BridgeError("bad_request", "The mobile data choice must be explicit.")

    def configure(ArchiveError):
        from comic_archive import account_ui, network
        from comic_archive.workspace import Workspace
        workspace = Workspace(id="default", root=root)
        from comic_archive.locking import LibraryLock
        with LibraryLock(workspace.session / ".account-ui"):
            # Validate the entire request before writing either setting.
            if "speed" in args:
                value = args["speed"]
                if (not isinstance(value, str) or value not in account_ui.SPEEDS
                        or account_ui.SPEEDS[value]["speed"] != "normal"):
                    raise ArchiveError("That speed is not available in this Android build.")
                if account_ui.status(workspace).get("status") == "running":
                    raise ArchiveError("Pause saving to change the speed.")
            if "wifi_only" in args:
                network.configure(workspace.library, args["wifi_only"])
            if "speed" in args:
                account_ui.choose_speed(workspace, args["speed"])
        return _settings_value(root)

    return _core(configure)


def _enqueue_sync(args):
    """Queue the fixed library-sync job on the running worker; fixed words only (§2)."""
    session = _path_arg(args, "session_root")

    def enqueue(ArchiveError):
        from comic_archive import android_session, sites, worker_host
        from comic_archive.queue_client import submit_job
        if not android_session.is_signed_in(session):
            raise ArchiveError("Sign in before syncing the library.")
        site = android_session.signed_in_site(session)
        if site is None or site.key != sites.DEFAULT.key:
            raise ArchiveError("The Android beta syncs from Tapas only.")
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
            "settings": _settings,
            "configure_settings": _configure_settings,
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
