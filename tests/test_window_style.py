"""The window's look: one style sheet for light and dark, and the names the pages give their widgets."""
import ast
from pathlib import Path
import re
import unittest

from comic_archive import window_pages, window_style


def used_names():
    """The objectNames, button kinds and tones window_pages gives its widgets, read from its source."""
    tree = ast.parse(Path(window_pages.__file__).read_text('utf-8'))
    names, kinds, tones = set(), set(), set()

    def texts(node):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return {node.value}
        return texts(node.body) | texts(node.orelse) if isinstance(node, ast.IfExp) else set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        called = node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, 'id', None)
        if called == 'label' and len(node.args) > 1:
            names |= texts(node.args[1])
        elif called in ('frame', 'setObjectName') and node.args:
            names |= texts(node.args[0])
        elif called == 'button' and len(node.args) > 2:
            kinds |= texts(node.args[2])
        elif called == 'tone' and len(node.args) > 2 and texts(node.args[1]) == {'tone'}:
            tones |= texts(node.args[2])
    return names, kinds, tones - {''}


class StyleTests(unittest.TestCase):
    def test_provider_text_and_primary_buttons_have_readable_contrast_in_both_themes(self):
        def luminance(colour):
            rgb = [int(colour[i:i + 2], 16) / 255 for i in (1, 3, 5)]
            channels = [c / 12.92 if c <= .04045 else ((c + .055) / 1.055) ** 2.4 for c in rgb]
            return sum(c * weight for c, weight in zip(channels, (.2126, .7152, .0722), strict=True))
        for dark in (False, True):
            for appearance in window_style.CARD_STATES:
                c = window_style.card_colours(appearance, dark)
                for text, background in (('text', 'surface'), ('text', 'shade'), ('button-text', 'accent')):
                    a, b = luminance(c[text]), luminance(c[background])
                    self.assertGreaterEqual((max(a, b) + .05) / (min(a, b) + .05), 4.5,
                                            (appearance, dark, text, background))

    def test_light_and_dark_each_give_every_colour(self):
        light, dark = window_style.colours(False), window_style.colours(True)
        self.assertEqual(set(light), set(window_style.COLOURS))
        self.assertEqual(light['page'], '#f8f5ef')
        self.assertEqual(dark['page'], '#211c1d')
        self.assertTrue(all(re.fullmatch(r'#[0-9a-f]{6}', value) for value in [*light.values(), *dark.values()]))

    def test_destructive_text_and_outline_contrast_with_normal_and_hover_backgrounds(self):
        def luminance(colour):
            channels = [int(colour[i:i + 2], 16) / 255 for i in (1, 3, 5)]
            linear = [v / 12.92 if v <= 0.04045 else ((v + 0.055) / 1.055) ** 2.4 for v in channels]
            return sum(v * weight for v, weight in zip(linear, (0.2126, 0.7152, 0.0722), strict=True))
        for dark in (False, True):
            colours = window_style.colours(dark)
            foreground = luminance(colours['destructive'])
            for background in ('surface', 'destructive-hover'):
                other = luminance(colours[background])
                ratio = (max(foreground, other) + 0.05) / (min(foreground, other) + 0.05)
                self.assertGreaterEqual(ratio, 4.5, (dark, background))

    def test_the_sheet_is_filled_in_completely_for_both_looks(self):
        for dark in (False, True):
            sheet = window_style.stylesheet(dark)
            self.assertNotIn('%(', sheet)
            self.assertIn(window_style.colours(dark)['accent'], sheet)
            self.assertEqual(sheet.count('{'), sheet.count('}'))

    def test_the_tick_and_arrow_pictures_ship_for_both_looks_and_sharp_screens(self):
        for name in ('check', 'arrow'):
            for dark in (False, True):
                path = window_style.picture(name, dark)
                self.assertNotIn('\\', path)
                self.assertIn(path, window_style.stylesheet(dark))
                self.assertTrue(path.endswith(f"{name}-{'dark' if dark else 'light'}.png"))
                for picture in (Path(path), Path(path[:-len('.png')] + '@2x.png')):
                    self.assertEqual(picture.read_bytes()[:8], b'\x89PNG\r\n\x1a\n')

    def test_every_name_kind_and_tone_the_pages_use_has_a_look(self):
        names, kinds, tones = used_names()
        self.assertTrue({'card', 'pill', 'header', 'muted'} <= names)
        self.assertEqual([name for name in sorted(names) if '#' + name not in window_style.SHEET], [])
        self.assertEqual([kind for kind in sorted(kinds) if f'[kind="{kind}"]' not in window_style.SHEET], [])
        self.assertEqual([tone for tone in sorted(tones) if f'[tone="{tone}"]' not in window_style.SHEET], [])
        # The models' own tones, put on the pills, the B2 state and the badges; 'neutral' is the plain look.
        for tone in ('success', 'warning', 'working'):
            self.assertIn(f'QLabel#pill[tone="{tone}"]', window_style.SHEET)
        for tone in ('success', 'warning'):
            self.assertIn(f'QFrame#state[tone="{tone}"]', window_style.SHEET)
        self.assertIn('QLabel#badge[tone="warning"]', window_style.SHEET)


if __name__ == '__main__':
    unittest.main()
