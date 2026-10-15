"""Bounded, metadata-only discovery through links in the account's reading list.

This report is an access observation, never an authorization source for imports.
Every archive still checks the live episode info before requesting its reader.
"""
import json
from datetime import datetime, timezone
from pathlib import Path
from .archive import atomic_json, directory
from .models import ArchiveError


CATEGORIES = ("subscribed", "recent", "updated")
DISCOVERY_CATEGORIES = CATEGORIES + ('free_episodes',)
ACCESSIBLE = {"tapas_free", "tapas_unlocked"}
DISCOVERY_VERSION = 4


def now():
    return datetime.now(timezone.utc).isoformat()


def observed_id(value):
    return isinstance(value, str) and 0 < len(value) <= 20 and value.isascii() and value.isdigit() and 0 < int(value)


def summarize_series(inspection):
    if (inspection.get("authenticated") is not True or inspection.get("complete") is not True
            or not observed_id(inspection.get("source_series_id"))):
        raise ArchiveError("Discovery requires a complete authenticated series enumeration.")
    episodes = inspection.get("episodes")
    if (not isinstance(episodes, list) or type(inspection.get("episode_count")) is not int
            or len(episodes) != inspection["episode_count"]):
        raise ArchiveError("Discovery received an inconsistent episode count.")
    selected = []
    ids = set()
    for item in episodes:
        if (not isinstance(item, dict) or not observed_id(item.get("id")) or item["id"] in ids
                or (item.get("order") is not None and (type(item["order"]) is not int or item["order"] < 1))):
            raise ArchiveError("Discovery received invalid or repeated episode metadata.")
        ids.add(item["id"])
        if item.get("access_state") in ACCESSIBLE:
            selected.append({"id": item["id"], "title": item["title"], "order": item.get("order"),
                             "source_scene": item.get("source_scene"),
                             "access_state": item["access_state"],
                             "source_url": f"https://tapas.io/episode/{item['id']}"})
    return {"source_series_id": inspection["source_series_id"], "title": inspection["title"],
            "episode_count": len(episodes), "free_episode_count": sum(e["access_state"] == "tapas_free" for e in selected),
            "unlocked_episode_count": sum(e["access_state"] == "tapas_unlocked" for e in selected),
            "unavailable_or_unverified_count": len(episodes) - len(selected),
            "accessible_episodes": sorted(selected, key=lambda e: (e["order"] is None, e["order"] or 0, int(e["id"]))),
            "ordering_verified": all(e.get("order") is not None for e in episodes), "checked_at": now(),
            "enumeration_complete": True, "from_previous_scan": False}


def cached_observation(item):
    """Keep only report fields; never replay URLs or unknown cached properties."""
    key = item["source_series_id"]
    rows = item["accessible_episodes"]
    count = item["episode_count"]
    if (not observed_id(key) or not isinstance(rows, list) or type(count) is not int
            or count < len(rows) or item["enumeration_complete"] is not True
            or not isinstance(item["title"], str) or not isinstance(item["checked_at"], str)):
        raise ValueError
    selected = []
    for row in rows:
        if (not observed_id(row.get("id")) or row.get("access_state") not in ACCESSIBLE
                or (row.get("order") is not None and (type(row["order"]) is not int or row["order"] < 1))
                or not isinstance(row.get("title"), str)):
            raise ValueError
        selected.append({"id": row["id"], "order": row["order"], "title": row["title"],
                         "source_scene": row.get("source_scene") if type(row.get("source_scene")) is int else None,
                         "access_state": row["access_state"], "source_url": f"https://tapas.io/episode/{row['id']}"})
    if len({e["id"] for e in selected}) != len(selected):
        raise ValueError
    return {"source_series_id": key, "title": item["title"], "episode_count": count,
            "free_episode_count": sum(e["access_state"] == "tapas_free" for e in selected),
            "unlocked_episode_count": sum(e["access_state"] == "tapas_unlocked" for e in selected),
            "unavailable_or_unverified_count": count - len(selected), "accessible_episodes": selected,
            "ordering_verified": item.get("ordering_verified", True) is True,
            "checked_at": item["checked_at"], "enumeration_complete": True, "from_previous_scan": True}


