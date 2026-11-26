"""Lyrics layout for the song display (app/songs.py).

The service lays a song out; the display agents and the web page only draw
the lines at the size they are given. So every screen, the page preview and
the verse keys count the same slides. Widths come from the display PC (the
font's character widths, measured by its agent: POST /song/metrics) or, for
an agent too old for that, from an estimate (ESTIMATE).

Rules, per screen:

1. One text size for every slide of the song: the set size (font_size, in px
   on a 1080 px high screen) if the song fits.
2. A line of the file wider than the screen is wrapped: as few lines as
   possible, breaking preferably after a sentence end, then after a comma,
   semicolon, colon or dash, and only then between other words; lines come
   out roughly even, and repeat marks (|: :|) stay with their word. Lines of
   the file are never joined.
3. If a slide does not fit, the whole song gets smaller, but at most to
   SHRINK_LIMIT of the set size (never below the readable minimum).
4. A slide that still does not fit is split into as few slides as needed:
   at a line break of the file (best at an empty line or after a sentence
   end), inside a long line only where there is none (sentence end, comma,
   ..., a space as last resort), the parts roughly balanced. Then the song's
   one size is worked out again for the new slides.
5. The readable minimum (SLIDE_MIN_PX) is the last resort, for a slide that
   cannot be split (the title slide, a word wider than the screen): that
   slide alone is drawn at most that small, cut at the bottom and marked
   "overflow".

Several screens: the song is split first for the screen that needs the most
slides, then further wherever another screen still needs it, so all screens
show the same slides (the verse keys stay in step) and none goes below its
own shrink limit for a slide that can be split; each screen then gets its
own one size for them.

Speed: widths add up per character, wrapping that cannot fit is ruled out
by a quick greedy count before the search for nicer breaks, and results are
kept per Metrics object (options_memo, lines_memo): 300 long verses on three
screens take about half a second, and a second time a few milliseconds.

Constants shared with the drawing code: keep SLIDE_* and PREVIEW_* in sync
with desktop_agent/show_image_agent.py and SLIDE/PREVIEW in
app/static/js/songs.js (tests/test_slide_parity.py compares them).
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional

# Margins, title and copyright rows (fractions of the screen) and line height.
SLIDE_PAD_X, SLIDE_PAD_Y = 0.06, 0.07
SLIDE_TITLE_H, SLIDE_COPY_H = 0.12, 0.08
SLIDE_LINE = 1.25
# Smallest lyrics size in px on a 1080 px high screen: smaller is unreadable
# from the back. Only for slides that cannot be split (rule 5).
SLIDE_MIN_PX = 40
# A song gets at most 18 % smaller than the set size before a verse is split
# into two slides (rule 3); 0.82 keeps it clearly readable and "the same
# size" for the eye.
SHRINK_LIMIT = 0.82
# Sizes tried, from the set size down: each 6 % smaller than the one before.
SIZE_STEP = 0.94
# Next-slide preview: half the text size, at least PREVIEW_MIN px on 1080
# lines, under the text with a gap of half a line.
PREVIEW_SCALE, PREVIEW_MIN, PREVIEW_GAP = 0.5, 20, 0.5
# At most this many slides from one verse (a whole song pasted into one
# verse would otherwise become dozens of slides).
MAX_PARTS = 12

REPEAT_OPEN, REPEAT_CLOSE = "|:", ":|"
# Widths are measured at this size and scaled (fonts scale linearly).
REF_PX = 100

# Cost of a line break, by what ends the line before it (rule 2).
_SENTENCE_END = re.compile(r"[.!?…](?:[\"'»«“”‘’)\]]*)?(?:\s*:\|)?$")
_CLAUSE_END = re.compile(r"[,;:–—-](?:[\"'»«“”‘’)\]]*)?(?:\s*:\|)?$")
BREAK_COST = {"sentence": 0.0, "clause": 0.15, "word": 0.5}
# A line ending in a short word ("und", "die", "zu") reads badly: that word
# goes to the next line.
SHORT_WORD, SHORT_WORD_COST = 3, 0.15
# A line more is worth a break at a sentence or clause end instead of in the
# middle of a phrase, as long as the slide still fits (else: fewest lines).
LINE_COST = 0.15
# Weight of a line's empty end (lines of even length look calmer).
SLACK_WEIGHT = 0.5
# Cost of a split between slides (rule 4) by what ends the first part.
SPLIT_COST = {"stanza": 0.0, "sentence": 0.1, "clause": 0.4, "word": 1.0, "inside": 2.0}
# Weight of uneven parts against a better split point.
BALANCE_WEIGHT = 1.0


def _ending(text: str) -> str:
    text = text.rstrip()
    if _SENTENCE_END.search(text):
        return "sentence"
    if _CLAUSE_END.search(text):
        return "clause"
    return "word"


# ---- measuring ---------------------------------------------------------------

# Character widths of Verdana (the default font) in px at REF_PX, rounded:
# the estimate for display agents that cannot measure (older versions).
_ESTIMATE_WIDTHS = {
    " ": 35, "a": 61, "b": 62, "c": 52, "d": 62, "e": 59, "f": 35, "g": 62, "h": 63, "i": 27,
    "j": 34, "k": 59, "l": 27, "m": 97, "n": 63, "o": 60, "p": 62, "q": 62, "r": 43, "s": 52,
    "t": 39, "u": 63, "v": 59, "w": 82, "x": 59, "y": 59, "z": 51, "ä": 61, "ö": 60, "ü": 63,
    "ß": 62, "A": 68, "B": 69, "C": 70, "D": 77, "E": 63, "F": 58, "G": 77, "H": 75, "I": 42,
    "J": 45, "K": 69, "L": 56, "M": 84, "N": 75, "O": 79, "P": 60, "Q": 79, "R": 70, "S": 69,
    "T": 61, "U": 73, "V": 68, "W": 99, "X": 69, "Y": 61, "Z": 69, "Ä": 68, "Ö": 79, "Ü": 73,
    ".": 36, ",": 36, ":": 45, ";": 45, "!": 39, "?": 55, "'": 27, '"': 46, "-": 42, "(": 45,
    ")": 45, "|": 45, "/": 45, "„": 46, "“": 46, "”": 46, "–": 60, "—": 100,
}
_ESTIMATE_DIGIT, _ESTIMATE_DEFAULT = 64, 62


class Metrics:
    """Text widths in one font: character widths at REF_PX, summed (the
    display agent measures each character in its font; kerning is left out,
    which Windows does not apply to plain text either)."""

    def __init__(self, widths: Dict[str, float], default: float = _ESTIMATE_DEFAULT, source: str = "agent"):
        self.widths = dict(widths)
        self.default = default
        self.source = source
        self._cache: Dict[str, float] = {}
        # Layouts worked out with these widths (the same paragraph at the same
        # size comes up again and again: sizes tried, splits, later layouts).
        self.options_memo: Dict[tuple, dict] = {}
        self.lines_memo: Dict[tuple, List[str]] = {}

    def ref_width(self, text: str) -> float:
        width = self._cache.get(text)
        if width is None:
            width = sum(self.widths.get(c, self.default) for c in text)
            if len(self._cache) < 50_000:
                self._cache[text] = width
        return width

    def width(self, text: str, px: float) -> float:
        return self.ref_width(text) * px / REF_PX


def estimate_metrics() -> Metrics:
    widths = dict(_ESTIMATE_WIDTHS)
    widths.update({d: _ESTIMATE_DIGIT for d in "0123456789"})
    return Metrics(widths, _ESTIMATE_DEFAULT, source="estimate")


@dataclass
class Screen:
    """A screen to lay out for: its window size and the font's widths there."""
    metrics: Metrics
    width: int
    height: int


