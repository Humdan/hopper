import io
import os
import unittest
from unittest import mock

from hopper import theme


class Tty(io.StringIO):
    def isatty(self):
        return True


class Color(unittest.TestCase):
    def env(self, **kw):
        base = {k: v for k, v in os.environ.items() if k not in ("NO_COLOR", "FORCE_COLOR", "COLORTERM", "TERM", "TERM_PROGRAM")}
        return mock.patch.dict(os.environ, {**base, **kw}, clear=True)

    def test_off_when_not_a_terminal(self):
        with self.env(TERM="xterm-256color"):
            self.assertEqual(theme.level(io.StringIO()), 0)

    def test_no_color_wins_even_when_forced(self):
        with self.env(NO_COLOR="1", FORCE_COLOR="1"):
            self.assertEqual(theme.level(Tty()), 0)
            with mock.patch("sys.stdout", Tty()):
                self.assertEqual(theme.paint("x"), "x")

    def test_levels(self):
        with self.env(TERM="xterm-256color", COLORTERM="truecolor"):
            self.assertEqual(theme.level(Tty()), 3)
        with self.env(TERM="xterm-256color"):
            self.assertEqual(theme.level(Tty()), 2)
        with self.env(TERM="vt100"):
            self.assertEqual(theme.level(Tty()), 1)
        with self.env(TERM="dumb"):
            self.assertEqual(theme.level(Tty()), 0)

    def test_brand_yellow_in_truecolor_and_a_yellow_everywhere_else(self):
        self.assertIn("38;2;255;214;10", theme._fg(theme.YELLOW, 3))
        self.assertEqual(theme._fg(theme.YELLOW, 1), "\x1b[93m")
        # 256 colors: the brand yellow is cube entry 220 (#FFD700); the gradient ends are 221 and 214
        pick = lambda rgb: int(theme._fg(rgb, 2).split(";")[-1].rstrip("m"))
        self.assertEqual(pick(theme.YELLOW), 220)
        self.assertEqual(pick(theme.LIGHT), 221)
        self.assertEqual(pick(theme.AMBER), 214)

    def test_banner_plain_has_the_name_and_version(self):
        with self.env():
            out = theme.banner("9.9.9")
        self.assertIn("H O P P E R", out)
        self.assertIn("v9.9.9", out)
        self.assertNotIn("\x1b", out)

    def test_banner_without_utf8_drops_the_art_not_the_text(self):
        with self.env(), mock.patch("sys.stdout", io.TextIOWrapper(io.BytesIO(), encoding="ascii")):
            out = theme.banner("1.0")
        self.assertNotIn("⣿", out)
        self.assertIn("HOPPER", out)

    def test_columns_align_on_visible_text(self):
        with self.env(FORCE_COLOR="1", COLORTERM="truecolor"), mock.patch("sys.stdout", Tty()):
            rows = theme.columns([("a", "one"), ("longer", "two")])
        import re
        plain = [re.sub(r"\x1b\[[0-9;]*m", "", r) for r in rows]
        self.assertEqual(plain[0].index("one"), plain[1].index("two"))

    def test_rabbit_art_is_consistent(self):
        for art in (theme.RABBIT, theme.RABBIT_MINI):
            self.assertGreaterEqual(len(art), 7)
            self.assertTrue(all(set(r) <= {" "} | {chr(c) for c in range(0x2800, 0x2900)} for r in art))
