"""Explicit POSIX host state custody; no account lease or network authority.

One real process lock and one thread own the private directory, current file FD
and identity. JSON alone cannot construct this owner. Unknown write durability
poisons the owner: inspect/reopen retained state, never retry approval blindly.
"""

import os
import stat
import threading
import uuid
from pathlib import Path

from .files import SharedRoot, SourceError, _directory
from .jsoncodec import dumps, loads, require
from .renewal import validate as validate_renewal
from .signaling import Identity, _bundle, _identity_record, _restore_identity
from .wire import _id

FORMAT = "comic-archiver.connected-host/1"
RENEWAL_FORMAT = "comic-archiver.connected-host/2"
MAXIMUM = 1024**2
PAIRINGS_MAX = 1024
STATE = "host-state.json"
LOCK = "host-state.lock"
CREATE = object()


def _integer(value):
    require(type(value) is int and 0 <= value < 2**31)


def _private(fd, directory=False):
    value = os.fstat(fd)
    require(value.st_uid == os.geteuid() and not value.st_mode & 0o077)
    require(
        stat.S_ISDIR(value.st_mode)
        if directory
        else stat.S_ISREG(value.st_mode) and value.st_nlink == 1
    )
    return value


def _stamp(value):
    return (
        value.st_dev,
        value.st_ino,
        value.st_size,
        value.st_mtime_ns,
        value.st_ctime_ns,
    )


def _validate(value):
    require(
        isinstance(value, dict)
        and value.get("format") in {FORMAT, RENEWAL_FORMAT}
        and set(value) == ({"format", "revision", "identity", "pairings"}
                           | ({"renewals", "renewed"} if value["format"] == RENEWAL_FORMAT else set()))
    )
    _id(value["revision"])
    require(
        isinstance(value["pairings"], list) and len(value["pairings"]) <= PAIRINGS_MAX
    )
    seen = set()
    for pair in value["pairings"]:
        require(
            isinstance(pair, dict)
            and set(pair)
            == {
                "pairing_id",
                "status",
                "generation",
                "account_binding",
                "reader_keys",
                "shares",
                "approval_id",
                "request_sha256",
            }
        )
        _id(pair["pairing_id"])
        require(pair["pairing_id"] not in seen)
        seen.add(pair["pairing_id"])
        require(pair["status"] in {"pending", "approved", "revoked"})
        _integer(pair["generation"])
        if pair["status"] == "pending":
            require(pair["generation"] == 0)
        if pair["status"] == "revoked":
            require(pair["generation"] > 0)
        require(isinstance(pair["shares"], list) and len(pair["shares"]) <= 16)
        for share in pair["shares"]:
            _id(share)
        require(len(set(pair["shares"])) == len(pair["shares"]))
        if pair["approval_id"] is None:
            require(
                pair["status"] != "approved"
                and pair["account_binding"] is None
                and pair["reader_keys"] is None
                and not pair["shares"]
                and pair["request_sha256"] is None
            )
        else:
            _id(pair["approval_id"])
            _id(pair["account_binding"])
            _bundle(pair["reader_keys"])
            digest = pair["request_sha256"]
            require(
                isinstance(digest, str)
                and len(digest) == 64
                and all(ch in "0123456789abcdef" for ch in digest)
            )
            require(pair["status"] != "pending")
        if pair["status"] == "approved":
            require(pair["shares"])
        if pair["status"] == "revoked":
            require(not pair["shares"])

    if value["format"] == RENEWAL_FORMAT:
        rows = {row["pairing_id"]: row for row in value["pairings"]}
        for collection in ("renewals", "renewed"):
            renewals = value[collection]
            require(isinstance(renewals, list) and len(renewals) <= len(seen))
            pending = set()
            for renewal in renewals:
                validate_renewal(renewal)
                pair = renewal["pairing_id"]
                require(pair in rows and pair not in pending)
                pending.add(pair)
                validate_renewal(renewal, rows[pair], completed=collection == "renewed")


