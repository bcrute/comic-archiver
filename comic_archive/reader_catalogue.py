"""Personal B2 publication DATA; never account, consent or file-read authority.

The caller owns a real configured transfer and a verified cloud receipt. No site
retrieval, credentials, source URLs or private provenance enter public records.
"""

import hashlib
import json
import re
import stat
import tempfile
from pathlib import Path

from .locking import LibraryLock
from .models import ArchiveError
from .osdep import WINDOWS
from .publication import valid_export_id, validate_public_metadata
from .storage import safe_root

# Off until the web reader that reads these records ships: until then a B2 upload writes none, as before.
PUBLISHING = False
PUBLIC = "comic-archiver.reader-b2/1"
PRIVATE = "comic-archiver.reader-publication/1"
MAXIMUM = 16384
REVISIONS = 128
ZERO = "0" * 64
HEX = re.compile(r"[0-9a-f]{64}")
NAME = re.compile(
    r"([0-9]{10})-([0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12})-"
    r"([0-9]{10})-([0-9a-f]{64})-([0-9a-f]{64})\.json"
)
FIELDS = {
    "format",
    "library_id",
    "series_id",
    "episode_id",
    "revision",
    "previous",
    "version",
    "series_title",
    "title",
    "order",
    "archive",
}


def require(value):
    if not value:
        raise ArchiveError(
            "Reader catalogue metadata is inconsistent; originals are retained."
        )


def encoded(value):
    raw = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    require(len(raw) <= MAXIMUM)
    return raw


def pairs(items):
    value = {}
    for key, item in items:
        require(key not in value)
        value[key] = item
    return value


def parsed(raw):
    require(isinstance(raw, bytes) and len(raw) <= MAXIMUM)
    return json.loads(
        raw.decode("utf-8"),
        object_pairs_hook=pairs,
        parse_constant=lambda _: require(False),
    )


def text(value):
    require(
        isinstance(value, str)
        and value.strip()
        and len(value.encode("utf-8")) <= 240
        and not re.search(r"[\x00-\x1f\x7f]", value)
    )
    return value


def label(value):
    # Only a bounded public title, never the original private metadata object.
    require(isinstance(value, str))
    return text(
        re.sub(r"[\x00-\x1f\x7f]", " ", value)
        .strip()
        .encode("utf-8")[:240]
        .decode("utf-8", errors="ignore")
    )


def checked(value, library, episode):
    require(
        isinstance(value, dict) and set(value) == FIELDS and value["format"] == PUBLIC
    )
    require(value["library_id"] == library and value["episode_id"] == episode)
    for field in ("library_id", "series_id", "episode_id"):
        require(valid_export_id(value[field]))
    require(type(value["revision"]) is int and 1 <= value["revision"] <= REVISIONS)
    require(isinstance(value["previous"], str) and HEX.fullmatch(value["previous"]))
    require((value["revision"] == 1) == (value["previous"] == ZERO))
    require(isinstance(value["version"], str) and HEX.fullmatch(value["version"]))
    text(value["series_title"])
    text(value["title"])
    require(type(value["order"]) is int and 0 <= value["order"] <= 1000000000)
    archive = value["archive"]
    require(
        archive is None
        or (
            isinstance(archive, dict)
            and set(archive) == {"bytes", "sha256"}
            and type(archive["bytes"]) is int
            and 1 <= archive["bytes"] <= 64 * 1024**2
            and isinstance(archive["sha256"], str)
            and HEX.fullmatch(archive["sha256"])
        )
    )
    encoded(value)
    return value


def name(value):
    return (
        str(value["revision"]).zfill(10)
        + "-"
        + value["series_id"]
        + "-"
        + str(value["order"]).zfill(10)
        + "-"
        + value["previous"]
        + "-"
        + hashlib.sha256(encoded(value)).hexdigest()
        + ".json"
    )


def prefix(library, episode):
    require(valid_export_id(library) and valid_export_id(episode))
    return f"Comic-Archive/{library}/.reader/{episode}"


def chain(names):
    require(len(names) <= REVISIONS and len(names) == len(set(names)))
    records = {}
    for item in names:
        match = NAME.fullmatch(item)
        require(match is not None)
        revision = int(match[1])
        require(
            1 <= revision <= REVISIONS
            and str(revision).zfill(10) == match[1]
            and revision not in records
            and 0 <= int(match[3]) <= 1000000000
        )
        records[revision] = (match[4], match[5], item)
    previous = ZERO
    for revision in range(1, len(records) + 1):
        require(revision in records and records[revision][0] == previous)
        previous = records[revision][1]
    return records


