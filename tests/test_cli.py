import io
import os
import runpy
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch
from comic_archive.cli import main, parser, run
from comic_archive.models import ArchiveError


class CLITests(unittest.TestCase):
    def test_the_built_in_engine_gets_the_main_thread_and_every_other_browser_runs_the_command_directly(self):
        from comic_archive import cli
        for browser in ('auto', 'chromium', 'firefox'):
            with patch('comic_archive.cli.run', return_value=7) as command, patch('comic_archive.qt_engine.serve') as serve:
                args = parser().parse_args(['--browser', browser, 'doctor'])
                self.assertEqual(cli.run_beside_engine(args), 7)
                command.assert_called_once_with(args)
                serve.assert_not_called()
        with patch('comic_archive.cli.run', return_value=7) as command, \
                patch('comic_archive.qt_engine.serve', side_effect=lambda work: work()) as serve:
            args = parser().parse_args(['--browser', 'qt', 'doctor'])
            self.assertEqual(cli.run_beside_engine(args), 7)
            serve.assert_called_once()
            command.assert_called_once_with(args)

    @patch('comic_archive.worker.run_worker')
    def test_browser_worker_runs_the_fixed_worker(self,worker):
        args=parser().parse_args(['--browser','firefox','--headless','browser-worker'])
        run(args)
        worker.assert_called_once_with(args.session_dir,'firefox',True,1.0)

    def test_main_turns_failures_into_exit_codes_and_never_echoes_raw_exceptions(self):
        secret = 'https://cdn.example/page.png?token=SECRET'
        cases = ((ArchiveError('Library is busy.'), 1, 'Error: Library is busy.'),
                 (KeyboardInterrupt(), 130, 'completed pages remain cached'),
                 (EOFError(), 1, 'needs a terminal'),
                 (RuntimeError('GET ' + secret), 1, 'Operation failed'))
        for error, code, text in cases:
            with self.subTest(error=type(error).__name__), patch('sys.argv', ['comic-archive', 'doctor']), \
                    patch('comic_archive.cli.run', side_effect=error), redirect_stderr(io.StringIO()) as err, \
                    self.assertRaises(SystemExit) as raised:
                main()
            self.assertEqual(raised.exception.code, code)
            self.assertIn(text, err.getvalue())
            self.assertNotIn('SECRET', err.getvalue())
        for returned in (0, 1):
            with patch('sys.argv', ['comic-archive', 'doctor']), patch('comic_archive.cli.run', return_value=returned), \
                    self.assertRaises(SystemExit) as raised:
                main()
            self.assertEqual(raised.exception.code, returned)

    def test_main_ends_quietly_on_a_closed_pipe_and_shows_tracebacks_only_when_debugging(self):
        with patch('sys.argv', ['comic-archive', 'doctor']), patch('comic_archive.cli.run', side_effect=BrokenPipeError()), \
                redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()) as err, self.assertRaises(SystemExit) as raised:
            main()
        self.assertEqual((raised.exception.code, err.getvalue()), (141, ''))
        for debug in ('1', None):
            with self.subTest(debug=debug), patch.dict('os.environ'), patch('sys.argv', ['comic-archive', 'doctor']), \
                    patch('comic_archive.cli.run', side_effect=RuntimeError('DETAIL')), redirect_stderr(io.StringIO()) as err, \
                    self.assertRaises(SystemExit) as raised:
                os.environ.pop('COMIC_ARCHIVE_DEBUG', None)
                if debug: os.environ['COMIC_ARCHIVE_DEBUG'] = debug
                main()
            self.assertEqual(raised.exception.code, 1)
            self.assertIn('Operation failed', err.getvalue())
            self.assertEqual(('Traceback' in err.getvalue(), 'DETAIL' in err.getvalue()), (bool(debug), bool(debug)))

    def test_a_symlinked_default_session_folder_never_breaks_parsing_and_is_refused_when_used(self):
        with tempfile.TemporaryDirectory() as tmp, patch('pathlib.Path.cwd', return_value=Path(tmp)):
            (Path(tmp) / 'elsewhere').mkdir()
            (Path(tmp) / '.comic-session').symlink_to(Path(tmp) / 'elsewhere')
            self.assertEqual(parser().parse_args(['doctor']).session_dir, Path('.comic-session'))
            with redirect_stdout(io.StringIO()) as out, self.assertRaises(SystemExit) as raised:
                parser().parse_args(['--help'])
            self.assertEqual(raised.exception.code, 0)
            self.assertIn('comic-archive', out.getvalue())
            link = Path(tmp) / '.comic-session'
            with patch('comic_archive.worker._run_worker') as work, self.assertRaises(ArchiveError):
                run(parser().parse_args(['--session-dir', str(link), 'browser-worker']))
            work.assert_not_called()
            self.assertEqual(list((Path(tmp) / 'elsewhere').iterdir()), [])

    def test_the_command_line_is_only_the_downloader_and_the_browser_check(self):
        for retired in ('sync', 'verify', 'serve-local', 'cloud-status'):
            with self.subTest(command=retired), redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                parser().parse_args([retired])

    def test_python_dash_m_entry_point_exits_nonzero_when_the_command_fails(self):
        with patch('sys.argv', ['comic-archive', 'doctor']), \
                patch('comic_archive.session.doctor', side_effect=ArchiveError('Cannot start a browser.')), \
                redirect_stderr(io.StringIO()) as err, self.assertRaises(SystemExit) as raised:
            runpy.run_module('comic_archive', run_name='__main__')
        self.assertEqual(raised.exception.code, 1)
        self.assertIn('Cannot start a browser.', err.getvalue())


if __name__ == "__main__":
    unittest.main()