class HostState:
    """Owns actual private state handles and lock; not a service/account owner."""

    def __init__(self, token, path, directory, lock_fd, state_fd, value):
        require(token is CREATE)
        self._initializing = True
        self.path, self.directory, self.lock_fd, self.state_fd = (
            path,
            directory,
            lock_fd,
            state_fd,
        )
        self.thread, self.closed = threading.get_ident(), False
        self.directory_identity = _stamp(os.fstat(directory))[:2]
        self.lock_identity = _stamp(_private(lock_fd))[:2]
        self.state_stamp = _stamp(_private(state_fd))
        _validate(value)
        self._data = value
        self.identity = _restore_identity(value["identity"])
        self.current()
        self._initializing = False

    @classmethod
    def open(cls, path, roots=(), create=False):
        directory = lock_fd = state_fd = None
        try:
            require(os.name == "posix", "unsupported_format")
            import fcntl

            path = Path(path).absolute()
            require(".." not in path.parts and path.name not in {"", ".", ".."})
            for root in roots:
                require(isinstance(root, SharedRoot))
                root.current()
                require(not path.is_relative_to(root.path), "resource_limit")
            if create:
                # Init is an explicit new-directory operation. Never recreate a
                # missing identity inside an existing/recovered state directory.
                parent = _directory(path.parent)
                try:
                    os.mkdir(path.name, 0o700, dir_fd=parent)
                    os.fsync(parent)
                finally:
                    os.close(parent)
            directory = _directory(path)
            _private(directory, directory=True)
            flags = os.O_RDWR | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK
            lock_fd = os.open(
                LOCK,
                flags | (os.O_CREAT | os.O_EXCL if create else 0),
                0o600,
                dir_fd=directory,
            )
            _private(lock_fd)
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise SourceError("busy") from None
            if create:
                value = dict(
                    format=FORMAT,
                    revision=str(uuid.uuid4()),
                    identity=_identity_record(Identity.generate()),
                    pairings=[],
                )
                encoded = dumps(value, MAXIMUM).encode("utf-8")
                state_fd = os.open(
                    STATE, flags | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=directory
                )
                cls._write(state_fd, encoded)
                os.fsync(state_fd)
                os.fsync(directory)
            else:
                state_fd = os.open(STATE, flags, dir_fd=directory)
            value = cls._read(state_fd)
            return cls(CREATE, path, directory, lock_fd, state_fd, value)
        except Exception as error:
            for fd in [state_fd, lock_fd, directory]:
                if fd is not None:
                    os.close(fd)
            raise SourceError(
                error.code if isinstance(error, SourceError) else "integrity_failed"
            ) from None

    @staticmethod
    def _write(fd, data):
        offset = 0
        while offset < len(data):
            count = os.write(fd, data[offset:])
            require(count > 0)
            offset += count

    @staticmethod
    def _read(fd):
        value = _private(fd)
        require(0 < value.st_size <= MAXIMUM)
        raw = os.pread(fd, value.st_size + 1, 0)
        require(len(raw) == value.st_size and _stamp(os.fstat(fd)) == _stamp(value))
        return loads(raw.decode("utf-8"), MAXIMUM)

    def current(self):
        require(threading.get_ident() == self.thread, "cancelled")
        try:
            require(not self.closed, "cancelled")
            _private(self.directory, directory=True)
            current = _directory(self.path)
            try:
                require(_stamp(os.fstat(current))[:2] == self.directory_identity)
            finally:
                os.close(current)
            require(
                _stamp(os.stat(LOCK, dir_fd=self.directory, follow_symlinks=False))[:2]
                == self.lock_identity
            )
            _private(self.lock_fd)
            require(
                _stamp(_private(self.state_fd)) == self.state_stamp
                and _stamp(os.stat(STATE, dir_fd=self.directory, follow_symlinks=False))
                == self.state_stamp
            )
            return True
        except Exception:
            if not self._initializing:
                self.close()
            raise SourceError("integrity_failed") from None

    def close(self):
        require(threading.get_ident() == self.thread, "cancelled")
        if self.closed:
            return
        self.closed = True
        self.identity = None
        for name in ["state_fd", "lock_fd", "directory"]:
            fd = getattr(self, name)
            if fd is not None:
                os.close(fd)
                setattr(self, name, None)

    def records(self):
        self.current()
        return loads(dumps(self._data["pairings"], MAXIMUM), MAXIMUM)

    def record(self, pairing_id):
        self.current()
        _id(pairing_id)
        row = next(
            (
                value
                for value in self._data["pairings"]
                if value["pairing_id"] == pairing_id
            ),
            None,
        )
        # Same native descriptor/stamp checks, bounded copy of this one record.
        # Avoid serializing the whole1024-record store before every data frame.
        return None if row is None else loads(dumps(row, 8192), 8192)

    def _commit(self, pairs, renewals=None, renewed=None):
        """Caller is the serialized local owner. No JSON reply is read authority."""
        temporary = old_fd = None
        try:
            self.current()
            value = dict(self._data, pairings=pairs, revision=str(uuid.uuid4()))
            if renewals is not None:
                value.update(format=RENEWAL_FORMAT, renewals=renewals,
                             renewed=self._data.get("renewed", []) if renewed is None else renewed)
            elif renewed is not None:
                require(value["format"] == RENEWAL_FORMAT)
                value["renewed"] = renewed
            _validate(value)
            encoded = dumps(value, MAXIMUM).encode("utf-8")
            temporary_name = ".pending-" + str(uuid.uuid4())
            temporary = os.open(
                temporary_name,
                os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                0o600,
                dir_fd=self.directory,
            )
            self._write(temporary, encoded)
            os.fsync(temporary)
            self.current()
            os.replace(
                temporary_name,
                STATE,
                src_dir_fd=self.directory,
                dst_dir_fd=self.directory,
            )
            os.fsync(self.directory)
            # Independent readback through the actual installed descriptor and
            # pathname. A lost replace/fsync/readback outcome poisons this owner.
            require(
                _stamp(os.stat(STATE, dir_fd=self.directory, follow_symlinks=False))
                == _stamp(_private(temporary))
            )
            require(os.pread(temporary, len(encoded) + 1, 0) == encoded)
            old_fd = self.state_fd
            self.state_fd, temporary = temporary, None
            self.state_stamp = _stamp(_private(self.state_fd))
            self._data = self._read(self.state_fd)
            self.current()
            os.close(old_fd)
            old_fd = None
        except Exception:
            if temporary is not None:
                os.close(temporary)
            # Retain uncertain private files for inspection. Never acknowledge
            # approval or recreate/retry from an uncertain stored outcome.
            self.close()
            raise SourceError("integrity_failed") from None
        finally:
            if old_fd is not None:
                os.close(old_fd)

    def renewal(self, pairing_id):
        self.current()
        _id(pairing_id)
        found = next((item for item in self._data.get("renewals", [])
                      if item["pairing_id"] == pairing_id), None)
        return None if found is None else loads(dumps(found, 8192), 8192)

    def receipt(self, pairing_id):
        self.current()
        _id(pairing_id)
        found = next((item for item in self._data.get("renewed", [])
                      if item["pairing_id"] == pairing_id), None)
        return None if found is None else loads(dumps(found, 8192), 8192)

    def _prepare_renewal(self, intent):
        self.current()
        row = self.record(intent["pairing_id"])
        validate_renewal(intent, row)
        require(row["generation"] == intent["from_generation"], "revoked")
        prior = self.renewal(intent["pairing_id"])
        if prior is not None:
            require(prior == intent, "busy")
            return prior
        renewals = loads(dumps(self._data.get("renewals", []), MAXIMUM), MAXIMUM)
        renewals.append(intent)
        self._commit(self.records(), renewals)
        return self.renewal(intent["pairing_id"])

    def _finish_renewal(self, intent, status):
        pair = intent["pairing_id"]
        require(self.renewal(pair) == intent, "revoked")
        pairs = self.records()
        row = next((item for item in pairs if item["pairing_id"] == pair), None)
        validate_renewal(intent, row)
        if row["generation"] == intent["from_generation"]:
            require(status == "committed", "revoked")
            row["generation"] = intent["generation"]
        else:
            require(status in {"committed", "not_committed", "superseded"})
        renewals = [item for item in self._data["renewals"] if item["pairing_id"] != pair]
        renewed = [item for item in self._data.get("renewed", []) if item["pairing_id"] != pair]
        if status == "committed":
            renewed.append(intent)
        elif self.receipt(pair) is not None:
            renewed.append(self.receipt(pair))
        self._commit(pairs, renewals, renewed)

    def _pending(self, pairing_id):
        _id(pairing_id)
        pairs = self.records()
        require(
            len(pairs) < PAIRINGS_MAX
            and all(p["pairing_id"] != pairing_id for p in pairs),
            "resource_limit",
        )
        pairs.append(
            dict(
                pairing_id=pairing_id,
                status="pending",
                generation=0,
                account_binding=None,
                reader_keys=None,
                shares=[],
                approval_id=None,
                request_sha256=None,
            )
        )
        self._commit(pairs)

    def _approve(
        self,
        pairing_id,
        account_binding,
        reader_keys,
        shares,
        approval_id,
        request_sha256,
    ):
        pairs = self.records()
        row = next((p for p in pairs if p["pairing_id"] == pairing_id), None)
        require(row is not None and row["status"] == "pending", "revoked")
        row.update(
            status="approved",
            account_binding=account_binding,
            reader_keys=reader_keys,
            shares=shares,
            approval_id=approval_id,
            request_sha256=request_sha256,
        )
        self._commit(pairs)

    def revoke(self, pairing_id):
        _id(pairing_id)
        pairs = self.records()
        row = next((p for p in pairs if p["pairing_id"] == pairing_id), None)
        require(row is not None, "not_found")
        if row["status"] == "revoked":
            return False
        intent = self.renewal(pairing_id)
        generation = max(row["generation"], intent["generation"] if intent else 0) + 1
        require(generation < 2**31, "resource_limit")
        row.update(status="revoked", generation=generation, shares=[])
        renewals = ([item for item in self._data["renewals"] if item["pairing_id"] != pairing_id]
                    if self._data["format"] == RENEWAL_FORMAT else None)
        renewed = ([item for item in self._data["renewed"] if item["pairing_id"] != pairing_id]
                   if self._data["format"] == RENEWAL_FORMAT else None)
        self._commit(pairs, renewals, renewed)
        return True

    def remove_share(self, share_id):
        _id(share_id)
        pairs = self.records()
        changed = False
        for row in pairs:
            if row["status"] == "approved" and share_id in row["shares"]:
                intent = self.renewal(row["pairing_id"])
                generation = max(row["generation"], intent["generation"] if intent else 0) + 1
                require(generation < 2**31, "resource_limit")
                row["shares"].remove(share_id)
                row["generation"] = generation
                if not row["shares"]:
                    row["status"] = "revoked"
                changed = True
        if changed:
            approved = {row["pairing_id"] for row in pairs if row["status"] == "approved"}
            renewals = ([item for item in self._data["renewals"]
                         if item["pairing_id"] in approved]
                        if self._data["format"] == RENEWAL_FORMAT else None)
            renewed = ([item for item in self._data["renewed"] if item["pairing_id"] in approved]
                       if self._data["format"] == RENEWAL_FORMAT else None)
            self._commit(pairs, renewals, renewed)
        return changed

    def retire_pending(self):
        pairs = self.records()
        changed = False
        for row in pairs:
            if row["status"] == "pending":
                row.update(status="revoked", generation=row["generation"] + 1)
                changed = True
        if changed:
            self._commit(pairs)
