# Genuine earlier package metadata

Exact pyproject.toml from0dca28f (same native runtime bytes as c605de7 qualified
by operations30/31), SHA25699b931d14f6ffe8c855862c9528485fee4affcd8808f1daa4afaabe176d96816.
The installed-package check verifies every unchanged package file and this earlier
metadata, builds and installs the earlier wheel, and lets its actual CLI produce
real local approval/state before replacing it with the current wheel. No earlier
state/permission row is fabricated. This fixture remains immutable.