@dataclass
class SlideInput:
    """A slide as the song gives it."""
    text: str
    title: bool = False          # the title row is on this slide
    copyright: bool = False      # the copyright row is on this slide
    splittable: bool = True      # the title slide is never split
    key: str = ""


@dataclass
class ShownSlide:
    """A slide as shown (after splitting): source is the index of the slide
    it came from, part/parts its piece (1/1 when not split)."""
    text: str
    title: bool
    copyright: bool
    source: int
    part: int = 1
    parts: int = 1
    key: str = ""
    splittable: bool = True


@dataclass
class ScreenLayout:
    width: int
    height: int
    px: Optional[float]                       # the song's one size (None: no slide fits)
    slides: List[dict] = field(default_factory=list)   # {px, lines, overflow, preview}


def slide_box(width: float, height: float, title: bool, copyright: bool):
    """(box_w, box_h) of the lyrics: the screen less margins, title and copyright rows."""
    box_w = width - 2 * SLIDE_PAD_X * width
    box_h = (height - 2 * SLIDE_PAD_Y * height - (SLIDE_TITLE_H * height if title else 0)
             - (SLIDE_COPY_H * height if copyright else 0))
    return box_w, box_h


# ---- line breaks (rule 2) -----------------------------------------------------

def _next_word(words: List[str], start: int) -> int:
    while start < len(words) and words[start] == "":
        start += 1
    return start