def remote_head(transfer, library, episode):
    location = prefix(library, episode)
    raw = transfer.run(
        "lsf", transfer.remote(location), "--files-only", missing_ok=True
    )
    require(len(raw) <= 32768)
    records = chain(raw.decode("utf-8").splitlines())
    if not records:
        return None, records
    filename = records[len(records)][2]
    body = transfer.run("cat", transfer.remote(location + "/" + filename))
    require(len(body) <= MAXIMUM)
    value = checked(parsed(body), library, episode)
    # Public producers are canonical. Hash actual bytes, not a guessed re-encoding.
    require(encoded(value) == body and name(value) == filename)
    return value, records


def state_path(root, library, target, episode):
    require(all(valid_export_id(value) for value in (library, target, episode)))
    return (
        safe_root(root)
        / ".archive-state/cloud/reader/b2"
        / target
        / (episode + ".json")
    )


def state_read(path, library, target, episode):
    if not path.exists():
        return {
            "format": PRIVATE,
            "library_id": library,
            "target_id": target,
            "episode_id": episode,
            "committed": None,
            "pending": None,
            "request": None,
            "seen_version": None,
        }
    safe_root(path)
    info = path.stat()
    require(
        stat.S_ISREG(info.st_mode)
        and info.st_nlink == 1
        and (WINDOWS or not info.st_mode & 0o077)
        and info.st_size <= 2 * MAXIMUM + 2048
    )
    value = json.loads(path.read_text("utf-8"), object_pairs_hook=pairs)
    require(
        isinstance(value, dict)
        and set(value)
        == {
            "format",
            "library_id",
            "target_id",
            "episode_id",
            "committed",
            "pending",
            "request",
            "seen_version",
        }
    )
    require(
        value["format"] == PRIVATE
        and value["library_id"] == library
        and value["target_id"] == target
        and value["episode_id"] == episode
    )
    for field in ("committed", "pending"):
        if value[field] is not None:
            checked(value[field], library, episode)
    request = value["request"]
    require(
        request is None
        or (
            isinstance(request, dict)
            and set(request) == {"version", "advance"}
            and isinstance(request["version"], str)
            and HEX.fullmatch(request["version"])
            and type(request["advance"]) is bool
        )
    )
    require(
        value["seen_version"] is None
        or (
            isinstance(value["seen_version"], str)
            and HEX.fullmatch(value["seen_version"])
        )
    )
    committed, pending = value["committed"], value["pending"]
    if pending is not None:
        require(
            pending["revision"] == (committed["revision"] + 1 if committed else 1)
            and pending["previous"]
            == (hashlib.sha256(encoded(committed)).hexdigest() if committed else ZERO)
        )
    return value


def needs(root, library, target, record):
    """Cheap metadata backlog observation, never provider/account permission."""
    try:
        state = state_read(
            state_path(root, library, target, record["episode_id"]),
            library,
            target,
            record["episode_id"],
        )
        return (
            state["pending"] is not None
            or state["request"] is not None
            or state["committed"] is None
            or state["seen_version"] != record["version"]
        )
    except (ArchiveError, OSError, ValueError, KeyError, TypeError):
        return True


def projection(root, transfer, record, library):
    from .archive import sidecar_path
    from .cloud import _json

    require(record.get("state") == "verified" and record["library_id"] == library)
    archive = safe_root(Path(root) / record["archive"])
    require(len(archive.relative_to(root).parts) == 2)
    episode = _json(sidecar_path(archive), 16 * 1024**2)
    series = _json(archive.parent / "series.json", 16 * 1024**2)
    validate_public_metadata(episode)
    validate_public_metadata(series, series=True)
    require(
        episode["export_id"] == record["episode_id"]
        and series["export_id"] == record["series_id"]
    )
    items = record["files"]
    require(isinstance(items, list) and len(items) >= 3)
    require(
        hashlib.sha256(sidecar_path(archive).read_bytes()).hexdigest()
        == items[1]["sha256"]
    )
    require(
        hashlib.sha256((items[0]["sha256"] + items[1]["sha256"]).encode()).hexdigest()
        == record["version"]
    )
    selection = None
    pages = episode.get("content_hashes")
    eligible = (
        episode["format"] == "cbz"
        and isinstance(pages, list)
        and 1 <= len(pages) <= 1000
        and all(
            isinstance(page, dict)
            and isinstance(page.get("file"), str)
            and re.search(r"\.(?:jpe?g|png|webp)$", page["file"], re.I)
            and type(page.get("size")) is int
            and 0 < page["size"] <= 32 * 1024**2
            for page in pages
        )
        and sum(page["size"] for page in pages) <= 256 * 1024**2
    )
    if eligible:
        key = (
            f"Comic-Archive/{library}/{record['series_id']}/{record['episode_id']}"
            f"/{record['version']}/episode.cbz"
        )
        size = transfer.size(key)
        require(type(size) is int and size > 0)
        if size <= 64 * 1024**2:
            selection = {"bytes": size, "sha256": items[0]["sha256"]}
    return {
        "format": PUBLIC,
        "library_id": library,
        "series_id": record["series_id"],
        "episode_id": record["episode_id"],
        "version": record["version"],
        "series_title": label(series["title"]),
        "title": label(episode["title"]),
        "order": episode["episode_number"],
        "archive": selection,
    }


