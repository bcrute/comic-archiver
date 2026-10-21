"""Versioned JSON bridge between the Kotlin app and the embedded Python runtime.

Contract: docs/android-beta-contracts.md §2. One entry point, dispatch(request) -> response;
requests are {"v":1,"op":name,"args":{...}}; responses are {"v":1,"ok":true,"result":...} or
{"v":1,"ok":false,"code":...,"message":...} with public-safe messages. The bridge validates
envelope and argument shapes and refuses unknown ops. Long work runs on the Python worker
thread (comic_archive.worker_host); dispatch itself returns promptly. Ops other than ping are
implemented by the native line before the integration build; each still answers with its
frozen envelope shape rather than raising.
"""
import json
import platform

VERSION = 1

OPS = frozenset({"ping", "prepare_workspace", "worker_status", "worker_start", "go", "pause",
                 "enqueue_sync", "library_listing", "export_manifest", "export_status"})


class _BridgeError(Exception):
    """An envelope-level failure; the message is always public-safe."""

    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
        self.message = message


def _ping(args):
    return {"python": platform.python_version(), "bridge_version": VERSION}


def _native_line(op):
    def handler(args):
        raise _BridgeError("not_implemented",
                           "This operation is implemented by the native line before the integration build.")
    return handler


HANDLERS = {"ping": _ping}
HANDLERS.update({op: _native_line(op) for op in OPS if op != "ping"})


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
