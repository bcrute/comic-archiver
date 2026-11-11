"""Native, scalable paper folds for the provider cards. No images or network resources."""
from .window_style import card_colours


def paper_card(q):
    class PaperCard(q.QtWidgets.QFrame):
        def paintEvent(self, event):
            colours = card_colours(self.property('appearance'), bool(self.window().property('comicDark')))
            painter = q.QtGui.QPainter(self)
            painter.setRenderHint(q.QtGui.QPainter.RenderHint.Antialiasing)
            rect = q.QtCore.QRectF(self.rect()).adjusted(.75, .75, -.75, -.75)
            outline = q.QtGui.QPainterPath()
            outline.addRoundedRect(rect, 18, 18)
            painter.setClipPath(outline)
            gradient = q.QtGui.QLinearGradient(rect.topLeft(), rect.bottomRight())
            gradient.setColorAt(0, q.QtGui.QColor(colours['surface']))
            gradient.setColorAt(1, q.QtGui.QColor(colours['shade']))
            painter.fillPath(outline, q.QtGui.QBrush(gradient))
            edge = q.QtGui.QColor(colours['accent'])
            edge.setAlpha(110)
            painter.setPen(q.QtGui.QPen(edge, 1.5))
            painter.drawPath(outline)
            # Three paper faces, with a lifted edge and cast shadows. Decoration
            # stays in the corner; it never implies download progress.
            right = rect.right()
            underside = q.QtGui.QPainterPath()
            underside.moveTo(right, 36)
            underside.lineTo(right, 108)
            underside.lineTo(right - 52, 85)
            underside.closeSubpath()
            back = q.QtGui.QLinearGradient(right - 52, 45, right, 108)
            back.setColorAt(0, q.QtGui.QColor(colours['accent']).lighter(135))
            back.setColorAt(1, q.QtGui.QColor(colours['accent']).darker(115))
            painter.fillPath(underside, q.QtGui.QBrush(back))
            shadow = q.QtGui.QPainterPath()
            shadow.moveTo(right - 16, 34)
            shadow.lineTo(right, 64)
            shadow.lineTo(right - 56, 90)
            shadow.closeSubpath()
            painter.fillPath(shadow, q.QtGui.QColor(0, 0, 0, 85))
            turn = q.QtGui.QPainterPath()
            turn.moveTo(right - 18, 30)
            turn.lineTo(right, 62)
            turn.lineTo(right - 52, 85)
            turn.closeSubpath()
            crease = q.QtGui.QLinearGradient(right - 43, 75, right - 4, 44)
            crease.setColorAt(0, q.QtGui.QColor(colours['accent']).lighter(125))
            crease.setColorAt(1, q.QtGui.QColor(colours['accent']).darker(180))
            painter.fillPath(turn, q.QtGui.QBrush(crease))
            fold = q.QtGui.QPainterPath()
            fold.moveTo(right - 94, 0)
            fold.lineTo(right, 0)
            fold.lineTo(right, 53)
            fold.closeSubpath()
            lifted = q.QtGui.QPainterPath()
            lifted.moveTo(right - 94, 2)
            lifted.lineTo(right, 56)
            lifted.lineTo(right, 63)
            lifted.closeSubpath()
            painter.fillPath(lifted, q.QtGui.QColor(0, 0, 0, 90))
            face = q.QtGui.QLinearGradient(right - 94, 0, right, 53)
            face.setColorAt(0, q.QtGui.QColor(colours['accent']).lighter(140))
            face.setColorAt(1, q.QtGui.QColor(colours['accent']))
            painter.fillPath(fold, q.QtGui.QBrush(face))
            painter.save()
            painter.setClipPath(fold, q.QtCore.Qt.ClipOperation.IntersectClip)
            dots = q.QtGui.QColor(colours['button-text'])
            dots.setAlpha(105)
            painter.setPen(q.QtCore.Qt.PenStyle.NoPen)
            painter.setBrush(dots)
            for y in range(3, 55, 6):
                for x in range(int(right) - 96, int(right), 6):
                    radius = 0.7 + 1.1 * (x - right + 96) / 96
                    painter.drawEllipse(q.QtCore.QPointF(x + (y % 12) / 2, y), radius, radius)
            painter.restore()
            painter.end()

    card = PaperCard()
    card.setObjectName('card')
    card.setProperty('site', True)
    card.setProperty('appearance', 'neutral')
    shadow = q.QtWidgets.QGraphicsDropShadowEffect(card)
    shadow.setBlurRadius(20)
    shadow.setOffset(0, 5)
    shadow.setColor(q.QtGui.QColor(0, 0, 0, 75))
    card.setGraphicsEffect(shadow)
    return card


