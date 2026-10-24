"""Fleet invoked with installed Python -I; real installed bytes and saved owners."""

import argparse
import configparser
import hashlib
import importlib.metadata
import json
import sys
from pathlib import Path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--expected", type=Path, required=True)
    parser.add_argument("--state", type=Path)
    parser.add_argument("--root", type=Path)
    parser.add_argument("--index", type=Path)
    args = parser.parse_args()
    expected = json.loads(args.expected.read_text())
    prefix = Path(sys.prefix).resolve(strict=True)
    assert sys.flags.isolated and prefix != Path(sys.base_prefix).resolve()
    distribution = importlib.metadata.distribution("comic-archive")
    assert distribution.metadata["Name"] == "comic-archive"
    assert distribution.version == expected["version"]
    for name, record in expected["installed_files"].items():
        path = Path(distribution.locate_file(name))
        assert path.is_file() and not path.is_symlink()
        assert path.resolve(strict=True).is_relative_to(prefix)
        raw = path.read_bytes()
        assert record == {"bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest()}
    actual = {
        item.metadata["Name"].lower().replace("_", "-"): item.version
        for item in importlib.metadata.distributions()
    }
    assert actual == dict(
        expected["runtime_versions"], **{"comic-archive": expected["version"]}
    )
    entries = configparser.ConfigParser()
    entries.read_string(distribution.read_text("entry_points.txt"))
    assert entries["gui_scripts"]["comic-archiver"] == "comic_archive.desktop_app:main"
    if expected["console"]:
        assert dict(entries["console_scripts"]) == {
            "comic-archiver-reader": "comic_archive.connected.__main__:main"
        }
    else:
        assert not entries.has_section("console_scripts")

    import comic_archive

    for name in ("__main__", "controller", "driver", "rtc"):
        importlib.import_module("comic_archive.connected." + name)

    assert Path(comic_archive.__file__).resolve(strict=True).is_relative_to(prefix)
    forbidden = (
        "comic_archive.cli",
        "comic_archive.adapters",
        "comic_archive.sites",
        "comic_archive.session",
        "comic_archive.desktop_app",
        "comic_archive.app_runtime",
    )
    assert not any(
        name == part or name.startswith(part + ".")
        for name in sys.modules
        for part in forbidden
    )
    result = {
        "installed_bytes_match": True,
        "isolated_import": True,
        "runtime_versions": actual,
        "downloader_imported": False,
        "console": expected["console"],
    }
    if args.state is not None:
        assert args.root is not None
        from comic_archive.connected.files import Index, SharedRoot
        from comic_archive.connected.state import STATE, HostState

        root = SharedRoot(args.root)
        try:
            assert args.index is not None
            index = Index(args.index, root)
            try:
                result["saved_catalogue"] = hashlib.sha256(
                    json.dumps(
                        {"share": index.share_id, "catalogue": index.snapshot()},
                        sort_keys=True,
                        separators=(",", ":"),
                    ).encode()
                ).hexdigest()
            finally:
                index.close()
            state = HostState.open(args.state, roots=[root])
            try:
                semantic = {
                    "identity": state.identity.public_bundle(),
                    "pairs": state.records(),
                }
                result["saved_approval"] = hashlib.sha256(
                    json.dumps(semantic, sort_keys=True, separators=(",", ":")).encode()
                ).hexdigest()
            finally:
                state.close()
        finally:
            root.close()
        result["saved_state_bytes"] = hashlib.sha256(
            (args.state / STATE).read_bytes()
        ).hexdigest()
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