def publish(root, transfer, record, library, target, *, advance=False):
    """Optional metadata after verified originals; failure never changes receipts.

    Real provider/live acceptance is separate. Test transports exercise data and
    file transitions, not actual B2 ownership or an account/file permission grant.
    """
    from .cloud import _write, settings
    from .cloud_history import retain

    try:
        with LibraryLock(root):
            current = settings(root)
            require(
                current is not None
                and current["library_id"] == library
                and current["targets"]["b2"]["target_id"] == target
                and transfer.provider == "b2"
                and record.get("state") == "verified"
                and record.get("library_id") == library
                and record.get("target_id") == target
                and isinstance(record.get("version"), str)
                and HEX.fullmatch(record["version"])
            )
            retain(
                root, "b2", record
            )  # Durable verified history BEFORE metadata publication.
            episode = record["episode_id"]
            path = state_path(root, library, target, episode)
            state = state_read(path, library, target, episode)
            if not needs(root, library, target, record):
                return True
            request = state["request"]
            advance = advance or (
                request is not None
                and request["version"] == record["version"]
                and request["advance"]
            )
            # Preserve fresh-upload intent before even a read-only provider
            # request can fail. Later verified-receipt retries are backfills.
            request = {"version": record["version"], "advance": advance}
            if state["request"] != request:
                state["request"] = request
                _write(path, state)
            observed, records = remote_head(transfer, library, episode)
            committed, pending = state["committed"], state["pending"]
            if committed is not None:
                require(
                    committed["revision"] in records
                    and records[committed["revision"]][2] == name(committed)
                )
            if pending is not None:
                if pending["revision"] in records:
                    require(records[pending["revision"]][2] == name(pending))
                else:
                    require(len(records) == pending["revision"] - 1)
                _send(transfer, path, pending)
                state.update(
                    committed=pending,
                    pending=None,
                    request=None,
                    seen_version=pending["version"],
                )
                _write(path, state)
                observed, records = remote_head(transfer, library, episode)
            request = {"version": record["version"], "advance": advance}
            if state["request"] != request:
                state["request"] = request
                _write(
                    path, state
                )  # Preserve retry intent even if the metadata stat fails.
            if observed is not None and (
                state["committed"] is None
                or observed["revision"] > state["committed"]["revision"]
            ):
                state["committed"] = observed  # Actual scoped immutable metadata only.
                _write(path, state)
            committed = state["committed"]
            if committed is not None and not advance:
                # Backfill/key rotation/older local restore adopts an existing
                # published selection; it never republishes an older edition.
                state.update(request=None, seen_version=record["version"])
                _write(path, state)
                return True
            requested = projection(root, transfer, record, library)
            if committed is not None and all(
                committed[key] == value for key, value in requested.items()
            ):
                state.update(request=None, seen_version=record["version"])
                _write(path, state)
                return True
            value = requested | {
                "revision": committed["revision"] + 1 if committed else 1,
                "previous": hashlib.sha256(encoded(committed)).hexdigest()
                if committed
                else ZERO,
            }
            checked(value, library, episode)
            state["pending"] = value
            _write(path, state)  # Fixed intent BEFORE first remote metadata mutation.
            _send(transfer, path, value)
            state.update(
                committed=value,
                pending=None,
                request=None,
                seen_version=record["version"],
            )
            _write(path, state)
            return True
    except (ArchiveError, OSError, ValueError, KeyError, TypeError):
        return False


def _send(transfer, path, value):
    raw = encoded(value)
    # The existing immutable copy+exact verification path; no mutable pointer,
    # guessed CAS primitive, source URL, original deletion or writer credential.
    with tempfile.TemporaryDirectory(dir=path.parent, prefix=".reader-") as temporary:
        output = Path(temporary) / "publication.json"
        output.write_bytes(raw)
        output.chmod(0o600)
        transfer.upload(
            output,
            prefix(value["library_id"], value["episode_id"]) + "/" + name(value),
            hashlib.sha256(raw).hexdigest(),
        )
    observed, records = remote_head(transfer, value["library_id"], value["episode_id"])
    require(
        observed is not None
        and value["revision"] in records
        and records[value["revision"]][2] == name(value)
    )