def word_units(paragraph: str) -> List[str]:
    """The paragraph's words; a repeat mark is joined to its neighbour so a
    line break never separates them: "|:" to the word after it, ":|" to the
    one before, also across double spaces; a mark with no word on that side
    takes the one on the other side. Joined with spaces the units give the
    paragraph back."""
    words = paragraph.split(" ")
    units: List[str] = []
    i = 0
    while i < len(words):
        word = words[i]
        if word in (REPEAT_OPEN, REPEAT_CLOSE):
            forward = _next_word(words, i + 1)
            back = len(units)
            while back > 0 and units[back - 1] == "":
                back -= 1
            if word == REPEAT_OPEN and forward < len(words) or word == REPEAT_CLOSE and back == 0 and forward < len(words):
                units.append(" ".join(words[i:forward + 1]))
                i = forward + 1
                continue
            if back > 0:
                units[back - 1:] = [" ".join(units[back - 1:] + [word])]
                i += 1
                continue
        units.append(word)
        i += 1
    return units


def _letter_wrap(metrics: Metrics, paragraph: str, px: float, width: float) -> List[str]:
    """Greedy wrapping that also breaks words wider than a line between
    letters (only for paragraphs with such a word). Widths add up (the
    character widths are summed), so a line's width is kept as it grows."""
    out, line, line_w = [], "", 0.0
    space = metrics.width(" ", px)
    for word in word_units(paragraph):
        word_w = metrics.width(word, px)
        if not line or line_w + space + word_w <= width:
            line, line_w = (word, word_w) if not line else (f"{line} {word}", line_w + space + word_w)
        else:
            out.append(line)
            line, line_w = word, word_w
        if line_w <= width:
            continue
        while len(line) > 1 and metrics.width(line, px) > width:
            cut = next((k for k in range(len(line) - 1, 0, -1) if metrics.width(line[:k], px) <= width), 1)
            out.append(line[:cut])
            line = line[cut:]
        line_w = metrics.width(line, px)
    out.append(line)
    return out


# A paragraph may get at most this many lines more than it needs, for better breaks.
EXTRA_LINES = 3


MEMO_MAX = 50_000


def _paragraph_options(metrics: Metrics, paragraph: str, px: float, width: float) -> Dict[int, tuple]:
    """{number of lines: (cost, lines)} of one line of the file: itself if
    it fits; else, for the fewest lines and a few more, the breaks with the
    lowest cost (after sentence ends, then clause ends, even line lengths,
    no lonely last word)."""
    key = (paragraph, px, width)
    found = metrics.options_memo.get(key)
    if found is None:
        found = _paragraph_options_uncached(metrics, paragraph, px, width)
        if len(metrics.options_memo) >= MEMO_MAX:
            metrics.options_memo.clear()
        metrics.options_memo[key] = found
    return found


