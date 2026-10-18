"""Import explicitly ordered files the user has already downloaded."""
import json
from pathlib import Path
from ..models import ArchiveError, Episode, Manifest, Page, Series


class LocalAdapter:
    def __init__(self, path: Path):
        self.root = path.resolve().parent
        try:
            obj = json.loads(path.read_text(encoding="utf-8"))
            s = obj["series"]
            creators = s.get("creators", [])
            # A plain string would otherwise become one creator per character.
            if not isinstance(creators, list) or any(not isinstance(name, str) for name in creators):
                raise ValueError
            series = Series(s["id"], s["title"], tuple(creators),
                            s.get("description"), s.get("source_url"), s.get("source", "tapas"))
            episodes = []
            for e in obj["episodes"]:
                pages = []
                for p in e["pages"]:
                    location = (self.root / p["file"]).resolve()
                    if not location.is_relative_to(self.root) or not location.is_file():
                        raise ArchiveError("Page must be an existing file inside the manifest directory.")
                    pages.append(Page(p["order"], str(location)))
                episodes.append(Episode(e["id"], series.id, e["title"], e["order"],
                                        tuple(pages), "local_owned", e.get("source_url"),
                                        e.get("published_at")))
            self._manifest = Manifest(series, tuple(episodes))
        except (KeyError, TypeError, ValueError, OSError):
            raise ArchiveError("Invalid or unreadable local manifest.") from None

    def manifest(self):
        return self._manifest

    def retrieve(self, page):
        try:
            if Path(page.location).stat().st_size > 100 * 1024 * 1024:
                raise ArchiveError("Page exceeds the 100 MiB limit.")
            return Path(page.location).read_bytes()
        except OSError:
            raise ArchiveError("Cannot read a local page.") from None

    def refresh(self, episode):
        raise ArchiveError("Local file retrieval failed; check the source files.")
