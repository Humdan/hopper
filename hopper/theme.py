"""Terminal styling for the hop CLI: the Hopper yellow, the rabbit, and honest fallbacks.

Zero dependencies. Color follows the usual rules: NO_COLOR turns it off, FORCE_COLOR turns it on, and
nothing is colored when output isn't a terminal. Truecolor is used where the terminal says it can, with a
256-color and a basic-16 fallback, so the yellow never turns into something else.
"""
from __future__ import annotations

import os
import sys

YELLOW = (255, 214, 10)      # #FFD60A, the brand yellow (docs/brand)
LIGHT = (255, 232, 102)      # top of the gradient
AMBER = (240, 178, 0)        # bottom of the gradient

# The mark from docs/brand/hopper-mark-yellow.svg, rendered into braille dots (2x4 per cell).
RABBIT = (
    '   ⣴⣿⡆    ⢠⣾⣷⡀',
    ' ⢀⣾⣿⣿⣷    ⣸⣿⣿⣷⡄',
    ' ⣾⣿⠋⣿⣿⡄   ⣿⣿⠉⣿⣿',
    ' ⢹⣿⡇⢸⣿⡇  ⢸⣿⡟⢀⣿⣿',
    ' ⢸⣿⣧⠈⣿⣿  ⣼⣿⠇⢸⣿⡇',
    '  ⣿⣿ ⢻⣿⡆⢀⣿⣿ ⣾⣿⠃',
    '  ⢻⣿⡆⠸⣿⣿⣿⣿⡇⢀⣿⣿',
    ' ⢰⣿⡿⠃ ⠈⠁ ⠉ ⠈⢻⣿⣇',
    '⢀⣿⣿⠁         ⢿⣿⡄',
    '⣼⣿⠇          ⠈⣿⣿',
    '⢻⣿⣇          ⢰⣿⡿',
    ' ⢻⣿⣆        ⢠⣿⡿⠁',
    ' ⠈⠿⣿⣷⣦⣤⣀⣀⣠⣴⣾⣿⡿⠁',
    '    ⠉⠛⠿⢿⣿⠿⠛⠉⠁',
)

RABBIT_MINI = (
    ' ⣴⣷  ⣾⣷⡀',
    '⢸⣿⣿⡇⢠⣿⢻⡇',
    '⠈⣿⣸⣷⣸⡟⣿⡇',
    '⢀⣿⠏⠻⠻⠣⣿⡄',
    '⣼⡏    ⠸⣷',
    '⢹⣧    ⣰⡟',
    ' ⠙⠻⢷⡶⠿⠛⠁',
)


_CUBE = (0, 95, 135, 175, 215, 255)   # xterm 256-color cube channel levels


def level(stream=None) -> int:
    """0 = no color, 1 = basic 16, 2 = 256 colors, 3 = truecolor."""
    stream = stream or sys.stdout
    if os.environ.get("NO_COLOR"):
        return 0
    forced = os.environ.get("FORCE_COLOR")
    if not forced and not (hasattr(stream, "isatty") and stream.isatty()):
        return 0
    term = os.environ.get("TERM", "")
    if term == "dumb" and not forced:
        return 0
    if os.environ.get("COLORTERM", "").lower() in ("truecolor", "24bit"):
        return 3
    if "256" in term or os.environ.get("TERM_PROGRAM") in ("iTerm.app", "Apple_Terminal", "vscode"):
        return 3 if os.environ.get("TERM_PROGRAM") in ("iTerm.app", "vscode") else 2
    return 1


def _fg(rgb, lvl: int) -> str:
    r, g, b = rgb
    if lvl >= 3:
        return f"\x1b[38;2;{r};{g};{b}m"
    if lvl == 2:
        # nearest entry in the 6x6x6 cube, whose channel levels are not evenly spaced
        idx = [min(range(6), key=lambda i: abs(_CUBE[i] - v)) for v in (r, g, b)]
        return f"\x1b[38;5;{16 + 36 * idx[0] + 6 * idx[1] + idx[2]}m"
    return "\x1b[93m"


def paint(text: str, rgb=YELLOW, bold: bool = False, dim: bool = False, stream=None) -> str:
    lvl = level(stream)
    if not lvl:
        return text
    pre = ("\x1b[1m" if bold else "") + ("\x1b[2m" if dim else "") + (_fg(rgb, lvl) if rgb else "")
    return f"{pre}{text}\x1b[0m"


def yellow(text: str) -> str:
    return paint(text)


def heading(text: str) -> str:
    return paint(text, bold=True)


def cmd(text: str) -> str:
    """A command the reader should type: bold, in the default text color."""
    return paint(text, rgb=None, bold=True)


def dim(text: str) -> str:
    return paint(text, rgb=None, dim=True)


def ok(text: str) -> str:
    return paint("✓ ", bold=True) + text


def _mix(a, b, t):
    return tuple(round(x + (y - x) * t) for x, y in zip(a, b))


def _utf8(stream=None) -> bool:
    return "utf" in (getattr(stream or sys.stdout, "encoding", "") or "").lower()


def rabbit(mini: bool = False) -> list[str]:
    """The rabbit, one string per row, shaded from light yellow at the ears to amber at the chin."""
    if not _utf8():
        return []
    art = RABBIT_MINI if mini else RABBIT
    n = max(1, len(art) - 1)
    return [paint(row, _mix(LIGHT, AMBER, i / n), bold=True) for i, row in enumerate(art)]


def banner(version: str, tagline: str = "a durable job queue for AI agents", mini: bool = False) -> str:
    """The rabbit with the name beside it. Plain text when there's no color or no UTF-8."""
    art = rabbit(mini)
    plain_w = max((len(r) for r in (RABBIT_MINI if mini else RABBIT)), default=0)
    text = {
        len(art) // 2 - 1: paint("H O P P E R", bold=True),
        len(art) // 2: dim(tagline),
        len(art) // 2 + 1: dim("v" + version),
    }
    if not art:
        return paint("HOPPER", bold=True) + "  " + dim(tagline + "  v" + version)
    src = RABBIT_MINI if mini else RABBIT
    rows = []
    for i, row in enumerate(art):
        pad = " " * (plain_w - len(src[i]) + 3)
        rows.append(row + pad + text.get(i, ""))
    return "\n".join(rows)


def columns(rows, indent: int = 2, gap: int = 3, bold_right=None) -> list[str]:
    """Two aligned columns. Pads on the visible text, so color codes never throw the alignment off.

    By default the left column is the command (bold) and the right is its description (dim); with
    `bold_right` = indexes of rows whose right side is a command instead, and the left is plain text."""
    width = max(len(left) for left, _ in rows)
    out = []
    for i, (left, right) in enumerate(rows):
        pad = " " * (width - len(left) + gap)
        if bold_right is None:
            out.append(" " * indent + cmd(left) + pad + dim(right))
        else:
            out.append(" " * indent + left + pad + (cmd(right) if i in bold_right else dim(right)))
    return out