def draw_symbol(q, painter, name, colour, rect, step=0):
    """A small shared outline vocabulary, painted sharply at any display scale."""
    import math
    painter.save()
    painter.setRenderHint(q.QtGui.QPainter.RenderHint.Antialiasing)
    painter.translate(rect.x(), rect.y())
    painter.scale(rect.width() / 24, rect.height() / 24)
    pen = q.QtGui.QPen(q.QtGui.QColor(colour), 1.8)
    pen.setCapStyle(q.QtCore.Qt.PenCapStyle.RoundCap)
    pen.setJoinStyle(q.QtCore.Qt.PenJoinStyle.RoundJoin)
    painter.setPen(pen)
    painter.setBrush(q.QtCore.Qt.BrushStyle.NoBrush)
    path = q.QtGui.QPainterPath()
    if name == 'clock':
        painter.drawEllipse(q.QtCore.QRectF(3, 3, 18, 18))
        path.moveTo(12, 6); path.lineTo(12, 12); path.lineTo(16, 14)
    elif name == 'storage':
        painter.drawEllipse(q.QtCore.QRectF(4, 3, 16, 6))
        path.moveTo(4, 6); path.lineTo(4, 18); path.cubicTo(4, 22, 20, 22, 20, 18); path.lineTo(20, 6)
        path.moveTo(4, 12); path.cubicTo(4, 16, 20, 16, 20, 12)
    elif name == 'folder':
        path.moveTo(3, 20); path.lineTo(3, 5); path.lineTo(9, 5); path.lineTo(12, 8)
        path.lineTo(21, 8); path.lineTo(21, 20); path.closeSubpath()
    elif name == 'dashboard':
        for x, y in ((4, 4), (14, 4), (4, 14), (14, 14)):
            painter.drawRoundedRect(q.QtCore.QRectF(x, y, 6, 6), 1, 1)
    elif name == 'library':
        path.moveTo(12, 6); path.cubicTo(8, 3, 5, 3, 2, 4); path.lineTo(2, 20)
        path.cubicTo(6, 18, 9, 19, 12, 21); path.cubicTo(15, 19, 18, 18, 22, 20)
        path.lineTo(22, 4); path.cubicTo(19, 3, 16, 3, 12, 6); path.lineTo(12, 21)
    elif name == 'settings':
        for index in range(8):
            angle = index * math.pi / 4
            painter.drawLine(q.QtCore.QPointF(12 + 8 * math.cos(angle), 12 + 8 * math.sin(angle)),
                             q.QtCore.QPointF(12 + 10 * math.cos(angle), 12 + 10 * math.sin(angle)))
        painter.drawEllipse(q.QtCore.QRectF(5, 5, 14, 14))
        painter.drawEllipse(q.QtCore.QRectF(9, 9, 6, 6))
    elif name == 'pause':
        painter.setBrush(q.QtGui.QColor(colour))
        for x in (6, 15):
            painter.drawRoundedRect(q.QtCore.QRectF(x, 4, 3, 16), 0.8, 0.8)
    elif name == 'play':
        path.moveTo(7, 4); path.lineTo(20, 12); path.lineTo(7, 20); path.closeSubpath()
        painter.setBrush(q.QtGui.QColor(colour))
    elif name == 'check':
        painter.drawEllipse(q.QtCore.QRectF(2, 2, 20, 20))
        path.moveTo(7, 12); path.lineTo(11, 16); path.lineTo(17, 9)
    elif name == 'filter':
        for y, x in ((5, 16), (12, 8), (19, 14)):
            painter.drawLine(3, y, 21, y)
            painter.drawEllipse(q.QtCore.QRectF(x - 2, y - 2, 4, 4))
    elif name == 'spinner':
        painter.setPen(q.QtCore.Qt.PenStyle.NoPen)
        for index in range(10):
            angle = index * math.pi / 5
            ink = q.QtGui.QColor(colour)
            ink.setAlpha(55 + 20 * ((index - step) % 10))
            painter.setBrush(ink)
            painter.drawEllipse(q.QtCore.QPointF(12 + 8 * math.cos(angle), 12 + 8 * math.sin(angle)), 1.6, 1.6)
    else:
        painter.drawEllipse(q.QtCore.QRectF(3, 3, 18, 18))
        path.moveTo(12, 7); path.lineTo(12, 13)
        painter.drawPoint(q.QtCore.QPointF(12, 17))
    painter.drawPath(path)
    painter.restore()


