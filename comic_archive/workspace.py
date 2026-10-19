"""The project folder the desktop app works in: its library and its private session beside it."""
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class Workspace:
    id: str
    root: Path = field(repr=False)

    @property
    def library(self):
        return self.root/'Library'

    @property
    def session(self):
        from .state_paths import session_path
        return session_path(self.root)
