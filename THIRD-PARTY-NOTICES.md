# Third-party notices

Comic Archiver itself is released under the MIT license (see `LICENSE`). The
app folders for Linux and Windows also contain the software below, each under
its own license.
The license texts that each package supplies are copied into the `licenses/`
folder of the app (one subfolder per package, including the Qt LGPL text and
Python's own `LICENSE.txt`).
`requirements-frozen.txt` in the app folder lists the exact Python package
versions that were installed.

Comic Archiver is an independent personal-archiving tool. It is not affiliated
with, endorsed by or supported by Tapas Media, Inc. or any other company named
here.

## CPython

The Python interpreter is CPython, under the Python Software Foundation
License. Windows uses the Python Software Foundation's signed embeddable
Windows distribution (https://www.python.org/downloads/windows/). Linux uses
python-build-standalone (https://github.com/astral-sh/python-build-standalone).
Both distributions also contain other open-source libraries (for example
OpenSSL, SQLite, zlib and libffi); their licenses are included in the
interpreter's own license text. The Linux distribution additionally includes
libraries such as ncurses and readline.
Source: https://www.python.org/downloads/ and the python-build-standalone
release page.

## Qt and PySide6 (LGPL-3.0)

The application window uses Qt 6, including Qt WebEngine, through PySide6 and
Shiboken6 (the `PySide6`, `PySide6_Essentials`, `PySide6_Addons` and `shiboken6`
packages from The Qt Company). These are used under the GNU Lesser General
Public License version 3 (https://www.gnu.org/licenses/lgpl-3.0.html), with
the GNU General Public License version 3 where a component requires it. Qt and
PySide6 are copyright The Qt Company Ltd. and other contributors.

Qt is linked dynamically: its libraries are separate files inside
`python/lib/python*/site-packages/PySide6/` (Linux) or
`python\Lib\site-packages\PySide6\` (Windows). You may replace them with a
modified or different build of the same version of those libraries. On Linux,
for example, install another PySide6 build into this folder's Python with
`python/bin/python3 -m pip install`. The Windows app holds only the Qt
libraries it loads, and no pip: copy the files of another PySide6 build of the
same version over them (a PySide6 package from PyPI is a zip file).

Corresponding source code:

- Qt: https://download.qt.io/official_releases/qt/ and https://code.qt.io/
- PySide6 and Shiboken6: https://code.qt.io/cgit/pyside/pyside-setup.git and
  the source distribution of `PySide6` on https://pypi.org/project/PySide6/

Qt WebEngine is based on the Chromium project (https://www.chromium.org/),
which is made of many components under BSD-style, MIT, MPL and other licenses;
the full list and the source are at
https://code.qt.io/cgit/qt/qtwebengine-chromium.git.

## Other Python packages

- httpx (BSD-3-Clause) and its dependencies httpcore (BSD-3-Clause), h11 (MIT),
  anyio (MIT), idna (BSD-3-Clause) and certifi (MPL-2.0).
- zstandard (BSD-3-Clause), which bundles the Zstandard library (BSD-3-Clause).
- Pillow (MIT-CMU, the Historical Permission Notice and Disclaimer), which
  bundles libjpeg-turbo, libpng, libtiff, libwebp, FreeType, zlib and other
  libraries under their own permissive licenses.
- typing_extensions (PSF).

## Optional connected-reader source (not packaged or enabled)

The opt-in `connected` extra pins [JWCrypto1.6.1](https://pypi.org/project/jwcrypto/1.6.1/)
under LGPL-3.0-or-later and [aiortc1.15.0](https://pypi.org/project/aiortc/1.15.0/)
under BSD-3-Clause, plus [websockets15.0.1](https://pypi.org/project/websockets/15.0.1/)
under BSD-3-Clause. They remain ordinary replaceable Python dependencies. Source
metadata is in `packaging/connected-direct-sources.json`; neither dependency nor
its native/transitive packages has been installed or qualified for this source
stage. No current Linux/Windows app bundle gains the connected extra here.
Before distributing that opt-in host, preserve each supplied license/source notice
in the existing package-license collection and record the actual transitive/
native versions. The default downloader/desktop dependency set stays separate.
