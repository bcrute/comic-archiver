"""Fleet-only installed producer upgrade; object transport is synthetic DATA.

No B2 session/owner/permission is constructed or verified here. Genuine installed
publication/receipt writers run against a recording object store with no network.
"""

import argparse
import base64
import hashlib
import importlib.metadata
import importlib.util
import json
import os
import socket
import struct
import sys
import threading
import zlib
from contextlib import closing
from pathlib import Path, PurePosixPath
from unittest.mock import patch


def fingerprint(raw):
    return hashlib.sha256(raw).hexdigest()


def png():
    def chunk(kind, raw):
        return struct.pack(">I", len(raw)) + kind + raw + struct.pack(">I", zlib.crc32(kind + raw))

    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(b"\x00\x14\x28\x50")) + chunk(b"IEND", b""))


class ObjectData:
    """Recording DATA adapter, not the actual Transfer or a B2 authority."""

    folder = None
    lose_reply = False

    def __init__(self, provider, target):
        assert provider == "b2" and set(target) >= {"remote", "config", "target_id"}
        self.provider = provider

    @classmethod
    def path(cls, name):
        parsed = PurePosixPath(name)
        assert not parsed.is_absolute() and ".." not in parsed.parts and "\\" not in name
        return cls.folder / str(parsed)

    def remote(self, name):
        return name

    def upload(self, source, name, expected):
        raw = Path(source).read_bytes()
        assert fingerprint(raw) == expected
        self.upload_bytes(raw, name)
        self.verify(name, expected)
        if "/.reader/" in name and ObjectData.lose_reply:
            ObjectData.lose_reply = False
            from comic_archive.models import ArchiveError

            raise ArchiveError("Synthetic lost object reply")

    def upload_bytes(self, raw, name):
        path = self.path(name)
        if path.exists():
            assert path.read_bytes() == raw, "An immutable synthetic original changed"
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(raw)
            path.chmod(0o600)

    def verify(self, name, expected, **_options):
        assert fingerprint(self.path(name).read_bytes()) == expected

    def sha1s(self, name):
        path = self.path(name)
        paths = [path] if path.is_file() else list(path.rglob("*"))
        return {(item.name if item == path else item.relative_to(path).as_posix()):
                hashlib.sha1(item.read_bytes(), usedforsecurity=False).hexdigest()
                for item in paths if item.is_file()}

    def delete_test_file(self, name):
        self.path(name).unlink(missing_ok=True)

    def delete_upload(self, name):
        self.path(name).unlink(missing_ok=True)

    def size(self, name):
        return self.path(name).stat().st_size

    def run(self, command, name, *_args, **_options):
        path = self.path(name)
        if command == "cat":
            return path.read_bytes()
        assert command == "lsf"
        return "\n".join(sorted(item.name for item in path.glob("*") if item.is_file())).encode()


def objects(folder):
    return {path.relative_to(folder).as_posix(): fingerprint(path.read_bytes())
            for path in folder.rglob("*") if path.is_file()}


