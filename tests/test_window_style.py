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
    def test_light_and_dark_each_give_every_colour(self):
        light, dark = window_style.colours(False), window_style.colours(True)
        self.assertEqual(set(light), set(window_style.COLOURS))
        self.assertEqual(light['page'], '#f4f6f2')
        self.assertEqual(dark['page'], '#111918')
        self.assertTrue(all(re.fullmatch(r'#[0-9a-f]{6}', value) for value in [*light.values(), *dark.values()]))

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
