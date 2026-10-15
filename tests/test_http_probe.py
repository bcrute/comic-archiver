import json
import unittest
from unittest.mock import Mock, patch

import httpx

from comic_archive.adapters.http_probe import probe_session
from comic_archive.network import NetworkPolicyHold


class HTTPProbeTests(unittest.TestCase):
    def test_policy_holds_identity_probe_before_request_and_during_response(self):
        client=Mock();guard=Mock();guard.check.side_effect=NetworkPolicyHold('held')
        with self.assertRaises(NetworkPolicyHold):probe_session([],client,network_guard=guard)
        client.build_request.assert_not_called()
        guard.check.side_effect=[None,NetworkPolicyHold('changed')]
        with httpx.Client(transport=httpx.MockTransport(lambda request:httpx.Response(200,json={'data':{'user':{'id':1}}}))) as actual:
            with self.assertRaises(NetworkPolicyHold):probe_session([],actual,network_guard=guard)
        self.assertEqual(guard.check.call_count,3)

    def probe(self, response, cookies=()):
        calls = []
        def reply(request):
            calls.append(request)
            return response(request) if callable(response) else response
        with httpx.Client(transport=httpx.MockTransport(reply)) as client:
            result = probe_session(cookies, client)
        return result, calls

    def test_one_identity_get_preserves_cookie_paths_and_exports_only_outcome(self):
        cookies = [{"name": "session", "value": "PRIVATE_COOKIE", "domain": ".tapas.io", "path": "/"},
                   {"name": "other", "value": "PRIVATE_PATH", "domain": "tapas.io", "path": "/other"},
                   {"name": "expired", "value": "PRIVATE_EXPIRED", "domain": "tapas.io", "expires": 1},
                   {"name": "elsewhere", "value": "PRIVATE_HOST", "domain": "example.com"}]
        result, calls = self.probe(httpx.Response(200, json={"data": {"user": {"id": 123,
                                                "name": "PRIVATE_NAME"}}}), cookies)
        self.assertTrue(result["authenticated"])
        self.assertEqual(len(calls), 1)
        self.assertEqual(str(calls[0].url), "https://tapas.io/who-am-i")
        self.assertEqual(calls[0].method, "GET")
        self.assertEqual(calls[0].headers.get("Cookie"), "session=PRIVATE_COOKIE")
        self.assertNotIn("PRIVATE", json.dumps(result))
        self.assertNotIn("123", json.dumps(result))

    def test_refused_or_redirected_identity_never_retries_or_follows(self):
        for code in (301, 302, 401, 403, 429, 503):
            with self.subTest(code=code):
                result, calls = self.probe(httpx.Response(code, headers={"Location": "https://example.com/"}))
                self.assertEqual(len(calls), 1)
                self.assertEqual(result["http_status"], code)
                self.assertEqual(result["reason"], "http_refused_or_redirected")
                self.assertFalse(result["authenticated"])

    def test_unexpected_metadata_never_grants_identity(self):
        for user in (None, {}, {"id": True}, {"id": 0}, {"id": "123"}):
            result, _ = self.probe(httpx.Response(200, json={"data": {"user": user}}))
            self.assertFalse(result["authenticated"])
        result, _ = self.probe(httpx.Response(200, content=b"PRIVATE HTML"))
        self.assertEqual(result["reason"], "metadata_unexpected")
        self.assertNotIn("PRIVATE", json.dumps(result))

    def test_large_or_failed_request_has_safe_bounded_result(self):
        result, _ = self.probe(httpx.Response(200, content=b"x" * (2 * 1024 * 1024 + 1)))
        self.assertEqual(result["reason"], "metadata_too_large")
        def fail(request):
            raise httpx.ConnectError("PRIVATE_COOKIE", request=request)
        result, calls = self.probe(fail)
        self.assertEqual(len(calls), 1)
        self.assertEqual(result["reason"], "network_or_client_error")
        self.assertNotIn("PRIVATE", json.dumps(result))

    def test_different_account_is_rejected_without_exporting_identifiers(self):
        with httpx.Client(transport=httpx.MockTransport(lambda request:
                httpx.Response(200,json={'data':{'user':{'id':1234567}}}))) as client:
            result=probe_session([],client,expected_account_id='7654321')
        self.assertFalse(result['authenticated'])
        self.assertEqual(result['reason'],'account_changed')
        self.assertNotIn('1234567',json.dumps(result))
        self.assertNotIn('7654321',json.dumps(result))

    def test_own_client_ignores_proxies_and_redirects_and_is_always_closed_but_a_borrowed_one_is_left_open(self):
        real=httpx.Client;created=[];handler={'reply':lambda request:httpx.Response(200,json={'data':{'user':{'id':5}}})}
        def factory(**options):
            client=real(transport=httpx.MockTransport(lambda request:handler['reply'](request)),**options)
            created.append((client,options))
            return client
        def refuse(request):
            raise httpx.ConnectError('PRIVATE_COOKIE',request=request)
        with patch('httpx.Client',factory):
            self.assertTrue(probe_session([])['authenticated'])
            handler['reply']=refuse
            self.assertEqual(probe_session([])['reason'],'network_or_client_error')
        for client,options in created:
            self.assertTrue(client.is_closed)
            self.assertIs(options['trust_env'],False);self.assertIs(options['follow_redirects'],False)
        self.assertEqual(len(created),2)
        with httpx.Client(transport=httpx.MockTransport(lambda request:httpx.Response(200,json={'data':{'user':{'id':5}}}))) as borrowed:
            self.assertTrue(probe_session([],borrowed)['authenticated'])
            self.assertFalse(borrowed.is_closed)
