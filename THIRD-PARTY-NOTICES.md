# Third-party notices

Comic Archiver itself is copyright 2026 Ben Crute and released under the MIT
license (see `LICENSE`). The apps also contain the software below, each under
its own license. The full license texts are on the **Licenses** page in the
app's settings, and in the `licenses/` folder of the desktop app (one subfolder
per Python package, plus the texts in `licenses/other/`). The desktop app's
`requirements-frozen.txt` lists the exact Python package versions it holds.

Comic Archiver is an independent personal-archiving tool. It is not affiliated
with, endorsed by or supported by Tapas Media, Inc. or any other company named
here.

## In every app

### Python

The apps run on CPython, under the Python Software Foundation License, which
incorporates code under the licenses listed with it (`python-3.14.txt` on the
desktop, `python-3.13.txt` on Android). Python is built with these libraries:
OpenSSL (Apache-2.0), SQLite (public domain), libffi (MIT), bzip2 (bzip2
license), liblzma from XZ Utils (0BSD), mpdecimal (BSD-2-Clause), Expat (MIT)
and zlib (Zlib).
Source: https://www.python.org/downloads/

### Python packages

- httpx (BSD-3-Clause) and its dependencies httpcore (BSD-3-Clause), h11 (MIT),
  anyio (MIT), idna (BSD-3-Clause) and certifi (MPL-2.0), whose list of trusted
  certificate authorities comes from Mozilla.
- zstandard (BSD-3-Clause), which bundles the Zstandard library (BSD-3-Clause).
- typing_extensions (PSF-2.0).

### Webtoon signing protocol

The mobile gateway signing implementation follows the MIT-licensed
[webtoon-api client](https://github.com/s0ko1ex/webtoon-api), copyright 2023
Alexandr Sokolov. Its copyright and full permission notice are retained in
`comic_archive/webtoon_gateway.py`. No additional runtime package is required.

## Desktop app (Linux, Windows and macOS)

### Python on the desktop

Windows uses the Python Software Foundation's signed embeddable Windows
distribution (https://www.python.org/downloads/windows/); its own `LICENSE.txt`
adds the conditions of that Windows build. Linux and macOS use python-build-standalone
(https://github.com/astral-sh/python-build-standalone), which also builds in
ncurses (X11 license), libedit (BSD-3-Clause), libuuid (BSD-3-Clause), zlib-ng
(Zlib) and Zstandard (BSD-3-Clause).

### More Python packages

- Pillow (MIT-CMU, the Historical Permission Notice and Disclaimer), which
  bundles libjpeg-turbo, libpng, libtiff, libwebp, FreeType, zlib and other
  libraries under their own permissive licenses.
- tzdata (Apache-2.0), on Windows.
- psutil (BSD-3-Clause), on macOS, for native process and volume information.
  Source: https://github.com/giampaolo/psutil
- pip (MIT) and the packages it carries, on Linux and macOS, so that Qt can be replaced
  (see below).

### Qt and PySide6 (LGPL-3.0)

The application window uses Qt 6, including Qt WebEngine, through PySide6 and
Shiboken6 (the `PySide6`, `PySide6_Essentials`, `PySide6_Addons` and `shiboken6`
packages from The Qt Company). These are used under the GNU Lesser General
Public License version 3, which builds on the GNU General Public License
version 3; both texts are in `licenses/other/`. Qt and PySide6 are copyright
The Qt Company Ltd. and other contributors.

Qt is linked dynamically: its libraries are separate files inside
`python/lib/python*/site-packages/PySide6/` (Linux) or
`python\Lib\site-packages\PySide6\` (Windows). The app holds only the Qt
libraries it loads on Linux and Windows. The macOS bundle keeps Qt under
`Contents/Resources/python/lib/python*/site-packages/PySide6/`, retaining
the framework and browser-helper bundle layouts. You may replace them with a modified or different build of
the same version of those libraries. On Linux, for example, install another
PySide6 build into this folder's Python with `python/bin/python3 -m pip install`.
The Windows app has no pip: copy the files of another PySide6 build of the same
version over them (a PySide6 package from PyPI is a zip file).

Corresponding source code:

- Qt: https://download.qt.io/official_releases/qt/ and https://code.qt.io/
- PySide6 and Shiboken6: https://code.qt.io/cgit/pyside/pyside-setup.git and
  the source distribution of `PySide6` on https://pypi.org/project/PySide6/

Qt WebEngine is based on the Chromium project (https://www.chromium.org/),
which is made of many components under BSD-style, MIT, MPL and other licenses;
the full list and the source are at
https://code.qt.io/cgit/qt/qtwebengine-chromium.git.

## Android app

- Chaquopy (MIT), which runs Python on Android, copyright Chaquo Ltd and
  contributors. Its start-up code carries setuptools (MIT) and pyelftools
  (public domain).
- AndroidX, Jetpack Compose and Material 3 (Apache-2.0), copyright The Android
  Open Source Project and Google LLC.
- The Kotlin standard library and kotlinx.coroutines (Apache-2.0), copyright
  JetBrains s.r.o. and Kotlin contributors.

## Optional connected-reader source (not packaged or enabled)

The opt-in `connected` extra pins [JWCrypto1.6.1](https://pypi.org/project/jwcrypto/1.6.1/)
under LGPL-3.0-or-later and [aiortc1.15.0](https://pypi.org/project/aiortc/1.15.0/)
under BSD-3-Clause, plus [websockets15.0.1](https://pypi.org/project/websockets/15.0.1/)
under BSD-3-Clause. They remain ordinary replaceable Python dependencies. Neither
they nor their native/transitive packages have been installed or qualified for this
source stage. No current app gains the connected extra here.
Before distributing that opt-in host, preserve each supplied license/source notice
in the existing package-license collection and record the actual transitive/
native versions. The default downloader/desktop dependency set stays separate.