def write(path, value):
    path.write_text(json.dumps(value, sort_keys=True) + "\n")
    path.chmod(0o600)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("phase", choices=("prior", "upgrade", "reopen"))
    parser.add_argument("--work", type=Path, required=True)
    parser.add_argument("--expected", type=Path, required=True)
    args = parser.parse_args()
    assert os.environ.get("FLEET_STORAGE_JOB") and sys.flags.isolated
    work = args.work.resolve(strict=True)
    prefix = Path(sys.prefix).resolve(strict=True)
    assert prefix != Path(sys.base_prefix).resolve()
    expected = json.loads(args.expected.read_text())
    distribution = importlib.metadata.distribution("comic-archive")
    for name, identity in expected["installed_files"].items():
        path = Path(distribution.locate_file(name))
        assert path.is_file() and not path.is_symlink()
        assert path.resolve(strict=True).is_relative_to(prefix)
        raw = path.read_bytes()
        assert identity == {"bytes": len(raw), "sha256": fingerprint(raw)}
    actual_versions = {item.metadata["Name"].lower().replace("_", "-"): item.version
                       for item in importlib.metadata.distributions()}
    assert actual_versions == dict(expected["runtime_versions"], **{"comic-archive": expected["version"]})
    assert not set(actual_versions) & {"pillow", "playwright", "pyside6", "ruff"}
    ObjectData.folder = work / "synthetic-object-data"
    ObjectData.folder.mkdir(mode=0o700, exist_ok=True)
    library = work / "User Library"
    state = work / "observations.json"
    attempts = []
    original_threads = {item.ident for item in threading.enumerate()}

    def denied(*_args, **_kwargs):
        attempts.append("connect")
        raise AssertionError("Network connection refused by installed DATA profile")

    with patch.object(socket.socket, "connect", denied), patch.object(socket.socket, "connect_ex", denied):
        with socket.socket() as control:
            try:
                control.connect(("127.0.0.1", 9))
            except AssertionError:
                pass
        assert attempts == ["connect"]
        from comic_archive import cloud
        from comic_archive.models import Episode, Manifest, Page, Series
        from comic_archive.publication import PublicationStore

        def publish(key, title):
            class MemoryData:
                def retrieve(self, _page):
                    return png()

            episode = Episode(key, "42", title, int(key),
                              (Page(1, "https://never-fetch.invalid/page", False),), "tapas_free")
            with closing(PublicationStore(library)) as store:
                store.archive(Manifest(Series("42", "Synthetic installed library"), (episode,)),
                              MemoryData(), progress=lambda _: None)

        with patch.object(cloud, "Transfer", ObjectData):
            if args.phase == "prior":
                assert importlib.util.find_spec("comic_archive.reader_catalogue") is None
                publish("80", "Setup original")
                config = work / "synthetic-config.conf"
                config.write_text("[b2]\ntype=b2\narchive_bucket=synthetic-library\n")
                config.chmod(0o600)
                cloud.configure_target(library, "b2", config=config)
                assert cloud.smoke_test(library, "b2", episodes=1)["test_files_deleted"]
                cloud.enable(library, "b2", True)
                # Exercise the genuine earlier queued receipt writer; the
                # upgraded edition below uses the ordinary direct writer too.
                with patch.object(cloud, "direct_target", return_value=None):
                    publish("82", "Earlier installed edition")
                receipt = next((library / ".archive-state/cloud/outbox/b2").glob("*.json"))
                raw = receipt.read_bytes()
                record = json.loads(raw)
                assert record["state"] == "verified" and record["eviction_requested"]
                assert not (library / record["archive"]).exists()
                originals = objects(ObjectData.folder)
                assert len(originals) == 3 and not any("/.reader/" in name for name in originals)
                write(state, {"episode": record["episode_id"], "old_receipt": base64.b64encode(raw).decode(),
                              "originals": originals})
            else:
                from comic_archive import reader_catalogue as catalogue

                observed = json.loads(state.read_text())
                receipt = library / ".archive-state/cloud/outbox/b2" / (observed["episode"] + ".json")
                old = base64.b64decode(observed["old_receipt"], validate=True)
                settings = cloud.settings(library)
                lib, target = settings["library_id"], settings["targets"]["b2"]["target_id"]
                head = catalogue.state_path(library, lib, target, observed["episode"])

                def selected():
                    return catalogue.state_read(head, lib, target, observed["episode"])

                if args.phase == "upgrade":
                    assert receipt.read_bytes() == old
                    assert cloud.upload_pending(library)["uploaded_episodes"] == 0
                    assert receipt.read_bytes() == old
                    assert selected()["committed"]["revision"] == 1
                    ObjectData.lose_reply = True
                    publish("82", "Current installed edition")
                    record = json.loads(receipt.read_bytes())
                    assert record["state"] == "verified" and record["eviction_requested"]
                    assert not (library / record["archive"]).exists()
                    assert not list((library / ".archive-state/cloud/direct/b2").glob("*.json"))
                    pending = selected()["pending"]
                    assert pending["revision"] == 2 and pending["title"] == "Current installed edition"
                    observed.update(current_receipt=base64.b64encode(receipt.read_bytes()).decode(),
                                    pending=pending, after_unknown_reply=objects(ObjectData.folder))
                    write(state, observed)
                else:
                    current = base64.b64decode(observed["current_receipt"], validate=True)
                    assert receipt.read_bytes() == current
                    assert selected()["pending"] == observed["pending"]
                    assert cloud.upload_pending(library)["uploaded_episodes"] == 0
                    assert selected()["pending"] is None
                    assert selected()["committed"] == observed["pending"]
                    assert receipt.read_bytes() == current
                    assert objects(ObjectData.folder) == observed["after_unknown_reply"]
                    # Genuine earlier outbox and absent optional metadata head;
                    # neither becomes authority to replace the newer selection.
                    receipt.write_bytes(old)
                    head.unlink()
                    assert cloud.upload_pending(library)["uploaded_episodes"] == 0
                    assert receipt.read_bytes() == old
                    assert selected()["committed"] == observed["pending"]
                    assert objects(ObjectData.folder) == observed["after_unknown_reply"]
                    receipt.write_bytes(current)
                    assert cloud.upload_pending(library)["uploaded_episodes"] == 0
                    assert receipt.read_bytes() == current
                    final = selected()["committed"]
                    assert final["archive"]["bytes"] > 0
                    key = (f"Comic-Archive/{lib}/{final['series_id']}/{final['episode_id']}/"
                           f"{final['version']}/episode.cbz")
                    assert fingerprint(ObjectData.path(key).read_bytes()) == final["archive"]["sha256"]
                    assert len([name for name in objects(ObjectData.folder) if "/.reader/" in name]) == 2
                after = objects(ObjectData.folder)
                assert all(after[name] == digest for name, digest in observed["originals"].items())
        assert attempts == ["connect"], "Production flow attempted network I/O"
        assert {item.ident for item in threading.enumerate()} == original_threads
        for descriptor in Path("/proc/self/fd").iterdir():
            try:
                target = descriptor.readlink()
            except FileNotFoundError:
                continue
            assert not str(target).startswith(str(library) + "/"), "Owned library descriptor remained open"
        for name, module in tuple(sys.modules.items()):
            if name.startswith("comic_archive") and getattr(module, "__file__", None):
                assert Path(module.__file__).resolve(strict=True).is_relative_to(prefix)
        forbidden = ("comic_archive.adapters", "comic_archive.cli", "comic_archive.session",
                     "comic_archive.desktop_app", "comic_archive.app_runtime", "PIL", "PySide6", "playwright")
        assert not any(name == value or name.startswith(value + ".") for name in sys.modules for value in forbidden)
    result = {"phase": args.phase, "installed_package_bytes": True, "isolated_poisoned_cwd": True,
              "runtime_distributions": len(actual_versions), "unexpected_network_connections": 0,
              "owned_threads_and_library_descriptors_retired": True,
              "object_transport": "synthetic filesystem DATA, not actual B2", "passed": True}
    write(work / (args.phase + "-result.json"), result)
    print(json.dumps(result), flush=True)


if __name__ == "__main__":
    main()