def _paragraph_options_uncached(metrics: Metrics, paragraph: str, px: float, width: float) -> Dict[int, tuple]:
    if metrics.width(paragraph, px) <= width:
        return {1: (0.0, [paragraph])}
    units = word_units(paragraph)
    widths = [metrics.width(unit, px) for unit in units]
    if any(w > width for w in widths):
        lines = _letter_wrap(metrics, paragraph, px, width)
        return {len(lines): (0.0, lines)}
    space = metrics.width(" ", px)
    m = len(units)
    prefix = [0.0]
    for w in widths:
        prefix.append(prefix[-1] + w)
    # Line counts beyond the fewest (greedy filling) + EXTRA_LINES are of no
    # use: a state that cannot finish within that is dropped. The rest of the
    # paragraph needs at least its width / the line width more lines.
    most = len(_letter_wrap(metrics, paragraph, px, width)) + EXTRA_LINES
    rest = [math.ceil((prefix[m] - prefix[j]) / width - 1e-9) for j in range(m + 1)]
    # Cost of a break after each word (by its punctuation; short words cost more).
    break_cost = []
    for unit in units:
        ending = _ending(unit)
        break_cost.append(BREAK_COST[ending] + (SHORT_WORD_COST if ending == "word" and len(unit) <= SHORT_WORD else 0.0))
    # best[j][n]: cost of units[:j] in n lines ending in a break; how[j][n] the last line's start.
    best: List[Dict[int, float]] = [dict() for _ in range(m + 1)]
    how: List[Dict[int, int]] = [dict() for _ in range(m + 1)]
    best[0][0] = 0.0
    for j in range(1, m + 1):
        if j < m and units[j] == "":
            continue  # never start a line with an empty word (double space)
        for i in range(j - 1, -1, -1):
            if i > 0 and units[i] == "":
                continue
            line_w = prefix[j] - prefix[i] + space * (j - i - 1)
            if line_w > width:
                break
            if not best[i]:
                continue
            slack = (width - line_w) / width
            # The last line may be shorter, but not a lonely word.
            cost = SLACK_WEIGHT * slack * slack * (0.5 if j == m else 1.0)
            if j < m:
                cost += break_cost[j - 1]
            for n, before in best[i].items():
                if n + 1 + rest[j] > most:
                    continue
                total = before + cost
                if total < best[j].get(n + 1, math.inf):
                    best[j][n + 1], how[j][n + 1] = total, i
    fewest = min(best[m])
    options = {}
    for n in range(fewest, fewest + EXTRA_LINES + 1):
        if n not in best[m]:
            continue
        lines, j, k = [], m, n
        while j > 0:
            i = how[j][k]
            line = " ".join(units[i:j])
            lines.append(line.lstrip(" ") if i > 0 else line)
            j, k = i, k - 1
        lines.reverse()
        lines = [line.rstrip(" ") if index < len(lines) - 1 else line for index, line in enumerate(lines)]
        options[n] = (best[m][n], lines)
    return options


def wrap_paragraph(metrics: Metrics, paragraph: str, px: float, width: float, compact: bool = False) -> List[str]:
    """The lines of one line of the file: the best breaks (a line more only
    where that buys a better break, LINE_COST); compact: as few lines as
    possible."""
    options = _paragraph_options(metrics, paragraph, px, width)
    if compact:
        return options[min(options)][1]
    return min(options.items(), key=lambda item: item[1][0] + LINE_COST * item[0])[1][1]


def _fits(lines: List[str], px: float, box_h: float) -> bool:
    return len(lines) * px * SLIDE_LINE <= box_h + 1e-6


def slide_lines(metrics: Metrics, text: str, px: float, width: float, box_h: float = math.inf) -> List[str]:
    """The lines of a slide: the best breaks of all its lines together that
    still fit into box_h (a line more for a better break only where there is
    room); where nothing fits, as few lines as possible."""
    key = (text, px, width, box_h)
    found = metrics.lines_memo.get(key)
    if found is None:
        found = _slide_lines_uncached(metrics, text, px, width, box_h)
        if len(metrics.lines_memo) >= MEMO_MAX:
            metrics.lines_memo.clear()
        metrics.lines_memo[key] = found
    return list(found)


def _greedy_lines(metrics: Metrics, text: str, px: float, width: float) -> List[str]:
    """Lines filled one after the other: as few as possible, quickly (for
    the grey preview, and to see whether a size can fit at all)."""
    lines: List[str] = []
    for paragraph in text.split("\n"):
        lines.extend([paragraph] if metrics.width(paragraph, px) <= width
                     else _letter_wrap(metrics, paragraph, px, width))
    return lines


