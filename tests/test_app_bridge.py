"""The bridge envelope contract: shapes, unknown ops, the frozen cookie-store ABI and the
lifecycle/listing ops against real throwaway workspaces (no network is touched)."""
import hashlib
import importlib.util
import json
import os
import unittest
from pathlib import Path

_spec = importlib.util.spec_from_file_location(
    "app_bridge", Path(__file__).resolve().parent.parent / "android/app/src/main/python/app_bridge.py")
app_bridge = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(app_bridge)


class PortableCatalogBridgeTests(unittest.TestCase):
    def test_portable_catalog_works_signed_out_without_creating_a_workspace_or_starting_a_worker(self):
        import tempfile
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'catalog.json'
            path.write_text(json.dumps({'v': 1, 'folders': [{'name': 'Exported series', 'files': [
                {'key': 'a' * 64, 'name': 'One.epub', 'bytes': 100, 'modified': None}]}]}))
            before = path.read_bytes()
            with patch.object(app_bridge, '_catalog_registry', side_effect=AssertionError('No working library')):
                value = json.loads(app_bridge.dispatch(json.dumps({'v': 1, 'op': 'portable_catalog',
                    'args': {'manifest_path': str(path), 'options': {'kind': 'episodes'}}})))
            self.assertTrue(value['ok'])
            row = value['result']['rows'][0]
            self.assertEqual(row['open'], 'a' * 64)
            self.assertEqual(row['type'], 'Novel')
            self.assertEqual(row['storage'], 'Opened folder')
            self.assertFalse(row['b2_copy'])
            self.assertIsNone(row['saved_epoch'])
            self.assertEqual(path.read_bytes(), before)
            self.assertEqual([p.name for p in Path(directory).iterdir()], ['catalog.json'])

    def test_missing_malformed_or_private_manifest_never_becomes_an_empty_success(self):
        import tempfile
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'catalog.json'
            for body in [None, '[]', '{"cookies":"PRIVATE"}', '{"v":1,"folders":"bad"}']:
                if body is not None:
                    path.write_text(body)
                value = json.loads(app_bridge.dispatch(json.dumps({'v': 1, 'op': 'portable_catalog',
                    'args': {'manifest_path': str(path)}})))
                self.assertFalse(value['ok'])
                self.assertNotIn('PRIVATE', json.dumps(value))


class BridgeEnvelopeTests(unittest.TestCase):
    def call(self, request):
        return json.loads(app_bridge.dispatch(request))

    def test_ping_answers_the_envelope_with_a_result(self):
        value = self.call('{"v":1,"op":"ping","args":{}}')
        self.assertTrue(value["ok"])
        self.assertEqual(value["result"]["bridge_version"], 1)
        self.assertIn("python", value["result"])

    def test_unknown_and_malformed_requests_are_refused_without_detail(self):
        for bad in ('{"v":2,"op":"ping","args":{}}', 'not json', '{"v":1,"op":"nope","args":{}}',
                    '{"v":1,"op":"ping","args":7}', '{"v":1,"op":"ping"}'):
            with self.subTest(bad):
                value = self.call(bad)
                self.assertFalse(value["ok"])
                self.assertEqual(value["v"], 1)
                self.assertIn(value["code"], {"bad_request", "unknown_op"})

    def test_non_text_requests_get_the_same_refusal(self):
        value = json.loads(app_bridge.dispatch({"v": 1}))
        self.assertFalse(value["ok"])
        self.assertEqual(value["code"], "bad_request")

    def test_every_op_in_the_envelope_vocabulary_answers(self):
        # Every §2 op (and the flagged native-line extensions) is a real op now: each one
        # answers its own envelope, never the scaffold's not_implemented.
        for op in ("enqueue_sync", "library_listing", "export_manifest", "export_status",
                   "signin_probe", "signin_restore", "signout_record", "settings", "configure_settings", "file_catalog", "file_open",
                   "cloud_status", "cloud_action"):
            with self.subTest(op):
                value = self.call(json.dumps({"v": 1, "op": op, "args": {}}))
                self.assertFalse(value["ok"])
                self.assertNotEqual(value["code"], "not_implemented")
                self.assertNotIn("/", value["message"])


