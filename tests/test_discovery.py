import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from support import PRIVATE_FILE
from comic_archive.discovery import discover_available
from comic_archive.models import ArchiveError


class DiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.work = tempfile.TemporaryDirectory()
        self.addCleanup(self.work.cleanup)
        self.report = Path(self.work.name) / "discovery.json"
        self.adapter = Mock()

        def library(category):
            keys = {"subscribed": ["42"], "recent": ["42", "43"], "updated": ["43"]}[category]
            return {"authenticated": True, "membership_verified": True, "scope": category,
                    "complete": False, "series": [{"source_series_id": key, "title": "S"} for key in keys]}

        def series(key, strict_order=False):
            return {"authenticated": True, "complete": True, "source_series_id": key, "title": "S",
                    "episode_count": 4, "account_email": "never export", "episodes": [
                        {"id": "81", "title": "Free", "order": 1, "access_state": "tapas_free", "cookie": "never export"},
                        {"id": "82", "title": "Unlocked", "order": 2, "access_state": "tapas_unlocked"},
                        {"id": "83", "title": "WUF", "order": 3, "access_state": "unavailable_or_unverified"},
                        {"id": "84", "title": "Preview", "order": 4, "access_state": "reader_confirmed"}]}

        self.adapter.inspect_library.side_effect = library
        self.adapter.inspect_series.side_effect = series

    @patch("builtins.print")
    def test_reading_tabs_deduplicate_and_export_only_observed_access(self, output):
        result = discover_available(self.adapter, self.report)
        self.assertEqual([call.kwargs["category"] for call in self.adapter.inspect_library.call_args_list],
                         ["subscribed", "recent", "updated"])
        self.assertEqual([call.args[0] for call in self.adapter.inspect_series.call_args_list], ["42", "43"])
        self.assertEqual(result["scanned_episode_count"], 8)
        self.assertEqual(result["free_episode_count"], 2)
        self.assertEqual(result["unlocked_episode_count"], 2)
        self.assertFalse(result["complete"])
        self.assertEqual(result["state"], "finished")
        self.assertEqual([e["id"] for e in result["series"][0]["accessible_episodes"]], ["81", "82"])
        self.assertEqual(self.report.stat().st_mode & 0o777, PRIVATE_FILE)
        self.assertNotIn("never export", self.report.read_text())
        self.assertEqual({call[0] for call in self.adapter.mock_calls}, {"inspect_library", "inspect_series"})

    @patch('builtins.print')
    def test_explicit_free_view_is_a_candidate_source_without_account_membership_claim(self,output):
        self.adapter.inspect_library.side_effect=None
        self.adapter.inspect_library.return_value={'authenticated':True,'membership_verified':True,
            'account_membership_verified':False,'scope':'free_episodes','complete':False,
            'series':[{'source_series_id':'42','title':'S'}]}
        result=discover_available(self.adapter,self.report,categories=['free_episodes'])
        self.assertFalse(result['complete']);self.assertFalse(result['categories'][0]['account_membership_verified'])
        self.assertEqual(result['free_episode_count'],1);self.assertEqual(result['unlocked_episode_count'],1)
        self.adapter.inspect_series.assert_called_once_with('42',strict_order=False)

    @patch("builtins.print")
    def test_interruption_saves_completed_observations_and_resume_skips_them(self, output):
        inspect = self.adapter.inspect_series.side_effect
        self.adapter.inspect_series.side_effect = lambda key, strict_order=False: inspect(key) if key == "42" else (_ for _ in ()).throw(KeyboardInterrupt())
        with self.assertRaises(KeyboardInterrupt):
            discover_available(self.adapter, self.report)
        saved = json.loads(self.report.read_text())
        self.assertEqual(saved["state"], "paused")
        self.assertEqual(saved["pending_series_ids"], ["43"])
        checked_at = saved["series"][0]["checked_at"]
        self.adapter.reset_mock()
        self.adapter.inspect_series.side_effect = inspect
        result = discover_available(self.adapter, self.report, resume=True)
        self.adapter.inspect_series.assert_called_once_with("43", strict_order=False)
        self.assertEqual(result["cached_series_count"], 1)
        self.assertEqual(result["series"][0]["checked_at"], checked_at)

    @patch("builtins.print")
    def test_default_scan_rechecks_access_and_changed_accounts(self, output):
        discover_available(self.adapter, self.report)
        self.adapter.reset_mock()
        discover_available(self.adapter, self.report)
        self.assertEqual(self.adapter.inspect_series.call_count, 2)

    @patch("builtins.print")
    def test_available_special_episode_with_unknown_order_remains_in_report(self, output):
        inspect = self.adapter.inspect_series.side_effect
        def special(key, strict_order=False):
            data = inspect(key)
            data["episodes"][1].update(order=None, source_scene=0)
            return data
        self.adapter.inspect_series.side_effect = special
        result = discover_available(self.adapter, self.report)
        self.assertEqual(result["unlocked_episode_count"], 2)
        self.assertFalse(result["series"][0]["ordering_verified"])
        episode = next(e for e in result["series"][0]["accessible_episodes"] if e["id"] == "82")
        self.assertIsNone(episode["order"])
        self.assertEqual(episode["source_scene"], 0)

    @patch("builtins.print")
    def test_resume_drops_unknown_properties_and_does_not_replay_saved_urls(self, output):
        discover_available(self.adapter, self.report)
        old = json.loads(self.report.read_text())
        old["series"][0]["authorization"] = "never export"
        old["series"][0]["accessible_episodes"][0]["source_url"] = "https://other.example/unlock?secret=never export"
        self.report.write_text(json.dumps(old))
        self.adapter.reset_mock()
        result = discover_available(self.adapter, self.report, resume=True)
        self.adapter.inspect_series.assert_not_called()
        self.assertNotIn("never export", self.report.read_text())
        self.assertEqual(result["series"][0]["accessible_episodes"][0]["source_url"], "https://tapas.io/episode/81")

    @patch("builtins.print")
    def test_authentication_scope_or_response_mismatch_stops_before_further_requests(self, output):
        self.adapter.inspect_library.return_value = {}
        self.adapter.inspect_library.side_effect = None
        with self.assertRaises(ArchiveError):
            discover_available(self.adapter, self.report)
        self.adapter.inspect_series.assert_not_called()
        self.assertEqual(json.loads(self.report.read_text())["state"], "paused")

    @patch("builtins.print")
    def test_failed_first_library_refresh_preserves_resume_observations(self, output):
        discover_available(self.adapter, self.report)
        self.adapter.inspect_library.side_effect = ArchiveError("Login needed")
        with self.assertRaises(ArchiveError):
            discover_available(self.adapter, self.report, resume=True)
        self.assertEqual(len(json.loads(self.report.read_text())["series"]), 2)

    def test_invalid_categories_and_nonboolean_resume_stop_before_requests(self):
        for categories in (["purchased"], ["recent", "recent"], [], "recent", [True]):
            with self.assertRaises(ArchiveError):
                discover_available(self.adapter, self.report, categories)
        with self.assertRaises(ArchiveError):
            discover_available(self.adapter, self.report, resume=1)
        self.assertEqual(self.adapter.mock_calls, [])


    @patch("builtins.print")
    def test_inconsistent_series_responses_pause_the_report_without_recording_them(self, output):
        inspect = self.adapter.inspect_series.side_effect
        def episodes(change):
            def response(key, strict_order=False):
                data = inspect(key)
                data = change(data) or data
                return data
            return response
        def replace_episode(**changes):
            return lambda data: data["episodes"].__setitem__(1, data["episodes"][1] | changes)
        cases = {
            "episode count differs from the list": lambda data: data.update(episode_count=5),
            "episode count is not an integer": lambda data: data.update(episode_count="4"),
            "episodes are not a list": lambda data: data.update(episodes=None, episode_count=0),
            "repeated episode": replace_episode(id="81"),
            "episode id is not numeric": replace_episode(id="abc"),
            "episode order is zero": replace_episode(order=0),
            "episode order is not an integer": replace_episode(order=True),
            "episode entry is not an object": lambda data: data["episodes"].__setitem__(1, "82"),
            "another series came back": lambda data: data.update(source_series_id="99"),
        }
        for label, change in cases.items():
            with self.subTest(label):
                self.report.unlink(missing_ok=True)
                self.adapter.inspect_series.side_effect = episodes(change)
                with self.assertRaises(ArchiveError):
                    discover_available(self.adapter, self.report)
                saved = json.loads(self.report.read_text())
                self.assertEqual(saved["state"], "paused")
                self.assertEqual(saved["series"], [])
                self.assertEqual(saved["pending_series_ids"], ["42", "43"])

    @patch("builtins.print")
    def test_damaged_or_foreign_report_cannot_be_resumed_and_is_left_untouched(self, output):
        discover_available(self.adapter, self.report)
        good = json.loads(self.report.read_text())
        def series(**changes):
            return lambda old: old["series"].__setitem__(0, old["series"][0] | changes)
        def row(**changes):
            def edit(old):
                rows = old["series"][0]["accessible_episodes"]
                rows[0] = rows[0] | changes
            return edit
        edits = {
            "another schema": lambda old: old.update(schema_version=2),
            "another source": lambda old: old.update(source="other"),
            "no series list": lambda old: old.pop("series"),
            "series is not a list": lambda old: old.update(series=5),
            "series entry is not an object": lambda old: old["series"].__setitem__(0, "42"),
            "series id is not numeric": series(source_series_id="abc"),
            "series id is zero": series(source_series_id="0"),
            "episode rows are not a list": series(accessible_episodes="81"),
            "episode count is below the rows": series(episode_count=1),
            "enumeration was incomplete": series(enumeration_complete=False),
            "title is not text": series(title=None),
            "check time is not text": series(checked_at=5),
            "row is not accessible": row(access_state="unavailable_or_unverified"),
            "row id is not numeric": row(id="x"),
            "row order is zero": row(order=0),
            "row order is not an integer": row(order=True),
            "row has no title": row(title=None),
            "row is not an object": lambda old: old["series"][0]["accessible_episodes"].__setitem__(0, "81"),
            "repeated row": lambda old: old["series"][0]["accessible_episodes"].append(
                old["series"][0]["accessible_episodes"][0]),
        }
        for label, edit in edits.items():
            with self.subTest(label):
                damaged = json.loads(json.dumps(good))
                edit(damaged)
                self.report.write_text(json.dumps(damaged))
                before = self.report.read_bytes()
                self.adapter.reset_mock()
                with self.assertRaises(ArchiveError):
                    discover_available(self.adapter, self.report, resume=True)
                self.assertEqual(self.adapter.mock_calls, [])
                self.assertEqual(self.report.read_bytes(), before)
        for label, content in (("not JSON", "{broken"), ("not an object", "[]")):
            with self.subTest(label):
                self.report.write_text(content)
                with self.assertRaises(ArchiveError):
                    discover_available(self.adapter, self.report, resume=True)
                self.assertEqual(self.report.read_text(), content)
        with self.report.open("wb") as stream:
            stream.truncate(16 * 1024 * 1024 + 1)
        with self.subTest("too large"), self.assertRaises(ArchiveError):
            discover_available(self.adapter, self.report, resume=True)
        self.assertEqual(self.adapter.mock_calls, [])

    @patch("builtins.print")
    def test_symlinked_report_is_refused_and_its_target_kept(self, output):
        target = Path(self.work.name) / "original.json"
        target.write_text("retain")
        self.report.symlink_to(target)
        for resume in (False, True):
            with self.subTest(resume=resume), self.assertRaises(ArchiveError):
                discover_available(self.adapter, self.report, resume=resume)
        self.assertEqual(target.read_text(), "retain")
        self.assertEqual(self.adapter.mock_calls, [])

    @patch("builtins.print")
    def test_listed_series_without_a_usable_id_are_counted_but_never_inspected(self, output):
        self.adapter.inspect_library.side_effect = None
        self.adapter.inspect_library.return_value = {
            "authenticated": True, "membership_verified": True, "scope": "subscribed", "complete": True,
            "series": [{"source_series_id": "abc"}, {"source_series_id": 42}, {"title": "no id"},
                       {"source_series_id": "0"}, {"source_series_id": "42", "title": "S"}]}
        result = discover_available(self.adapter, self.report, categories=["subscribed"])
        self.adapter.inspect_series.assert_called_once_with("42", strict_order=False)
        self.assertEqual(result["categories"][0]["visible_series_count"], 5)
        self.assertEqual([item["source_series_id"] for item in result["series"]], ["42"])


if __name__ == "__main__":
    unittest.main()
