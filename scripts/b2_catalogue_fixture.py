"""Fleet-only genuine local upload/catalogue writers with recording DATA transport.

Output is synthetic public content for matched JS/browser consumers, not live B2.
"""

import base64
import hashlib
import json
import os
import sys
from pathlib import Path
from contextlib import closing

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "tests")]
import test_cloud as helpers

from comic_archive import cloud
from comic_archive.models import Episode, Manifest, Page
from comic_archive.publication import PublicationStore


def main():
    assert os.environ.get("FLEET_STORAGE_JOB")
    case = helpers.CloudTests()
    case.setUp()
    try:
        case.publish()
        case.ready()
        for number in range(82, 104):
            case.publish(str(number), novel=number == 83)
        receipts = [
            json.loads(path.read_text())
            for path in (case.root / ".archive-state/cloud/outbox/b2").glob("*.json")
        ]
        selected = next(value for value in receipts if "Chapter 82" in value["archive"])
        library = cloud.settings(case.root)["library_id"]
        prefix = "Comic-Archive/" + library + "/"
        earlier = (
            prefix
            + selected["series_id"]
            + "/"
            + selected["episode_id"]
            + "/"
            + selected["version"]
            + "/episode.cbz"
        )
        episode = Episode('82', '42', 'Chapter 82 — current edition', 82,
                          (Page(1, 'https://cdn.test/a', False),), 'tapas_free')
        with closing(PublicationStore(case.root)) as store:
            store.archive(Manifest(case.series, (episode,)), helpers.MemoryAdapter(),
                          progress=lambda _: None)
        current = json.loads(
            (
                case.root
                / ".archive-state/cloud/outbox/b2"
                / (selected["episode_id"] + ".json")
            ).read_text()
        )
        key = earlier.replace(selected["version"], current["version"])
        raw = helpers.FakeTransfer.files[("b2", key)]
        output = {
            "format": "comic-archiver.synthetic-b2-catalogue/1",
            "prefix": prefix,
            "episode": selected["episode_id"],
            "key": key,
            "earlier_key": earlier,
            "digest": hashlib.sha256(raw).hexdigest(),
            "objects": {
                name: base64.b64encode(value).decode()
                for (provider, name), value in helpers.FakeTransfer.files.items()
                if provider == "b2"
            },
            "provenance": "actual local queue/upload/catalogue writers; recording DATA transport",
        }
        Path(sys.argv[1]).write_text(json.dumps(output, sort_keys=True) + "\n")
        print(
            json.dumps(
                {
                    "b2_catalogue_producer": "passed",
                    "episodes": len(receipts),
                    "revisions": 2,
                    "provider": "synthetic DATA transport",
                }
            ),
            flush=True,
        )
    finally:
        case.doCleanups()


if __name__ == "__main__":
    main()