def _slide_lines_uncached(metrics: Metrics, text: str, px: float, width: float, box_h: float) -> List[str]:
    budget = int((box_h + 1e-6) // (px * SLIDE_LINE)) if box_h != math.inf else 10 ** 6
    greedy = _greedy_lines(metrics, text, px, width)
    if len(greedy) > budget:
        # Not even the fewest lines fit (filling greedily gives the fewest):
        # no need to look for nicer breaks.
        return greedy
    options = [_paragraph_options(metrics, paragraph, px, width) for paragraph in text.split("\n")]
    # Knapsack over the lines of the file: lowest cost within the line budget.
    states = {0: (0.0, [])}   # lines used -> (cost, chosen option per paragraph)
    for o in options:
        following = {}
        for used, (cost, chosen) in states.items():
            for n, (option_cost, _lines) in o.items():
                if used + n > budget:
                    continue
                total = cost + option_cost + LINE_COST * n
                if total < following.get(used + n, (math.inf,))[0]:
                    following[used + n] = (total, chosen + [n])
        states = following
    _cost, chosen = min(states.values(), key=lambda state: state[0])
    return [line for o, n in zip(options, chosen) for line in o[n][1]]


def _sizes(max_px: float, floor_px: float) -> List[float]:
    """The sizes tried, largest first, ending exactly at floor_px."""
    sizes, px = [], max_px
    while px > floor_px + 1e-9:
        sizes.append(px)
        px *= SIZE_STEP
    sizes.append(floor_px)
    return sizes


def _ladder(max_px: float, floor_px: float, min_px: float) -> List[float]:
    """The sizes tried by a layout that may not split any more: the same
    steps as _sizes() down to floor_px (always included: the shrink limit
    itself is a size), then on down to min_px."""
    sizes = _sizes(max_px, floor_px)
    px = floor_px * SIZE_STEP
    while px > min_px + 1e-9:
        sizes.append(px)
        px *= SIZE_STEP
    if min_px < floor_px - 1e-9:
        sizes.append(min_px)
    return sizes


def _best_size(metrics: Metrics, text: str, sizes: List[float], box_w: float, box_h: float) -> Optional[float]:
    for px in sizes:
        if _fits(slide_lines(metrics, text, px, box_w, box_h), px, box_h):
            return px
    return None


# ---- splitting (rule 4) --------------------------------------------------------

@dataclass
class _Piece:
    paragraph: int    # pieces of one line of the file are joined by spaces
    text: str
    after: str        # what ends it: stanza, sentence, clause, word, inside


def _sentence_pieces(paragraph: str) -> List[str]:
    """A line of the file cut after sentence ends (the pieces joined by a space give it back)."""
    parts = re.split(r"(?<=[.!?…])\s+(?=\S)", paragraph)
    return [p for p in parts if p]


def _clause_pieces(sentence: str) -> List[str]:
    parts = re.split(r"(?<=[,;:–—])\s+(?=\S)", sentence)
    return [p for p in parts if p]


def _pieces(metrics: Metrics, text: str, px: float, box_w: float, box_h: float) -> List[_Piece]:
    """The text in pieces that each fit alone, with the split points between
    them: whole lines of the file where they fit, else sentences, clauses,
    then words."""
    paragraphs = text.split("\n")
    pieces: List[_Piece] = []

    def fits_alone(piece_text: str) -> bool:
        return _fits(slide_lines(metrics, piece_text, px, box_w, box_h), px, box_h)

    for index, paragraph in enumerate(paragraphs):
        following_empty = index + 1 < len(paragraphs) and not paragraphs[index + 1].strip()
        end = "stanza" if following_empty or not paragraph.strip() else _ending(paragraph)
        if fits_alone(paragraph):
            pieces.append(_Piece(index, paragraph, end))
            continue
        # Too long on its own: sentences, then clauses, then words.
        subs: List[str] = []
        for sentence in _sentence_pieces(paragraph):
            if fits_alone(sentence):
                subs.append(sentence)
                continue
            for clause in _clause_pieces(sentence):
                if fits_alone(clause):
                    subs.append(clause)
                else:
                    subs.extend(u for u in word_units(clause) if u)
        for k, sub in enumerate(subs):
            inner = _ending(sub)
            after = end if k == len(subs) - 1 else inner if inner != "word" else "inside"
            pieces.append(_Piece(index, sub, after))
    return pieces


def _join(pieces: List[_Piece]) -> str:
    """Pieces back into slide text: pieces of one line of the file joined by
    spaces, lines by line breaks, empty lines at the edges dropped."""
    lines: List[str] = []
    last = None
    for piece in pieces:
        if piece.paragraph == last:
            lines[-1] = f"{lines[-1]} {piece.text}"
        else:
            lines.append(piece.text)
            last = piece.paragraph
    while lines and not lines[0].strip():
        lines.pop(0)
    while lines and not lines[-1].strip():
        lines.pop()
    return "\n".join(lines)


def split_slide(metrics: Metrics, slide: SlideInput, px: float, width: float, height: float) -> List[str]:
    """The texts of the slides a verse is split into so each fits at px (the
    first part keeps the slide's title/copyright rows): as few as possible,
    at the best split points, roughly even. One text when it fits as it is."""
    first_box = slide_box(width, height, slide.title, slide.copyright)
    plain_box = slide_box(width, height, False, False)
    if _fits(slide_lines(metrics, slide.text, px, *first_box), px, first_box[1]):
        return [slide.text]
    pieces = _pieces(metrics, slide.text, px, *plain_box)
    # Empty lines at the start or end would only make empty parts.
    while pieces and not pieces[0].text.strip():
        pieces.pop(0)
    while pieces and not pieces[-1].text.strip():
        pieces.pop()
    n = len(pieces)
    if n == 0:
        return [slide.text]

    part_lines: Dict[tuple, Optional[int]] = {}

    def lines_of(i: int, j: int) -> Optional[int]:
        """Lines of pieces[i:j] as one slide: None if it does not fit, 0 if
        it has no text (empty lines only: no slide of its own, but more
        pieces may still fit with it)."""
        key = (i, j)
        if key not in part_lines:
            text = _join(pieces[i:j])
            box_w, box_h = first_box if i == 0 else plain_box
            if not text.strip():
                part_lines[key] = 0
            else:
                lines = slide_lines(metrics, text, px, box_w, box_h)
                part_lines[key] = len(lines) if _fits(lines, px, box_h) else None
        return part_lines[key]

    # Fewest parts: greedy (a part never fits better with more in it).
    count, i = 0, 0
    while i < n:
        j = i + 1
        while j < n and lines_of(i, j + 1) is not None:
            j += 1
        count += 1
        i = j
    count = min(count, MAX_PARTS)
    total = sum(len(slide_lines(metrics, p.text, px, *plain_box)) for p in pieces)
    target = max(1.0, total / count)
    # Best split into exactly `count` parts: cheap split points, even parts.
    INF = math.inf
    best = [[INF] * (count + 1) for _ in range(n + 1)]
    back = [[0] * (count + 1) for _ in range(n + 1)]
    best[0][0] = 0.0
    for j in range(1, n + 1):
        for k in range(1, count + 1):
            for i in range(j - 1, -1, -1):
                if best[i][k - 1] == INF:
                    continue
                lines = lines_of(i, j)
                if lines is None:
                    if j - i > 1:
                        break  # longer parts fit even less
                    continue
                if lines == 0:
                    continue   # empty lines only: not a slide (a longer part may be one)
                cost = best[i][k - 1] + BALANCE_WEIGHT * ((lines - target) / target) ** 2
                if j < n:
                    cost += SPLIT_COST[pieces[j - 1].after]
                if cost < best[j][k]:
                    best[j][k], back[j][k] = cost, i
    if best[n][count] == INF:
        # Some piece does not fit even alone (a gigantic word): greedy parts,
        # the slide that does not fit is cut (rule 5).
        parts, i = [], 0
        while i < n and len(parts) < MAX_PARTS - 1:
            j = i + 1
            while j < n and lines_of(i, j + 1) is not None:
                j += 1
            parts.append(_join(pieces[i:j]))
            i = j
        if i < n:
            parts.append(_join(pieces[i:]))
        return [p for p in parts if p.strip()] or [slide.text]
    bounds, j = [], n
    for k in range(count, 0, -1):
        i = back[j][k]
        bounds.append((i, j))
        j = i
    bounds.reverse()
    return [_join(pieces[i:j]) for i, j in bounds]


# ---- the song on one screen and on several (rules 1, 3, 5) ----------------------

def _limits(screen: Screen, set_px: float):
    max_px = set_px * screen.height / 1080
    min_px = min(SLIDE_MIN_PX * screen.height / 1080, max_px)
    floor_px = max(min_px, max_px * SHRINK_LIMIT)
    return max_px, min_px, floor_px


def split_song(screen: Screen, slides: List[ShownSlide], set_px: float) -> List[ShownSlide]:
    """The slides as shown on this screen: slides that do not fit even at
    the shrink limit split (rule 4); the parts keep their source. Part
    numbers are set by plan_song()."""
    max_px, _min_px, floor_px = _limits(screen, set_px)
    sizes = _sizes(max_px, floor_px)
    shown: List[ShownSlide] = []
    for slide in slides:
        box_w, box_h = slide_box(screen.width, screen.height, slide.title, slide.copyright)
        if not slide.splittable or _best_size(screen.metrics, slide.text, sizes, box_w, box_h) is not None:
            shown.append(slide)
            continue
        source = SlideInput(slide.text, slide.title, slide.copyright, key=slide.key)
        for k, text in enumerate(split_slide(screen.metrics, source, floor_px, screen.width, screen.height)):
            first = k == 0
            shown.append(ShownSlide(text, slide.title and first, slide.copyright and first, slide.source,
                                    key=slide.key))
    return shown


def preview_layout(metrics: Metrics, lines: List[str], next_text: str, px: float, box_w: float,
                   box_h: float, height: float) -> Optional[dict]:
    """{px, lines} of the grey next-slide preview under the text, or None
    where there is no room: half the text size (at least PREVIEW_MIN), as
    many of its lines as fit."""
    if not next_text:
        return None
    pp = max(PREVIEW_SCALE * px, PREVIEW_MIN * height / 1080)
    wrapped = _greedy_lines(metrics, next_text, pp, box_w)
    free = (box_h - len(lines) * px * SLIDE_LINE) / 2 - PREVIEW_GAP * px
    count = min(len(wrapped), int(free // (pp * SLIDE_LINE)) if free > 0 else 0)
    return {"px": pp, "lines": wrapped[:count]} if count >= 1 else None


def lay_out(screen: Screen, shown: List[ShownSlide], set_px: float, previews: List[str]) -> ScreenLayout:
    """The given slides on this screen, all at one size: the largest from
    the set size down where every slide fits; down to the readable minimum
    here, as these slides are not split any more (another screen may have
    needed fewer splits). A slide that cannot be split (the title slide) and
    would need more than the shrink limit gets its own smaller size instead
    of pulling the song down; slides that do not fit even at the minimum are
    drawn alone at it, cut (rule 5).
    previews: per slide the text of the next-slide preview ("" for none)."""
    metrics = screen.metrics
    max_px, min_px, floor_px = _limits(screen, set_px)
    sizes = _ladder(max_px, floor_px, min_px)
    boxes = [slide_box(screen.width, screen.height, s.title, s.copyright) for s in shown]
    own = [_best_size(metrics, s.text, sizes, *box) for s, box in zip(shown, boxes)]
    core = [px for px, s in zip(own, shown) if px is not None and (s.splittable or px >= floor_px - 1e-9)]
    song_px = min(core) if core else None
    out = ScreenLayout(screen.width, screen.height, song_px)
    for slide, box, best, upcoming in zip(shown, boxes, own, previews):
        if best is None:
            px = min_px
        elif song_px is None or best < song_px:
            px = best
        else:
            px = song_px
        lines = slide_lines(metrics, slide.text, px, *box)
        out.slides.append({
            "px": px, "lines": lines, "overflow": best is None,
            "preview": preview_layout(metrics, lines, upcoming, px, box[0], box[1], screen.height),
        })
    return out


def plan_song(screens: List[Screen], slides: List[SlideInput], set_px: float, show_next: bool = True):
    """(shown slides, [ScreenLayout per screen]) of a song on all its screens.
    Split first for the screen that needs the most slides on its own, then
    the other screens split further whatever still does not fit them at their
    shrink limit: all screens show the same slides (the verse keys stay in
    step) and none goes below its shrink limit for a slide that can be split."""
    base = [ShownSlide(s.text, s.title, s.copyright, i, key=s.key, splittable=s.splittable)
            for i, s in enumerate(slides)]
    alone = [split_song(screen, base, set_px) for screen in screens]
    order = sorted(range(len(screens)), key=lambda i: -len(alone[i]))
    shown = alone[order[0]] if screens else base
    for index in order[1:]:
        shown = split_song(screens[index], shown, set_px)
    # Part numbers per source slide (1/1 when not split).
    counts: Dict[int, int] = {}
    for slide in shown:
        counts[slide.source] = counts.get(slide.source, 0) + 1
    seen: Dict[int, int] = {}
    for slide in shown:
        seen[slide.source] = seen.get(slide.source, 0) + 1
        slide.part, slide.parts = seen[slide.source], counts[slide.source]
    previews = []
    for index, slide in enumerate(shown):
        title_slide = not slides[slide.source].splittable
        upcoming = shown[index + 1].text if show_next and index + 1 < len(shown) and not title_slide else ""
        previews.append(upcoming)
    return shown, [lay_out(screen, shown, set_px, previews) for screen in screens]