class BridgeLifecycleTests(unittest.TestCase):
    """The lifecycle ops against a real throwaway workspace (no network is touched)."""

    def setUp(self):
        import tempfile
        self._previous = Path.cwd()
        self._tmp = tempfile.TemporaryDirectory(prefix="bridge-workspace-")
        self.addCleanup(self._cleanup)
        self.root = Path(self._tmp.name)
        app_bridge._workspace.update(root=None, base=None, site_key="tapas")
        app_bridge._signin_attempts.clear()
        app_bridge._seams.update(store=None, cookie_source=None, cookie_sink=None)
        app_bridge._browser.update(factory=None)
        app_bridge._export_cache.update(root=None, facts=None, summary=None)
        app_bridge._catalog_cache.update(root=None, registry=None)

    def _cleanup(self):
        from comic_archive import network_android
        network_android.configure(None)  # Process-global probe state never leaks to later tests.
        app_bridge._browser.update(factory=None)
        os.chdir(self._previous)
        self._tmp.cleanup()

    def call(self, op, args):
        return json.loads(app_bridge.dispatch(json.dumps({"v": 1, "op": op, "args": args})))

    def test_prepare_workspace_makes_folders_holds_for_go_and_sets_the_cwd(self):
        value = self.call("prepare_workspace", {"root": str(self.root)})
        self.assertTrue(value["ok"], value)
        self.assertFalse(value["result"]["held"])
        self.assertEqual(Path.cwd(), self.root)
        self.assertTrue((self.root / "Library").is_dir())
        self.assertTrue((self.root / ".comic-session").is_dir())

    def test_prepare_workspace_refuses_a_symlinked_root(self):
        link = Path(self._tmp.name) / "link"
        os.symlink(self.root / "real", link)
        value = self.call("prepare_workspace", {"root": str(link)})
        self.assertFalse(value["ok"])
        # Refused at the bridge boundary (every path arg is), before the core sees it.
        self.assertEqual(value["code"], "bad_request")

    def test_worker_status_reads_the_protocol_file(self):
        self.call("prepare_workspace", {"root": str(self.root)})
        value = self.call("worker_status", {"session_root": str(self.root / ".comic-session")})
        self.assertTrue(value["ok"], value)
        self.assertFalse(value["result"]["running"])

    def test_worker_start_needs_the_cookie_seams_before_anything_else(self):
        self.call("prepare_workspace", {"root": str(self.root)})
        value = self.call("worker_start", {"session_root": str(self.root / ".comic-session"),
                                           "library_root": str(self.root / "Library"),
                                           "network_status_path": str(self.root / "net.json")})
        self.assertFalse(value["ok"])
        self.assertEqual(value["code"], "cookie_seams_missing")

    def test_worker_start_needs_a_whole_sign_in_record(self):
        app_bridge.set_cookie_seams(lambda url: "", lambda url, values: True)
        self.call("prepare_workspace", {"root": str(self.root)})
        value = self.call("worker_start", {"session_root": str(self.root / ".comic-session"),
                                           "library_root": str(self.root / "Library"),
                                           "network_status_path": str(self.root / "net.json")})
        self.assertFalse(value["ok"])
        self.assertEqual(value["code"], "refused")
        self.assertIn("Sign in", value["message"])

    def test_worker_stop_answers_through_the_envelope_when_no_worker_runs(self):
        self.call("prepare_workspace", {"root": str(self.root)})
        value = self.call("worker_stop", {"session_root": str(self.root / ".comic-session"),
                                          "wait_seconds": 0})
        self.assertTrue(value["ok"], value)
        self.assertFalse(value["result"]["running"])

    def test_go_needs_the_downloader_and_pause_answers_quietly(self):
        self.call("prepare_workspace", {"root": str(self.root)})
        # Go before any worker runs is the core's own refusal: consecutive imports need
        # the downloader's queue first. The app wires worker_start before go, so this is
        # exactly the envelope a premature Go must produce.
        value = self.call("go", {"root": str(self.root)})
        self.assertFalse(value["ok"])
        self.assertEqual(value["code"], "refused")
        self.assertTrue(self.call("pause", {"root": str(self.root)})["ok"])

    def test_download_status_reports_durable_plan_separately_from_worker_liveness(self):
        self.call("prepare_workspace", {"root": str(self.root)})
        queue = self.root / ".comic-session/worker"
        (queue / "requests").mkdir(parents=True)
        (queue / "responses").mkdir()
        self.assertTrue(self.call("go", {"root": str(self.root)})["ok"])
        value = self.call("download_status", {"root": str(self.root)})
        self.assertTrue(value["ok"])
        self.assertEqual(set(value["result"]), {"account", "worker", "counts", "progress"})
        self.assertEqual(value["result"]["account"]["status"], "running")
        self.assertEqual(value["result"]["account"]["phase"], "lists")
        self.assertFalse(value["result"]["worker"]["running"])
        self.assertTrue(self.call("pause", {"root": str(self.root)})["ok"])
        value = self.call("download_status", {"root": str(self.root)})
        self.assertEqual(value["result"]["account"]["status"], "stopped")

    def test_path_arguments_are_validated_without_detail(self):
        for bad in ({}, {"root": "Library"}, {"root": None}, {"session_root": "/tmp/a\0b"}):
            with self.subTest(bad):
                value = self.call("prepare_workspace", bad)
                self.assertFalse(value["ok"])
                self.assertEqual(value["code"], "bad_request")

    def test_cookie_seams_must_be_callables(self):
        with self.assertRaises(app_bridge._BridgeError):
            app_bridge.set_cookie_seams("not callable")

    def test_the_kotlin_cookie_store_registers_by_its_frozen_names(self):
        # Chaquopy performs no snake_case conversion, so the store Python sees carries its
        # frozen camelCase methods; registration keeps the store itself (the adapter wraps
        # it at use time) and the worker then fails on the missing sign-in, not the seams.
        class Store:
            def headerFor(self, url):
                return "session=1"

            def apply(self, url, values):
                return None

        store = Store()
        app_bridge.set_cookie_seams(store)
        self.assertIs(app_bridge._seams["store"], store)
        self.assertIsNone(app_bridge._seams["cookie_source"])
        self.call("prepare_workspace", {"root": str(self.root)})
        value = self.call("worker_start", {"session_root": str(self.root / ".comic-session"),
                                           "library_root": str(self.root / "Library"),
                                           "network_status_path": str(self.root / "net.json")})
        self.assertFalse(value["ok"])
        self.assertEqual(value["code"], "refused")
        self.assertIn("Sign in", value["message"])

    def test_a_store_without_the_frozen_methods_is_refused_not_swallowed(self):
        class HalfStore:
            def headerFor(self, url):
                return None

        with self.assertRaises(app_bridge._BridgeError):
            app_bridge.set_cookie_seams(HalfStore())
        with self.assertRaises(app_bridge._BridgeError):
            app_bridge.set_cookie_seams(object())

    def test_signin_probe_without_seams_is_an_honest_missing(self):
        self.call("prepare_workspace", {"root": str(self.root)})
        value = self.call("signin_probe", {"session_root": str(self.root / ".comic-session")})
        self.assertFalse(value["ok"])
        self.assertEqual(value["code"], "cookie_seams_missing")

    def test_restore_without_saved_session_issues_no_account_probe(self):
        from unittest.mock import patch
        with patch("comic_archive.android_adapter.signed_in_probe") as probe:
            value = self.call("signin_restore", {"session_root": str(self.root / ".comic-session")})
        self.assertTrue(value["ok"], value)
        self.assertFalse(value["result"]["signed_in"])
        probe.assert_not_called()

    def test_restore_verifies_saved_session_without_starting_worker_or_plan(self):
        from unittest.mock import patch
        from comic_archive import android_session, worker_host
        session = self.root / ".comic-session"
        android_session.mark_signed_in(session)
        app_bridge.set_cookie_seams(lambda url: "", lambda url, values: True)
        (self.root / "net.json").write_text('{"connection":"wifi","metered":false}')
        verified = {"signed_in": True, "verified": True, "reason": None}
        with patch("comic_archive.android_adapter.signed_in_probe", return_value=verified) as probe:
            value = self.call("signin_restore", {"session_root": str(session)})
        self.assertTrue(value["ok"], value)
        self.assertTrue(value["result"]["signed_in"])
        probe.assert_called_once()
        self.assertFalse(worker_host.status(session)["running"])
        self.assertFalse((session / "worker/account-export.json").exists())

    def test_restore_expired_saved_session_does_not_authenticate(self):
        from unittest.mock import patch
        from comic_archive import android_session
        session = self.root / ".comic-session"
        android_session.mark_signed_in(session)
        app_bridge.set_cookie_seams(lambda url: "", lambda url, values: True)
        (self.root / "net.json").write_text('{"connection":"wifi","metered":false}')
        refused = {"signed_in": False, "verified": False, "reason": "not_signed_in"}
        with patch("comic_archive.android_adapter.signed_in_probe", return_value=refused):
            value = self.call("signin_restore", {"session_root": str(session)})
        self.assertTrue(value["ok"], value)
        self.assertFalse(value["result"]["signed_in"])

    def test_provider_restore_uses_its_recorded_identity_and_never_starts_work(self):
        from unittest.mock import patch
        from comic_archive import android_session, sites, worker_host
        def source(url):
            return "sid=FIXTURE_SESSION"
        app_bridge.set_cookie_seams(source)
        verified = {"signed_in": True, "verified": True, "reason": None}
        for key in ("webtoon", "tappytoon"):
            site = sites.named(key)
            root = sites.folder(self.root, site)
            session = root / ".comic-session"
            android_session.mark_signed_in(session, site=site)
            (root / "net.json").write_text('{"connection":"wifi","metered":false}')
            with self.subTest(key=key), patch("comic_archive.android_readers.signed_in_probe",
                                             return_value=verified) as probe:
                value = self.call("signin_restore", {"session_root": str(session)})
            self.assertTrue(value["ok"], value)
            self.assertEqual(value["result"], verified)
            probe.assert_called_once()
            self.assertEqual(probe.call_args.args, (key, source, None))
            self.assertIsNotNone(probe.call_args.kwargs['network_guard'])
            self.assertEqual(android_session.signed_in_site(session), site)
            self.assertFalse(worker_host.status(session)["running"])
            self.assertFalse((session / "worker/account-export.json").exists())

    def test_probe_refuses_unknown_site_before_verification_or_a_record_write(self):
        from unittest.mock import patch
        session = self.root / ".comic-session"
        app_bridge.set_cookie_seams(lambda url: "sid=FIXTURE_SESSION")
        with patch("comic_archive.android_readers.signed_in_probe") as probe:
            value = self.call("signin_probe", {"session_root": str(session), "site_key": "unknown"})
        self.assertFalse(value["ok"])
        self.assertEqual(value["code"], "refused")
        self.assertFalse((session / "android.json").exists())
        probe.assert_not_called()

    def test_automatic_restore_honors_metered_choice_before_any_account_request(self):
        from unittest.mock import patch
        from comic_archive import android_session
        self.call("prepare_workspace", {"root": str(self.root)})
        session = self.root / ".comic-session"
        android_session.mark_signed_in(session)
        app_bridge.set_cookie_seams(lambda url: "", lambda url, values: True)
        (self.root / "net.json").write_text('{"connection":"cellular","metered":true}')
        verified = {"signed_in": True, "verified": True, "reason": None}
        with patch("comic_archive.android_adapter.signed_in_probe", return_value=verified) as probe:
            held = self.call("signin_restore", {"session_root": str(session)})
            self.assertTrue(held["ok"], held)
            self.assertFalse(held["result"]["signed_in"])
            self.assertEqual(held["result"]["reason"], "network_held")
            probe.assert_not_called()
            self.assertTrue(self.call("configure_settings", {"root": str(self.root), "wifi_only": False})["ok"])
            restored = self.call("signin_restore", {"session_root": str(session)})
        self.assertTrue(restored["result"]["signed_in"])
        probe.assert_called_once()
        self.assertFalse((session / "worker/account-export.json").exists())

    def test_restore_refuses_torn_saved_record_before_network(self):
        from unittest.mock import patch
        session = self.root / ".comic-session"
        session.mkdir()
        (session / "android.json").write_text('{"schema_version":1}')
        with patch("comic_archive.android_adapter.signed_in_probe") as probe:
            value = self.call("signin_restore", {"session_root": str(session)})
        self.assertFalse(value["ok"])
        probe.assert_not_called()

    def test_download_settings_use_core_catalog_and_unmetered_default_without_go(self):
        from comic_archive import account_ui, dashboard_model, worker_host
        from comic_archive.workspace import Workspace
        self.call("prepare_workspace", {"root": str(self.root)})
        value = self.call("settings", {"root": str(self.root)})
        self.assertTrue(value["ok"], value)
        result = value["result"]
        self.assertTrue(result["wifi_only"])
        self.assertFalse(result["allowed"])
        self.assertEqual(result["connection"], "unknown")
        self.assertEqual(result["speed"], "medium")
        self.assertEqual(result["speeds"], [
            {"key": key, "label": dashboard_model.SPEED_NAMES[key],
             "available": policy["speed"] == "normal"}
            for key, policy in account_ui.SPEEDS.items()])
        workspace = Workspace(id="default", root=self.root)
        self.assertEqual(account_ui.speed(workspace), account_ui.DEFAULT_SPEED)
        self.assertFalse(worker_host.status(workspace.session)["running"])
        self.assertFalse((workspace.session / "worker/account-export.json").exists())

    def test_native_fast_availability_preserves_a_stored_unavailable_choice_without_go(self):
        from test_android_browser import Factory
        self.call('prepare_workspace', {'root': str(self.root)})
        factory = Factory()
        app_bridge.set_browser_seam(factory)
        selected = self.call('configure_settings', {'root': str(self.root), 'speed': 'fast'})
        self.assertTrue(selected['ok'], selected)
        factory.enabled = False
        settings = self.call('settings', {'root': str(self.root)})['result']
        self.assertEqual(settings['speed'], 'fast')
        self.assertFalse(next(item['available'] for item in settings['speeds'] if item['key'] == 'fast'))
        self.assertFalse(self.call('go', {'root': str(self.root)})['ok'])
        self.assertEqual(factory.opens, [])

    def test_a_delayed_foreign_workspace_setting_is_refused_before_any_write(self):
        from comic_archive import account_ui, network
        from comic_archive.workspace import Workspace
        self.call('prepare_workspace', {'root': str(self.root)})
        other = self.root / 'Webtoon'
        other.mkdir()
        value = self.call('configure_settings', {'root': str(other), 'speed': 'slow', 'wifi_only': False})
        self.assertFalse(value['ok'])
        workspace = Workspace('other', other)
        self.assertFalse((workspace.session / account_ui.SPEED_FILE).exists())
        self.assertFalse(workspace.session.exists())
        self.assertTrue(network.policy(self.root / 'Library')['wifi_only'])

    def test_settings_consent_and_speed_persist_without_resuming_a_plan(self):
        from comic_archive import account_ui, network, worker_host
        from comic_archive.workspace import Workspace
        self.call("prepare_workspace", {"root": str(self.root)})
        (self.root / "net.json").write_text('{"connection":"cellular","metered":true}')
        before = self.call("settings", {"root": str(self.root)})["result"]
        self.assertFalse(before["allowed"])
        changed = self.call("configure_settings", {"root": str(self.root), "wifi_only": False, "speed": "slow"})
        self.assertTrue(changed["ok"], changed)
        workspace = Workspace(id="default", root=self.root)
        self.assertTrue(changed["result"]["allowed"])
        self.assertFalse(network.policy(workspace.library)["wifi_only"])
        self.assertEqual(account_ui.speed(workspace), "slow")
        self.call("prepare_workspace", {"root": str(self.root)})
        reopened = self.call("settings", {"root": str(self.root)})["result"]
        self.assertFalse(reopened["wifi_only"])
        self.assertEqual(reopened["speed"], "slow")
        self.assertFalse(worker_host.status(workspace.session)["running"])
        self.assertFalse((workspace.session / "worker/account-export.json").exists())
        self.assertTrue(self.call("configure_settings", {"root": str(self.root), "wifi_only": True})["ok"])
        self.assertFalse(self.call("settings", {"root": str(self.root)})["result"]["allowed"])

    def test_settings_refuse_unsupported_or_implicit_choices_before_any_write(self):
        from comic_archive import account_ui, network
        from comic_archive.workspace import Workspace
        self.call("prepare_workspace", {"root": str(self.root)})
        for bad in ({}, {"wifi_only": "false"}, {"wifi_only": 0}, {"speed": "fast", "wifi_only": False},
                    {"speed": []}, {"speed": "missing"}, {"other": True}):
            with self.subTest(bad):
                result = self.call("configure_settings", {"root": str(self.root)} | bad)
                self.assertFalse(result["ok"])
                workspace = Workspace(id="default", root=self.root)
                self.assertTrue(network.policy(workspace.library)["wifi_only"])
                self.assertEqual(account_ui.speed(workspace), account_ui.DEFAULT_SPEED)

    def test_running_plan_refuses_speed_change_but_allows_explicit_network_choice(self):
        from comic_archive import account_ui
        from comic_archive.workspace import Workspace
        self.call("prepare_workspace", {"root": str(self.root)})
        queue = self.root / ".comic-session/worker"
        (queue / "requests").mkdir(parents=True)
        (queue / "responses").mkdir()
        self.assertTrue(self.call("go", {"root": str(self.root)})["ok"])
        before = account_ui.speed(Workspace(id="default", root=self.root))
        result = self.call("configure_settings", {"root": str(self.root), "speed": "slow", "wifi_only": False})
        self.assertFalse(result["ok"])
        self.assertEqual(account_ui.speed(Workspace(id="default", root=self.root)), before)
        result = self.call("configure_settings", {"root": str(self.root), "wifi_only": False})
        self.assertTrue(result["ok"], result)
        self.assertTrue(self.call("pause", {"root": str(self.root)})["ok"])

    def test_signout_record_forgets_the_written_sign_in(self):
        from comic_archive import android_session
        session = self.root / ".comic-session"
        session.mkdir()
        android_session.mark_signed_in(session)
        value = self.call("signout_record", {"session_root": str(session)})
        self.assertTrue(value["ok"], value)
        self.assertFalse(android_session.is_signed_in(session))

    def test_enqueue_sync_needs_the_sign_in_first(self):
        self.call("prepare_workspace", {"root": str(self.root)})
        value = self.call("enqueue_sync", {"session_root": str(self.root / ".comic-session")})
        self.assertFalse(value["ok"])
        self.assertEqual(value["code"], "refused")
        self.assertIn("Sign in", value["message"])

    def _export_library(self):
        comic = self.root / "Library" / "Sample Comic"
        comic.mkdir(parents=True)
        (comic / "series.json").write_text(json.dumps(
            {"source_series_id": "123", "title": "Sample Comic", "episode_count": 2}))
        (comic / "episode-1.cbz").write_bytes(b"data")
        (comic / "episode-1.json").write_text(json.dumps(
            {"source": "tapas", "source_series_id": "123", "source_episode_id": "456",
             "content_type": "comic", "format": "cbz", "title": "One"}))
        # Contract §5: tar.zst is not exportable. It still counts as saved for the listing,
        # and the sidecar-less cbz is offered nowhere at all.
        (comic / "episode-2.tar.zst").write_bytes(b"")
        (comic / "episode-2.json").write_text(json.dumps(
            {"source": "tapas", "source_series_id": "123", "source_episode_id": "789",
             "content_type": "comic", "format": "tar.zst", "title": "Two"}))
        (comic / "episode-3.cbz").write_bytes(b"no sidecar")
        novel = self.root / "Library" / "Sample Novel"
        novel.mkdir(parents=True)
        (novel / "series.json").write_text(json.dumps(
            {"source_series_id": "999", "title": "Sample Novel"}))
        (novel / "chapter-1.epub").write_bytes(b"novel")
        (novel / "chapter-1.json").write_text(json.dumps(
            {"source": "tapas", "source_series_id": "999", "source_episode_id": "1000",
             "content_type": "novel", "format": "epub", "title": "Chapter"}))

    def test_library_listing_offers_series_by_the_cores_rules(self):
        self._export_library()
        value = self.call("library_listing", {"root": str(self.root)})
        self.assertTrue(value["ok"], value)
        self.assertEqual(value["result"], [
            {"seriesId": "123", "title": "Sample Comic", "kind": "comic",
             "episodeCount": 2, "savedCount": 2},
            {"seriesId": "999", "title": "Sample Novel", "kind": "novels",
             "episodeCount": 1, "savedCount": 1}])

    def test_export_manifest_digests_the_originals_and_skips_tar_zst(self):
        self._export_library()
        value = self.call("export_manifest", {"root": str(self.root)})
        self.assertTrue(value["ok"], value)
        entries = value["result"]["entries"]
        self.assertEqual([entry["filename"] for entry in entries],
                         ["episode-1.cbz", "chapter-1.epub"])
        self.assertEqual({entry["kind"] for entry in entries}, {"CBZ", "EPUB"})
        comic = entries[0]
        self.assertEqual(comic["sha256"], hashlib.sha256(b"data").hexdigest())
        self.assertEqual(comic["bytes"], 4)
        self.assertEqual(comic["episodeId"], "456")
        self.assertEqual(comic["seriesId"], "123")
        self.assertEqual(comic["relpath"], "Sample Comic/episode-1.cbz")
        self.assertRegex(value["result"]["manifest_sha256"], r"^[0-9a-f]{64}$")

    def test_file_catalog_matches_the_desktop_catalog_and_core_filter_catalogs(self):
        from unittest.mock import patch
        from comic_archive import library_model
        from comic_archive.local_library import LOCAL_OWNER
        self._export_library()
        with patch("comic_archive.worker_host.start_in_process") as start:
            value = self.call("file_catalog", {"root": str(self.root), "options": {"kind": "episodes"}})
        self.assertTrue(value["ok"], value)
        page = value["result"]
        registry = app_bridge._catalog_registry(self.root)
        desktop = registry.saved_files(LOCAL_OWNER, registry.workspace_id, kind="episodes", sort="saved_desc")
        self.assertEqual([(r["filename"], r["open"]) for r in page["rows"]],
                         [(r["filename"], r["open"]) for r in library_model.rows(desktop, False)])
        self.assertEqual(page["total"], 3)
        self.assertEqual(page["kinds"], [{"key": k, "label": v} for k, v in library_model.KINDS])
        self.assertEqual(page["sorts"], [{"key": k, "label": v} for k, v in library_model.SORTS])
        self.assertEqual(page["page_sizes"], list(library_model.PAGE_SIZES))
        self.assertTrue(all("relpath" not in row for row in page["rows"]))
        self.assertTrue(all(not row["b2_copy"] for row in page["rows"]))
        start.assert_not_called()

    def test_catalog_identifies_current_and_previous_b2_copies_without_exposing_a_url_or_local_open(self):
        from comic_archive.local_library import LOCAL_OWNER
        self._export_library()
        registry = app_bridge._catalog_registry(self.root)
        desktop = registry.saved_files(LOCAL_OWNER, registry.workspace_id, kind="episodes", sort="saved_desc")
        for location in ("b2", "b2-0123456789abcdef"):
            with self.subTest(location=location):
                row = dict(desktop["files"][0], location=location, id=None)
                raw = dict(desktop, files=[row], total=1, offset=0)
                result = app_bridge._catalog_projection(raw, {"location": "all"})["rows"][0]
                self.assertTrue(result["b2_copy"])
                self.assertIsNone(result["open"])
                self.assertNotIn("url", result)
                self.assertNotIn("relpath", result)

    def test_catalog_search_types_paging_and_stable_ids_survive_reconstruction(self):
        self._export_library()
        result = self.call("file_catalog", {"root": str(self.root), "options": {
            "query": "ONE", "kind": "episodes", "limit": 1, "sort": "series"}})
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["result"]["total"], 1)
        row = result["result"]["rows"][0]
        self.assertEqual(row["filename"], "episode-1.cbz")
        app_bridge._catalog_cache.update(root=None, registry=None)
        again = self.call("file_catalog", {"root": str(self.root), "options": {"query": "ONE", "kind": "episodes"}})
        self.assertEqual(again["result"]["rows"][0]["open"], row["open"])
        data = self.call("file_catalog", {"root": str(self.root), "options": {"kind": "metadata", "limit": 1}})
        self.assertTrue(data["ok"], data)
        self.assertGreater(data["result"]["total"], 1)
        self.assertEqual(len(data["result"]["rows"]), 1)
        self.assertIsNone(data["result"]["rows"][0]["open"])
        second = self.call("file_catalog", {"root": str(self.root), "options": {"kind": "metadata", "limit": 1, "offset": 1}})
        self.assertNotEqual(data["result"]["rows"][0]["row_id"], second["result"]["rows"][0]["row_id"])

    def test_file_open_resolves_only_registered_local_archives_and_refuses_stale_or_metadata_ids(self):
        from unittest.mock import patch
        self._export_library()
        page = self.call("file_catalog", {"root": str(self.root), "options": {"kind": "all"}})["result"]
        comic = next(row for row in page["rows"] if row["filename"] == "episode-1.cbz")
        with patch("subprocess.Popen") as spawn:
            opened = self.call("file_open", {"root": str(self.root), "id": comic["open"]})
        self.assertTrue(opened["ok"], opened)
        self.assertEqual(opened["result"], {"relpath": "Sample Comic/episode-1.cbz", "kind": "cbz"})
        spawn.assert_not_called()
        metadata = next(row for row in page["rows"] if row["type"] == "Data file")
        refused = self.call("file_open", {"root": str(self.root), "id": metadata["row_id"]})
        self.assertFalse(refused["ok"])
        original = self.root / "Library/Sample Comic/episode-1.cbz"
        original.unlink()
        self.assertFalse(self.call("file_open", {"root": str(self.root), "id": comic["open"]})["ok"])
        original.symlink_to(self.root / "Library/Sample Novel/chapter-1.epub")
        self.assertFalse(self.call("file_open", {"root": str(self.root), "id": comic["open"]})["ok"])
        for bad in ("../private", "0" * 64, 123, None):
            self.assertFalse(self.call("file_open", {"root": str(self.root), "id": bad})["ok"])

    def test_catalog_refuses_implicit_or_unknown_filters(self):
        self._export_library()
        for options in ({"limit": True}, {"refresh": "1"}, {"query": []}, {"location": "private"},
                        {"kind": "private"}, {"sort": "missing"}, {"offset": -1}, {"limit": 201},
                        {"query": "x" * 201}, {"extra": "private"}):
            with self.subTest(options):
                self.assertFalse(self.call("file_catalog", {"root": str(self.root), "options": options})["ok"])

    def test_export_status_answers_the_summary_and_follows_changes(self):
        self._export_library()
        first = self.call("export_status", {"root": str(self.root)})
        self.assertTrue(first["ok"], first)
        self.assertEqual(first["result"]["entries"], 2)
        self.assertEqual(first["result"]["bytes"], 9)
        manifest = self.call("export_manifest", {"root": str(self.root)})["result"]
        self.assertEqual(first["result"]["manifest_sha256"], manifest["manifest_sha256"])
        extra = self.root / "Library" / "Sample Comic"
        (extra / "episode-4.cbz").write_bytes(b"more")
        (extra / "episode-4.json").write_text(json.dumps(
            {"source": "tapas", "source_series_id": "123", "source_episode_id": "457",
             "content_type": "comic", "format": "cbz", "title": "Four"}))
        second = self.call("export_status", {"root": str(self.root)})
        self.assertTrue(second["ok"], second)
        self.assertEqual(second["result"]["entries"], 3)

