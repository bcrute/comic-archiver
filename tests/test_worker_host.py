"""The in-process worker host: seam wiring, refusals and one full worker lifecycle.

The lifecycle test runs the real worker loop against a throwaway workspace with the
Android adapter factory; no request is enqueued, so no network is touched — it proves
start, ready publication and the drain protocol, not downloading.
"""
import json
import os
import tempfile
import unittest
from pathlib import Path

from comic_archive import android_session, network_android, worker_host
from comic_archive.models import ArchiveError


def _cookie_header(url):
    return ""


def _set_cookies(url, values):
    return True


class AdapterFactoryTests(unittest.TestCase):
    def test_the_factory_builds_one_android_adapter_on_the_transport_seams(self):
        source, sink = _cookie_header, _set_cookies
        # No `with`: the adapter's enter runs the core's who-am-i (a network call); the
        # worker's job path calls it per job, never at worker start.
        adapter = worker_host.android_adapter_factory(source, sink)()
        self.assertIs(adapter.transport.cookie_source, source)
        self.assertIs(adapter.transport.cookie_sink, sink)
        self.assertEqual(adapter.transport.cookies, ())  # no jar beside the live source
        self.assertIn('tapas.io', adapter.transport.site.cookie_hosts)

    def test_the_factory_takes_the_kotlin_store_on_its_android_transport_route(self):
        class Store:
            def headerFor(self, url):
                return None

            def apply(self, url, values):
                return None

        from comic_archive.android_adapter import AndroidTapasAdapter
        from comic_archive.android_transport import AndroidTransport, WebViewCookieSource
        adapter = worker_host.android_adapter_factory(Store())()
        self.assertIsInstance(adapter, AndroidTapasAdapter)
        self.assertIsInstance(adapter.transport, AndroidTransport)
        self.assertIsInstance(adapter.transport.store, WebViewCookieSource)
        self.assertIs(adapter.transport.assets, adapter.transport)  # the pipeline's reader

    def test_non_callable_seams_are_refused(self):
        for source, sink in (("no", None), (None, None), (lambda url: "", "no")):
            with self.subTest((source, sink)):
                with self.assertRaises(ArchiveError):
                    worker_host.android_adapter_factory(source, sink)


class StartRefusalTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="worker-host-")
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.session = self.root / ".comic-session"
        self.session.mkdir()
        (self.root / "Library").mkdir()

    def test_start_needs_a_whole_android_sign_in_record(self):
        with self.assertRaises(ArchiveError):
            worker_host.start_in_process(self.session, self.root / "Library",
                                         _cookie_header, None, self.root / "net.json")

    def test_start_refuses_roots_that_are_not_one_workspace(self):
        android_session.mark_signed_in(self.session)
        other = self.root / "Elsewhere" / "Library"
        other.parent.mkdir()
        with self.assertRaises(ArchiveError):
            worker_host.start_in_process(self.session, other,
                                         _cookie_header, None, self.root / "net.json")
        stray = self.root / "session-copy"
        stray.mkdir()
        with self.assertRaises(ArchiveError):
            worker_host.start_in_process(stray, self.root / "Library",
                                         _cookie_header, None, self.root / "net.json")


class WorkerLifecycleTests(unittest.TestCase):
    def setUp(self):
        self._previous = Path.cwd()
        self._tmp = tempfile.TemporaryDirectory(prefix="worker-host-run-")
        self.addCleanup(self._cleanup)
        self.root = Path(self._tmp.name)
        self.session = self.root / ".comic-session"
        self.library = self.root / "Library"
        self.library.mkdir()
        android_session.mark_signed_in(self.session)
        # The one workspace rule: the worker's queue paths are workspace-relative.
        os.chdir(self.root)
        (self.root / "net.json").write_text(json.dumps({"connection": "wifi", "metered": False}))
        network_android.configure(None)

    def _cleanup(self):
        current = worker_host.status(self.session)
        if current["running"]:
            worker_host.request_stop(self.session, wait_seconds=15)
        os.chdir(self._previous)
        network_android.configure(None)
        self._tmp.cleanup()

    def test_start_reaches_ready_and_the_drain_protocol_stops_it(self):
        started = worker_host.start_in_process(self.session, self.library,
                                               _cookie_header, _set_cookies,
                                               self.root / "net.json")
        self.assertTrue(started["started"], started)
        self.assertEqual(started["state"], "ready")
        # A second start while one worker owns the queue is a no-op, not a second worker.
        again = worker_host.start_in_process(self.session, self.library,
                                             _cookie_header, None, self.root / "net.json")
        self.assertFalse(again["started"], again)
        stopped = worker_host.request_stop(self.session, wait_seconds=15)
        self.assertFalse(stopped["running"], stopped)
        self.assertEqual(stopped["state"], "stopped")

    def test_status_before_any_worker_is_stopped_not_unknown(self):
        observed = worker_host.status(self.session)
        self.assertFalse(observed["running"])
        self.assertEqual(observed["state"], "stopped")