def discover_available(adapter, report_path, categories=None, resume=False):
    categories = list(CATEGORIES) if categories is None else categories
    if (not isinstance(categories, (list, tuple)) or not categories or len(categories) > 4
            or any(not isinstance(c, str) or c not in DISCOVERY_CATEGORIES for c in categories)
            or len(set(categories)) != len(categories) or type(resume) is not bool):
        raise ArchiveError("Discovery needs distinct subscribed/recent/updated/free_episodes categories and an explicit resume flag.")
    report_path = Path(report_path)
    directory(report_path.parent)
    if report_path.is_symlink():
        raise ArchiveError("Refusing a symlink in discovery output.")
    previous = {}
    if resume and report_path.exists():
        try:
            if report_path.stat().st_size > 16 * 1024 * 1024:
                raise ValueError
            old = json.loads(report_path.read_text())
            if old["schema_version"] != 1 or old["source"] != "tapas":
                raise ValueError
            for item in old["series"]:
                normalized = cached_observation(item)
                previous[normalized["source_series_id"]] = normalized
        except (ValueError, KeyError, TypeError, AttributeError):
            raise ArchiveError("Discovery report cannot be resumed; run again without --resume.") from None
    report = {"schema_version": 1, "document_type": "comic_access_discovery", "source": "tapas", "started_at": now(), "state": "running",
              "complete": False, "scope": "observed_reading_list_series", "access_snapshot_only": True,
              "categories": [], "series": list(previous.values()), "pending_series_ids": [],
              "limitations": ["Rendered reading-list boundaries do not prove complete account purchase/unlock coverage.",
                              "Unlocked means the server flag; it does not prove a permanent purchase.",
                              "The optional Free Episodes view supplies candidates; it does not prove account membership or a purchase.",
                              "Dated access observations must be rechecked by the importer."]}

    def save():
        report["updated_at"] = now()
        atomic_json(report_path, report)
        report_path.chmod(0o600)

    save()
    try:
        candidates = {}
        for category in categories:
            print(f"Discovery: inspecting the {category} reading list.", flush=True)
            library = adapter.inspect_library(category=category)
            if (library.get("authenticated") is not True or library.get("membership_verified") is not True
                    or library.get("scope") != category):
                raise ArchiveError("Requested reading-list category was not verified in the normal site.")
            report["categories"].append({"name": category, "complete": library.get("complete") is True,
                                         "visible_series_count": len(library["series"]),
                                         "account_membership_verified": False if category=='free_episodes' else library.get('membership_verified') is True})
            for item in library["series"]:
                key = item.get("source_series_id")
                if observed_id(key):
                    candidates[key] = item
            report["pending_series_ids"] = [key for key in candidates if key not in previous]
            save()
        report["series"] = [previous[key] for key in candidates if key in previous]
        save()
        for index, key in enumerate(candidates, 1):
            if key in previous:
                print(f"Discovery: reused dated observation for series {key}.", flush=True)
                continue
            print(f"Discovery: checking series {key} ({index}/{len(candidates)}).", flush=True)
            result = summarize_series(adapter.inspect_series(key, strict_order=False))
            if result["source_series_id"] != key:
                raise ArchiveError("Discovery series response does not match the observed ID.")
            report["series"].append(result)
            report["pending_series_ids"].remove(key)
            save()
        report.update(state="finished", scanned_series_count=len(report["series"]),
                      scanned_episode_count=sum(s["episode_count"] for s in report["series"]),
                      free_episode_count=sum(s["free_episode_count"] for s in report["series"]),
                      unlocked_episode_count=sum(s["unlocked_episode_count"] for s in report["series"]),
                      cached_series_count=sum(s["from_previous_scan"] for s in report["series"]))
        save()
        return report
    except BaseException:
        report["state"] = "paused"
        save()
        raise
