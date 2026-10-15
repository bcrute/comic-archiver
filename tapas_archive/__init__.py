"""Compatibility imports for installations predating the Comic Archiver rename.

Legacy submodule imports resolve to the same objects as comic_archive, keeping
exception types, patches and active worker reloads consistent. New code should
import comic_archive directly.
"""
import importlib
import importlib.abc
import importlib.util
import sys


class _LegacyLoader(importlib.abc.Loader):
    def __init__(self, canonical):
        self.canonical = canonical

    def create_module(self, spec):
        module = importlib.import_module(self.canonical)
        self.attributes = {key: getattr(module, key) for key in ('__spec__', '__loader__', '__package__')}
        return module

    def exec_module(self, module):
        # Import machinery sets the alias spec on the returned module; restore
        # the canonical spec so importlib.reload still reloads its source.
        for key, value in self.attributes.items():
            setattr(module, key, value)

    def get_code(self, fullname):
        spec = importlib.util.find_spec(self.canonical)
        return spec.loader.get_code(self.canonical)


class _LegacyFinder(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if not fullname.startswith('tapas_archive.') or fullname == 'tapas_archive.__main__':
            return None
        canonical = 'comic_archive' + fullname[len('tapas_archive'):]
        spec = importlib.util.find_spec(canonical)
        if spec is None:
            return None
        return importlib.util.spec_from_loader(fullname, _LegacyLoader(canonical),
                                              is_package=spec.submodule_search_locations is not None)


sys.meta_path.insert(0, _LegacyFinder())
sys.modules[__name__] = importlib.import_module('comic_archive')
