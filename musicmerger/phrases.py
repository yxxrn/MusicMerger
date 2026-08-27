"""Display-only phrase grouping; never infer or change alignment timestamps."""
import math
import re

from .timing import WORD_RE


WEAK_ENDINGS = frozenset('a an the to of in on with and or but that what it '
                         'my your our their its every each this these those '
                         'little old new final small big long short rough smooth '
                         'natural golden wooden own same good bad '
                         'di ke dari dan yang dengan untuk'.split())
CLAUSE_STARTS = frozenset('let make what that when while because from with '
                         'and but yang ketika karena dari dengan'.split())
BOUNDARY_PUNCTUATION = re.compile(r'[,;:.!?…—]')


def split_phrases(lines, max_words=5, max_chars=26):
    """Prefer short, balanced phrases using pauses and visible-text boundaries.

    This is a deterministic heuristic, not a grammatical or acoustic parser.
    A single word exceeding max_chars stays intact; no word is discarded.
    """
    if (not isinstance(max_words, int) or max_words < 1
            or not isinstance(max_chars, int) or max_chars < 1):
        raise ValueError('Batas frasa harus berupa bilangan bulat positif')
    result = []
    for source_line, line in enumerate(lines, 1):
        if line.get('wstart') is None:
            continue
        tokens = list(WORD_RE.finditer(line['text']))
        words = line.get('words', [])
        if len(tokens) != len(words) or not words:
            raise ValueError('Jumlah kata dan timing frasa berbeda atau kosong')
        previous_end = 0
        for start, end in words:
            if not all(math.isfinite(v) for v in (start, end)) or start < previous_end or end < start:
                raise ValueError('Timing kata frasa tidak valid atau tumpang tindih')
            previous_end = end
        visible = [t.group().replace("'", '').replace('’', '') for t in tokens]
        n = len(visible)
        # Strength of a break after word i. Splitting inside a comma-delimited
        # phrase or across a sung pause is allowed, but costs more.
        boundaries = [0.0] * n
        for i in range(n - 1):
            between = line['text'][tokens[i].end():tokens[i + 1].start()]
            punctuation = 4.0 if BOUNDARY_PUNCTUATION.search(between) else 0.0
            pause = min(4.0, max(0, words[i + 1][0] - words[i][1]) / 150)
            boundaries[i] = max(punctuation, pause)
        costs, ends = [float('inf')] * (n + 1), [None] * n
        costs[n] = 0
        for start in range(n - 1, -1, -1):
            for end in range(min(n, start + max_words), start, -1):
                count = end - start
                text = ' '.join(visible[start:end])
                if count > 1 and len(text) > max_chars:
                    continue
                score = 1 + 0.35 * (count - 4) ** 2
                if count < 3:
                    score += (3 - count) * 1.5
                duration = (words[end - 1][1] - words[start][0]) / 1000
                score += max(0, 0.65 - duration) * 3
                score += sum(boundaries[start:end - 1])
                if end < n:
                    if visible[end - 1].casefold() in WEAK_ENDINGS:
                        score += 3
                    if visible[end].casefold() in CLAUSE_STARTS:
                        score -= 3
                score += costs[end]
                if score < costs[start]:
                    costs[start], ends[start] = score, end
        start = 0
        while start < n:
            end = ends[start]
            result.append({'text': ' '.join(visible[start:end]),
                           'words': list(words[start:end]),
                           'wstart': words[start][0] / 1000,
                           'wend': words[end - 1][1] / 1000,
                           'source_line': source_line, 'word_range': [start, end]})
            start = end
    return result
