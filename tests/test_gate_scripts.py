import contextlib
import io
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from scripts import mutation_check, validate


class GateScriptTests(unittest.TestCase):
    def test_a_mutation_list_with_nothing_in_it_is_not_a_pass(self):
        with tempfile.TemporaryDirectory() as temporary:
            empty = Path(temporary) / 'empty.json'; empty.write_text('[]')
            one = Path(temporary) / 'one.json'; one.write_text(json.dumps([{'id':'a'}]))
            for lists, lines, status in (([empty], [], 1), ([one], ['CAUGHT   a'], 0), ([one, empty], ['CAUGHT   a'], 0),
                                         ([one], ['SURVIVED a'], 1)):
                with self.subTest(lists=lists, lines=lines), patch.object(mutation_check.sys, 'argv', ['check', *map(str, lists)]), \
                        patch.object(mutation_check, 'check', side_effect=lines), contextlib.redirect_stdout(io.StringIO()) as out:
                    self.assertEqual(mutation_check.main(), status)
                    self.assertIn(f'SUMMARY {sum(line.startswith("CAUGHT") for line in lines)} of {len(lines)}', out.getvalue())

    def test_the_audited_list_names_every_importable_distribution_once(self):
        def distribution(name, version):
            return SimpleNamespace(metadata={'Name':name} if name else None, version=version)
        found = [distribution('httpx', '0.28.1'), distribution('Typing_Extensions', '4.15.0'), distribution(None, '1'),
                 distribution('typing-extensions', '4.0.0'), distribution('comic-archive', '0.4.0')]
        self.assertEqual(validate.installed_requirements(found),
                         'comic-archive==0.4.0\nhttpx==0.28.1\nTyping_Extensions==4.15.0\n')
        self.assertEqual(validate.installed_requirements([]), '')
        # This interpreter's own packages are found without asking a tool that only lists one folder.
        self.assertIn('==', validate.installed_requirements())

    def test_the_dependency_audit_reads_that_list_and_stops_when_it_is_empty(self):
        with tempfile.TemporaryDirectory() as temporary, patch.object(validate, 'OUT', Path(temporary)), \
                patch.object(validate, 'command') as lint, contextlib.redirect_stdout(io.StringIO()):
            with patch.object(validate, 'installed_requirements', return_value='comic-archive==0.4.0\n'), \
                    patch('scripts.audit_bundle.subprocess.run') as scanner, self.assertRaises(ValueError):
                validate.static_checks()
            scanner.assert_not_called()
            lint.assert_called_once()
            with patch.object(validate, 'installed_requirements', return_value='httpx==0.28.1\n'), \
                    patch('scripts.audit_bundle.audit') as audit:
                validate.static_checks()
            audit.assert_called_once_with(Path(temporary) / 'audit-requirements.txt', Path(temporary) / 'audit.json')
            self.assertEqual((Path(temporary) / 'audit-requirements.txt').read_text(), 'httpx==0.28.1\n')


if __name__ == '__main__':
    unittest.main()
