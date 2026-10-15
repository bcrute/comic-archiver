import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from comic_archive.adapters.tapas import TapasAdapter
from comic_archive.models import ArchiveError
from comic_archive.session import browser_failure_reason, launch_browser


class BrowserStartupTests(unittest.TestCase):
    @patch("builtins.print")
    def test_auto_falls_back_before_loading_account_data(self, output):
        driver = Mock()
        driver.chromium.launch.side_effect = RuntimeError("SIGTRAP PRIVATE_TOKEN")
        browser = launch_browser(driver)
        self.assertIs(browser, driver.firefox.launch.return_value)
        driver.chromium.launch.assert_called_once_with(headless=False, timeout=15000)
        driver.firefox.launch.assert_called_once_with(headless=False, timeout=15000)
        self.assertNotIn("PRIVATE_TOKEN", str(output.call_args_list))

    def test_explicit_browser_does_not_try_another_engine(self):
        driver = Mock()
        self.assertIs(launch_browser(driver, "firefox"), driver.firefox.launch.return_value)
        driver.chromium.launch.assert_not_called()

    @patch("builtins.print")
    def test_startup_failures_report_safe_causes(self, output):
        driver = Mock()
        driver.chromium.launch.side_effect = RuntimeError("Operation not permitted PRIVATE_COOKIE")
        driver.firefox.launch.side_effect = RuntimeError("Executable doesn't exist PRIVATE_HEADER")
        with self.assertRaises(ArchiveError) as raised:
            launch_browser(driver)
        text = str(raised.exception)
        self.assertIn("socket access", text)
        self.assertIn("browser binary is missing", text)
        self.assertNotIn("PRIVATE", text)

    def test_session_failure_is_separate_and_closes_browser(self):
        with tempfile.TemporaryDirectory() as work:
            state = Path(work) / "state.json"
            state.write_text('{"cookies": [], "origins": []}')
            driver = Mock()
            driver.chromium.launch.return_value.new_context.side_effect = RuntimeError("PRIVATE cookie value")
            factory = Mock()
            factory.return_value.start.return_value = driver
            with patch("comic_archive.adapters.tapas.browser_api", return_value=factory):
                with self.assertRaises(ArchiveError) as raised:
                    TapasAdapter(Path(work)).__enter__()
            self.assertIn("Browser started", str(raised.exception))
            self.assertNotIn("PRIVATE", str(raised.exception))
            driver.chromium.launch.return_value.close.assert_called_once()
            driver.stop.assert_called_once()
            driver.firefox.launch.assert_not_called()

    def test_the_built_in_engine_is_asked_for_by_name_and_launched_with_the_saved_session(self):
        with tempfile.TemporaryDirectory() as work:
            session = self.session_directory(work)
            driver = Mock()
            factory = Mock()
            factory.return_value.start.return_value = driver
            adapter = TapasAdapter(session, 'qt', True)
            with patch("comic_archive.adapters.tapas.browser_api", return_value=factory) as api:
                adapter.__enter__()
            api.assert_called_once_with('qt')
            driver.qt.launch.assert_called_once_with(headless=True, timeout=15000)
            driver.chromium.launch.assert_not_called(); driver.firefox.launch.assert_not_called()
            context = driver.qt.launch.return_value.new_context
            self.assertEqual(context.call_args.kwargs, {"storage_state": {"cookies": [], "origins": []}, "service_workers": "block"})

    def test_unknown_error_text_not_echoed(self):
        self.assertNotIn("SECRET", browser_failure_reason(RuntimeError("Cookie: SECRET")))


    def session_directory(self, work):
        session = Path(work) / "session"
        session.mkdir()
        (session / "state.json").write_text('{"cookies": [], "origins": []}')
        return session

    def test_missing_or_symlinked_session_is_refused_before_any_browser_work(self):
        with tempfile.TemporaryDirectory() as work:
            real = self.session_directory(work)
            (Path(work) / "linked").symlink_to(real, target_is_directory=True)
            elsewhere = Path(work) / "elsewhere.json"
            elsewhere.write_text('{"cookies": [], "origins": []}')
            file_link = Path(work) / "file-link"
            file_link.mkdir()
            (file_link / "state.json").symlink_to(elsewhere)
            for session in (Path(work) / "absent", Path(work) / "linked", file_link):
                with patch("comic_archive.adapters.tapas.browser_api") as api:
                    with self.assertRaises(ArchiveError):
                        TapasAdapter(session).__enter__()
                    api.assert_not_called()

    def test_driver_start_failure_is_reported_without_raw_details(self):
        with tempfile.TemporaryDirectory() as work:
            session = self.session_directory(work)
            factory = Mock()
            factory.return_value.start.side_effect = RuntimeError("PRIVATE cookie value")
            with patch("comic_archive.adapters.tapas.browser_api", return_value=factory):
                with self.assertRaises(ArchiveError) as raised:
                    TapasAdapter(session).__enter__()
            self.assertIn("Playwright", str(raised.exception))
            self.assertNotIn("PRIVATE", str(raised.exception))
            factory.return_value.start.side_effect = ArchiveError("Install the browser extra.")
            with patch("comic_archive.adapters.tapas.browser_api", return_value=factory):
                with self.assertRaises(ArchiveError) as raised:
                    TapasAdapter(session).__enter__()
            self.assertEqual(str(raised.exception), "Install the browser extra.")

    def test_failed_browser_launch_stops_the_driver_and_keeps_the_error(self):
        with tempfile.TemporaryDirectory() as work:
            session = self.session_directory(work)
            driver = Mock()
            factory = Mock()
            factory.return_value.start.return_value = driver
            with patch("comic_archive.adapters.tapas.browser_api", return_value=factory), \
                    patch("comic_archive.adapters.tapas.launch_browser", side_effect=ArchiveError("No browser could start.")):
                with self.assertRaises(ArchiveError) as raised:
                    TapasAdapter(session).__enter__()
            self.assertEqual(str(raised.exception), "No browser could start.")
            driver.stop.assert_called_once()

    def test_exit_closes_the_browser_and_driver_even_when_the_transport_fails_to_close(self):
        with tempfile.TemporaryDirectory() as work:
            session = self.session_directory(work)
            driver = Mock()
            factory = Mock()
            factory.return_value.start.return_value = driver
            adapter = TapasAdapter(session)
            adapter.network_guard = Mock()
            with patch("comic_archive.adapters.tapas.browser_api", return_value=factory):
                self.assertIs(adapter.__enter__(), adapter)
            browser = driver.chromium.launch.return_value
            self.assertEqual(browser.new_context.call_args.kwargs["storage_state"], {"cookies": [], "origins": []})
            self.assertEqual(browser.new_context.call_args.kwargs["service_workers"], "block")
            browser.new_context.return_value.route.assert_called_once_with("**/*", adapter.network_request)
            self.assertIs(adapter.page, browser.new_context.return_value.new_page.return_value)
            adapter.transport = Mock()
            adapter.transport.close.side_effect = RuntimeError("PRIVATE")
            with self.assertRaises(RuntimeError):
                adapter.__exit__(None, None, None)
            browser.close.assert_called_once()
            driver.stop.assert_called_once()


if __name__ == "__main__":
    unittest.main()
