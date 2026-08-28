"""Conservative lyric omissions, preserving original text and line identities.

Support ratios are heuristics, not calibrated accuracy. A local ASR failure may
hide a sung line; diagnostics and strict mode make that tradeoff explicit.
"""
import copy
import math

from .timing import WORD_RE

POLICIES = ('auto', 'strict')
POLICY = {'version': 1, 'min_line_support': .8, 'min_retained_tokens': .75}


def baseline_lines(lyrics):
    return [dict(label=label, text=text, words_text=WORD_RE.findall(text),
                 nwords=len(WORD_RE.findall(text)), wstart=0, wend=.001,
                 words=[], issues=[], needs_review=True)
            for label, text in lyrics]


def supported(line, duration):
    start, end = line.get('wstart'), line.get('wend')
    if (not all(isinstance(x, (int, float)) and math.isfinite(x) for x in (start, end))
            or not 0 <= start < end <= duration
            or line.get('matched_words', 0) < POLICY['min_line_support'] * line['nwords']
            or 'exact' not in line.get('provenance', [])
            or set(line.get('issues', [])) & {'no_exact_anchors', 'uncertain_interpolation_span',
                                             'cross_line_overlap', 'overlapping_asr_words'}):
        return False
    return True


def select_lines(small, medium, song_duration, policy='auto'):
    if policy not in POLICIES or not math.isfinite(song_duration) or song_duration <= 0:
        raise ValueError('Kebijakan/durasi fallback tidak valid')
    if not small or (medium is not None and
                    [x['words_text'] for x in small] != [x['words_text'] for x in medium]):
        raise ValueError('Referensi model tidak cocok dengan lirik')
    if policy == 'strict':
        chosen = copy.deepcopy(medium if medium is not None else small)
        if any(x.get('wstart') is None for x in chosen):
            raise ValueError('Mode strict: lirik tidak dapat dipetakan lengkap setelah dua percobaan')
        return chosen, []
    if medium is None and any(not supported(x, song_duration) for x in small):
        raise ValueError('Periksa model kedua sebelum melewati lirik')
    chosen, omitted, options = [], [], []
    for index, first in enumerate(small):
        second = medium[index] if medium is not None else None
        candidates = [x for x in (first, second) if x is not None and supported(x, song_duration)]
        conflict = (len(candidates) == 2 and
                    max(abs(first['wstart']-second['wstart']), abs(first['wend']-second['wend'])) > 2)
        if candidates and not conflict:
            chosen.append(copy.deepcopy(candidates[0]))
            options.append((index, candidates))
        else:
            row = copy.deepcopy(first)
            row.update(wstart=None, wend=None, words=[], omitted=True,
                       issues=['lyric_omitted_after_retry'], needs_review=True)
            chosen.append(row)
            evidence = [dict(model=name, matched_words=x['matched_words'], total_words=x['nwords'])
                        for name, x in (('small', first), ('medium', second)) if x is not None]
            omitted.append(dict(index=index, words_text=first['words_text'],
                                reason='conflicting_anchors' if conflict else 'unsupported_after_retry',
                                evidence=evidence))
    total = sum(x['nwords'] for x in chosen)
    kept = sum(x['nwords'] for x in chosen if not x.get('omitted'))
    if not kept or kept < POLICY['min_retained_tokens'] * total:
        raise ValueError('Kurang dari 75% kata lirik terpetakan; periksa pasangan MD/MP3')
    # At most two states per line. Prefer the original small mapping where
    # possible, but choose a compatible neighbor when medium rescues a line.
    # This selects existing timestamps; it never clips or shifts a word.
    states = [(0, [])]
    for index, candidates in options:
        next_states = []
        for rank, candidate in enumerate(candidates):
            compatible = [(cost, path) for cost, path in states
                          if not path or path[-1][1]['wend'] <= candidate['wstart'] + .000001]
            if compatible:
                cost, path = min(compatible, key=lambda state: state[0])
                next_states.append((cost + rank, path + [(index, candidate)]))
        if not next_states:
            raise ValueError('Kronologi lirik tidak pasti; fallback tidak boleh menebak timing')
        states = next_states
    _, path = min(states, key=lambda state: state[0])
    for index, candidate in path:
        chosen[index] = copy.deepcopy(candidate)
    return chosen, omitted


def validate_partition(lines, payload):
    """Validate omissions independently of the acoustic timestamps."""
    if (payload.get('policy') != POLICY or type(payload['policy'].get('version')) is not int
            or payload.get('source_words') != [x['words_text'] for x in lines]
            or type(payload.get('source_line_count')) is not int
            or payload['source_line_count'] != len(lines)):
        raise ValueError('Identitas/policy lirik fallback tidak cocok')
    rows, omissions = payload.get('lines'), payload.get('omitted_lines')
    if not isinstance(rows, list) or not rows or not isinstance(omissions, list) or not omissions:
        raise ValueError('Partisi timing fallback kosong/tidak valid')
    seen, omitted_indices = set(), set()

    for omitted, group in ((False, rows), (True, omissions)):
        for row in group:
            if not isinstance(row, dict):
                raise ValueError('Baris partisi fallback tidak valid')
            index = row.get('index')
            if type(index) is not int or index in seen or not 0 <= index < len(lines):
                raise ValueError('Indeks fallback duplikat/di luar lirik')
            seen.add(index)
            if row.get('words_text') != lines[index]['words_text']:
                raise ValueError('Kata fallback tidak cocok dengan MD')
            if omitted:
                omitted_indices.add(index)
                evidence = row.get('evidence')
                if (row.get('reason') not in ('unsupported_after_retry', 'conflicting_anchors')
                        or 'words' in row or not isinstance(evidence, list) or len(evidence) != 2):
                    raise ValueError('Alasan/bukti omission tidak valid')
                if {x.get('model') for x in evidence if isinstance(x, dict)} != {'small', 'medium'}:
                    raise ValueError('Omission harus memiliki bukti dua model')
                for item in evidence:
                    if (type(item.get('matched_words')) is not int
                            or type(item.get('total_words')) is not int
                            or item.get('total_words') != lines[index]['nwords']
                            or not 0 <= item['matched_words'] <= item['total_words']):
                        raise ValueError('Bukti support omission tidak valid')
    if seen != set(range(len(lines))):
        raise ValueError('Partisi fallback belum mencakup semua baris MD')
    total = sum(x['nwords'] for x in lines)
    kept = sum(x['nwords'] for i, x in enumerate(lines) if i not in omitted_indices)
    if not kept or kept < POLICY['min_retained_tokens'] * total:
        raise ValueError('Kurang dari 75% kata lirik terpetakan')
    return omitted_indices


def omission_windows(payload, song_duration):
    """Bound skipped blocks by real retained words, never synthetic word times."""
    timed = {x['index']: x for x in payload['lines']}
    missing = sorted(x['index'] for x in payload['omitted_lines'])
    groups = []
    for index in missing:
        if not groups or index != groups[-1][-1] + 1:
            groups.append([])
        groups[-1].append(index)
    windows = []
    for group in groups:
        left, right = timed.get(group[0]-1), timed.get(group[-1]+1)
        start = left['words'][-1][1] / 1000 if left else 0.0
        end = right['words'][0][0] / 1000 if right else song_duration
        if not 0 <= start < end <= song_duration:
            raise ValueError('Rentang omission tidak dapat dibatasi dengan aman')
        windows.append([start, end])
    return windows
