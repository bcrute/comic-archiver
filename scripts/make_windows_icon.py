"""Draw packaging/windows/comic-archiver.ico from packaging/comic-archiver.svg.

Run again only when the drawing changes; the result is kept in the repository so that building the
Windows app needs no drawing tools. Needs the desktop and validation extras (PySide6 and Pillow).

    QT_QPA_PLATFORM=offscreen python3 scripts/make_windows_icon.py
"""
import io
from pathlib import Path
import sys

PROJECT = Path(__file__).resolve().parents[1]
SOURCE = PROJECT / 'packaging/comic-archiver.svg'
TARGET = PROJECT / 'packaging/windows/comic-archiver.ico'
# What Windows asks for between the small icon of a title bar and the large one of the desktop.
SIZES = (16, 20, 24, 32, 40, 48, 64, 128, 256)


def drawn(size):
    from PIL import Image
    from PySide6 import QtCore, QtGui, QtSvg
    picture = QtGui.QImage(size, size, QtGui.QImage.Format.Format_ARGB32)
    picture.fill(QtCore.Qt.GlobalColor.transparent)
    painter = QtGui.QPainter(picture)
    try:
        painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
        renderer = QtSvg.QSvgRenderer(str(SOURCE))
        if not renderer.isValid():
            raise RuntimeError('The drawing could not be read')
        renderer.render(painter)
    finally:
        painter.end()
    store = QtCore.QBuffer()
    store.open(QtCore.QIODevice.OpenModeFlag.WriteOnly)
    picture.save(store, 'PNG')
    return Image.open(io.BytesIO(bytes(store.data()))).convert('RGBA')


def main():
    from PySide6 import QtGui
    application = QtGui.QGuiApplication.instance() or QtGui.QGuiApplication([])
    pictures = [drawn(size) for size in SIZES]
    TARGET.parent.mkdir(parents=True, exist_ok=True)
    # Every size is drawn from the lines of the drawing, not shrunk from the largest.
    pictures[-1].save(TARGET, format='ICO', sizes=[(size, size) for size in SIZES], append_images=pictures[:-1])
    del application
    print(f'Wrote {TARGET.relative_to(PROJECT)}: {TARGET.stat().st_size} bytes, sizes {SIZES}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