def icon(q, name, colour, size=20, gap=0):
    """A symbol as a button's picture. gap is clear room after it, to part it from the button's words: the
    button's icon size is then (size + gap, size)."""
    pixmap = q.QtGui.QPixmap((size + gap) * 2, size * 2)
    pixmap.fill(q.QtCore.Qt.GlobalColor.transparent)
    painter = q.QtGui.QPainter(pixmap)
    draw_symbol(q, painter, name, colour, q.QtCore.QRectF(0, 0, size * 2, size * 2))
    painter.end()
    pixmap.setDevicePixelRatio(2)
    return q.QtGui.QIcon(pixmap)


def symbol_label(q, text, name, size=18):
    class SymbolLabel(q.QtWidgets.QLabel):
        def __init__(self):
            super().__init__(text)
            self.symbol, self.step = name, 0
            self.setContentsMargins(size + 6, 0, 0, 0)
            self.timer = q.QtCore.QTimer(self)
            self.timer.setInterval(100)
            self.timer.timeout.connect(self.advance)

        def advance(self):
            self.step = (self.step + 1) % 10
            self.update()

        def setSymbol(self, value):
            self.symbol = value
            self.timer.start() if value == 'spinner' and self.isVisible() else self.timer.stop()
            self.update()

        def showEvent(self, event):
            super().showEvent(event)
            self.setSymbol(self.symbol)

        def hideEvent(self, event):
            self.timer.stop()
            super().hideEvent(event)

        def paintEvent(self, event):
            super().paintEvent(event)
            painter = q.QtGui.QPainter(self)
            colour = self.palette().color(q.QtGui.QPalette.ColorRole.WindowText)
            draw_symbol(q, painter, self.symbol, colour, q.QtCore.QRectF(0, (self.height() - size) / 2, size, size), self.step)
            painter.end()

    return SymbolLabel()


def paper_mark(q):
    class PaperMark(q.QtWidgets.QWidget):
        def paintEvent(self, event):
            painter = q.QtGui.QPainter(self)
            painter.setRenderHint(q.QtGui.QPainter.RenderHint.Antialiasing)
            painter.scale(self.width() / 36, self.height() / 44)
            colour = q.QtGui.QColor(card_colours(self.parentWidget().property('appearance'),
                                               bool(self.window().property('comicDark')))['accent'])
            painter.setPen(q.QtCore.Qt.PenStyle.NoPen)
            for points, alpha in ((((4, 15), (29, 4), (25, 24), (0, 35)), 255),
                                  (((13, 25), (35, 15), (31, 34), (9, 43)), 140)):
                colour.setAlpha(alpha)
                polygon = q.QtGui.QPolygonF([q.QtCore.QPointF(x, y) for x, y in points])
                painter.setBrush(q.QtGui.QColor(0, 0, 0, 65))
                painter.drawPolygon(q.QtGui.QPolygonF([q.QtCore.QPointF(x + 1, y + 2) for x, y in points]))
                face = q.QtGui.QLinearGradient(0, 5, 30, 42)
                face.setColorAt(0, colour.lighter(120))
                face.setColorAt(1, colour)
                painter.setBrush(q.QtGui.QBrush(face))
                painter.drawPolygon(polygon)
            painter.end()
    mark = PaperMark()
    mark.setFixedSize(30, 38)
    mark.setAttribute(q.QtCore.Qt.WidgetAttribute.WA_TransparentForMouseEvents)
    return mark


def brand_mark(q):
    """The app's own mark beside its name: an open box of pages."""
    class BrandMark(q.QtWidgets.QWidget):
        def paintEvent(self, event):
            painter = q.QtGui.QPainter(self)
            painter.setRenderHint(q.QtGui.QPainter.RenderHint.Antialiasing)
            painter.scale(self.width() / 40, self.height() / 40)
            painter.setPen(q.QtCore.Qt.PenStyle.NoPen)
            for points, colour in ((((3, 14), (20, 20), (20, 38), (3, 31)), '#f26a55'),
                                   (((20, 20), (37, 14), (37, 31), (20, 38)), '#c9483a'),
                                   (((3, 14), (20, 8), (37, 14), (20, 20)), '#8f2f29'),
                                   (((8, 10), (26, 2), (34, 7), (16, 15)), '#fbe3cf'),
                                   (((16, 15), (34, 7), (35, 10), (17, 18)), '#e9b99f')):
                painter.setBrush(q.QtGui.QColor(colour))
                painter.drawPolygon(q.QtGui.QPolygonF([q.QtCore.QPointF(x, y) for x, y in points]))
            painter.end()
    mark = BrandMark()
    mark.setFixedSize(34, 34)
    mark.setAttribute(q.QtCore.Qt.WidgetAttribute.WA_TransparentForMouseEvents)
    return mark

