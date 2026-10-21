"""The bridge envelope contract: shapes, unknown ops and public-safe failures."""
import importlib.util
import json
import os
import unittest
from pathlib import Path

_spec = importlib.util.spec_from_file_location(
    "app_bridge", Path(__file__).resolve().parent.parent / "android/app/src/main/python/app_bridge.py")
app_bridge = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(app_bridge)


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

    def test_scaffold_ops_answer_not_implemented_with_public_safe_words(self):
        value = self.call('{"v":1,"op":"enqueue_sync","args":{}}')
        self.assertFalse(value["ok"])
        self.assertEqual(value["code"], "not_implemented")
        self.assertNotIn("/", value["message"])


class BridgeLifecycleTests(unittest.TestCase):
    """The lifecycle ops against a real throwaway workspace (no network is touched)."""

    def setUp(self):
        import tempfile
        self._previous = Path.cwd()
        self._tmp = tempfile.TemporaryDirectory(prefix="bridge-workspace-")
        self.addCleanup(self._cleanup)
        self.root = Path(self._tmp.name)
        app_bridge._workspace["root"] = None
        app_bridge._seams.update(cookie_source=None, cookie_sink=None)

    def _cleanup(self):
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
        self.assertEqual(value["code"], "refused")

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

    def test_go_and_pause_answer_through_the_envelope(self):
        self.call("prepare_workspace", {"root": str(self.root)})
        self.assertTrue(self.call("go", {"root": str(self.root)})["ok"])
        self.assertTrue(self.call("pause", {"root": str(self.root)})["ok"])

    def test_path_arguments_are_validated_without_detail(self):
        for bad in ({}, {"root": "Library"}, {"root": None}, {"session_root": "/tmp/a\0b"}):
            with self.subTest(bad):
                value = self.call("prepare_workspace", bad)
                self.assertFalse(value["ok"])
                self.assertEqual(value["code"], "bad_request")

    def test_cookie_seams_must_be_callables(self):
        with self.assertRaises(Exception):
            app_bridge.set_cookie_seams("not callable")

    def test_the_kotlin_cookie_store_object_is_accepted_as_both_seams(self):
        class Store:
            def cookie_header(self, url):
                return "session=1"

            def set_session_cookies(self, url, values):
                return True

        app_bridge.set_cookie_seams(Store(), Store())
        source, sink = app_bridge._seams["cookie_source"], app_bridge._seams["cookie_sink"]
        self.assertEqual(source("https://tapas.io/x"), "session=1")
        sink("https://tapas.io/x", ["a=1"])  # A quiet refusal path, not a crash.

    def test_an_object_without_store_methods_is_refused(self):
        with self.assertRaises(Exception):
            app_bridge.set_cookie_seams(object())

