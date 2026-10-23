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

from comic_archive import android_session, network_android, sites, worker_host
from comic_archive.models import ArchiveError


def _cookie_header(url):
    return ""


def _set_cookies(url, values):
    return True


class AdapterFactoryTests(unittest.TestCase):
    def test_native_browser_selection_is_lazy_and_keeps_the_selected_library_guard(self):
        from comic_archive.android_browser import AndroidTapasBrowserAdapter
        from test_android_browser import Factory
        with tempfile.TemporaryDirectory() as temporary:
            library = Path(temporary) / 'Library'
            factory = Factory()
            adapter = worker_host.android_adapter_factory(_cookie_header, _set_cookies,
                library_root=library, browser_factory=factory)()
            self.assertIsInstance(adapter, AndroidTapasBrowserAdapter)
            self.assertEqual(adapter.session, library.parent / '.comic-session')
            self.assertEqual(adapter.network_guard.root, library)
            self.assertEqual(factory.opens, [])
            adapter.close()

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

    def test_native_browser_and_fast_pool_reach_ready_and_drain_without_loading_any_source(self):
        from test_android_browser import Factory
        factory = Factory()
        started = worker_host.start_in_process(self.session, self.library, _cookie_header, _set_cookies,
            self.root / 'net.json', browser_factory=factory)
        self.assertTrue(started['running'], started)
        self.assertEqual(started['state'], 'ready')
        self.assertEqual(factory.opens, [])
        self.assertEqual(factory.native.calls, [])
        stopped = worker_host.request_stop(self.session, wait_seconds=15)
        self.assertFalse(stopped['running'], stopped)
        self.assertFalse(any(call[0] in {'load', 'execute'} for call in factory.native.calls))

    def test_status_before_any_worker_is_stopped_not_unknown(self):
        observed = worker_host.status(self.session)
        self.assertFalse(observed["running"])
        self.assertEqual(observed["state"], "stopped")

    def test_each_provider_can_start_held_and_drain_before_the_next_workspace(self):
        for site in sites.SITES:
            with self.subTest(site=site.key):
                workspace = sites.folder(self.root, site)
                self.session = workspace / '.comic-session'
                self.library = workspace / 'Library'
                self.library.mkdir(parents=True, exist_ok=True)
                android_session.mark_signed_in(self.session, site=site)
                facts = workspace / 'net.json'
                facts.write_text('{"connection":"wifi","metered":false}')
                os.chdir(workspace)
                started = worker_host.start_in_process(self.session, self.library,
                    _cookie_header, _set_cookies, facts)
                self.assertTrue(started['running'], started)
                self.assertEqual(started['state'], 'ready')
                stopped = worker_host.request_stop(self.session, wait_seconds=15)
                self.assertFalse(stopped['running'], stopped)

    def test_a_refused_cross_workspace_start_cannot_retarget_the_live_network_probe(self):
        started = worker_host.start_in_process(self.session, self.library,
            _cookie_header, _set_cookies, self.root / 'net.json')
        self.assertTrue(started['running'], started)
        other = self.root / 'Webtoon'
        other_library = other / 'Library'
        other_library.mkdir(parents=True)
        other_session = other / '.comic-session'
        android_session.mark_signed_in(other_session, site=sites.named('webtoon'))
        (other / 'net.json').write_text('{"connection":"cellular","metered":true}')
        with self.assertRaises(ArchiveError):
            worker_host.start_in_process(other_session, other_library,
                _cookie_header, _set_cookies, other / 'net.json')
        self.assertEqual(network_android.probe(), {'connection': 'wifi', 'metered': False})
        self.assertTrue(worker_host.status(self.session)['running'])
