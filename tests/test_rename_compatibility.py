"""Upgrade behavior: preserve existing accounts, queue paths and library choices."""
import importlib
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from comic_archive import background, batches
from comic_archive.cli import parser
from comic_archive.hosted import Workspace
from comic_archive.local_web import LOCAL_OWNER, LocalWorkspaceRegistry
from comic_archive.models import ArchiveError
from comic_archive.state_paths import private_path, session_path


class RenameCompatibilityTests(unittest.TestCase):
    def setUp(self):
        work = tempfile.TemporaryDirectory(); self.addCleanup(work.cleanup)
        self.root = Path(work.name)

    def test_fresh_storage_is_neutral_and_legacy_storage_is_reused_without_copying(self):
        for kind in ('session', 'web', 'automation', 'hosted'):
            self.assertEqual(private_path(self.root, kind), self.root / ('.comic-' + kind))
            old = self.root / ('.tapas-' + kind); old.mkdir()
            (old / 'keep').write_text('existing')
            self.assertEqual(private_path(self.root, kind), old)
            self.assertEqual((old / 'keep').read_text(), 'existing')
            self.assertFalse((self.root / ('.comic-' + kind)).exists())

    def test_ambiguous_or_symlinked_state_is_rejected_before_creating_another_session(self):
        old = self.root / '.tapas-session'; old.mkdir()
        new = self.root / '.comic-session'; new.mkdir()
        with self.assertRaises(ArchiveError): session_path(self.root)
        new.rmdir(); old.rmdir(); old.symlink_to(self.root, target_is_directory=True)
        with self.assertRaises(ArchiveError): session_path(self.root)
        self.assertFalse(new.exists())

    def test_old_queue_and_cli_default_resolve_to_the_same_session(self):
        session = self.root / '.tapas-session'; queue = session / 'worker'
        (queue / 'requests').mkdir(parents=True); (queue / 'responses').mkdir()
        library = self.root / 'Library'; library.mkdir()
        previous = Path.cwd()
        try:
            os.chdir(self.root)
            args = parser().parse_args(['worker-status'])
            self.assertEqual(args.session_dir.absolute(), session)
            self.assertEqual(background.root(args.session_dir), queue)
            self.assertEqual(batches._root(args.session_dir, library), queue)
            self.assertEqual(Workspace('existing', self.root).session, session)
        finally:
            os.chdir(previous)

    def test_legacy_dashboard_identity_session_and_library_are_preserved(self):
        registry = LocalWorkspaceRegistry(self.root)
        workspace = registry.resolve(LOCAL_OWNER, registry.workspace_id)
        original_id = workspace.id
        registry.tapas_signin.close(); registry.cloud_signin.close()
        (self.root / '.comic-web').rename(self.root / '.tapas-web')
        workspace.session.rename(self.root / '.tapas-session')
        state = self.root / '.tapas-session/state.json'; state.write_text('existing private session')
        registry = LocalWorkspaceRegistry(self.root)
        self.addCleanup(registry.tapas_signin.close); self.addCleanup(registry.cloud_signin.close)
        self.assertEqual(registry.workspace_id, original_id)
        self.assertEqual(registry.resolve(LOCAL_OWNER, original_id).session, state.parent)
        self.assertEqual(state.read_text(), 'existing private session')
        self.assertFalse((self.root / '.comic-web').exists())
        self.assertFalse((self.root / '.comic-session').exists())

    def test_legacy_and_neutral_imports_share_classes_patches_and_reload_identity(self):
        # Both import orders must work in fresh processes; a warmed-up test
        # runner can conceal a duplicate exception class or broken package alias.
        for first, second in (('tapas_archive', 'comic_archive'), ('comic_archive', 'tapas_archive')):
            code = f'''
import importlib
from unittest.mock import patch
first = importlib.import_module({first!r} + '.models')
second = importlib.import_module({second!r} + '.models')
assert first is second and first.ArchiveError is second.ArchiveError
old = importlib.import_module('tapas_archive.state_paths')
new = importlib.import_module('comic_archive.state_paths')
assert old is new and old.__spec__.name == 'comic_archive.state_paths'
with patch('tapas_archive.state_paths.session_path', return_value='shared'):
    assert new.session_path('anywhere') == 'shared'
assert importlib.reload(old) is new
assert old.__spec__.name == 'comic_archive.state_paths'
old_adapter = importlib.import_module('tapas_archive.adapters.tapas')
new_adapter = importlib.import_module('comic_archive.adapters.tapas')
assert old_adapter is new_adapter and old_adapter.TapasAdapter is new_adapter.TapasAdapter
'''
            result = subprocess.run([sys.executable, '-c', code], capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stderr)
