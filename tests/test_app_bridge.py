"""The bridge envelope contract: shapes, unknown ops and public-safe failures."""
import importlib.util
import json
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
        value = self.call('{"v":1,"op":"worker_status","args":{}}')
        self.assertFalse(value["ok"])
        self.assertEqual(value["code"], "not_implemented")
        self.assertNotIn("/", value["message"])
