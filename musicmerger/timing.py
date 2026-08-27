"""Offline sequence alignment. Text matching is not acoustic forced alignment."""
import math
import re
import unicodedata
from difflib import SequenceMatcher

WORD_RE = re.compile(r"[^\W_]+(?:['’][^\W_]+)*", re.UNICODE)


def norm(text):
    return "".join(c for c in unicodedata.normalize("NFKD", text.casefold()) if c.isalnum())


def validate_words(words):
    if not isinstance(words, list) or not words:
        raise ValueError("Cache/transkripsi tidak berisi kata")
    previous = -1.0
    for word in words:
        if not isinstance(word, dict) or not isinstance(word.get("w"), str):
            raise ValueError("Format kata cache tidak valid")
        try:
            start, end = float(word["start"]), float(word["end"])
        except (KeyError, ValueError, TypeError) as exc:
            raise ValueError("Timestamp cache tidak valid") from exc
        if not math.isfinite(start + end) or start < 0 or end < start or start < previous:
            raise ValueError("Timestamp cache negatif, terbalik, tidak finite, atau tidak berurutan")
        previous = start
    return words


def align_tokens(tokens, transcript):
    """Global edit alignment, retaining order across repeated choruses.

    Low-similarity substitutions are not timing anchors. Complexity is O(N*M),
    bounded for song-sized inputs; unlike the original five-word lookahead this
    can recover after arbitrarily many missing lyric words.
    """
    n, m = len(tokens), len(transcript)
    if n * m > 5_000_000:
        raise ValueError("Lirik terlalu panjang; pecah menjadi beberapa paket lagu.")
    trace = [bytearray(m + 1) for _ in range(n + 1)]
    trace[0] = bytearray([2] * (m + 1))
    previous = [float(j) for j in range(m + 1)]
    similarity = {}
    for i, token in enumerate(tokens, 1):
        current = [float(i)] + [0.0] * m
        trace[i][0] = 1
        for j, word in enumerate(transcript, 1):
            key = token, word
            if key not in similarity:
                similarity[key] = SequenceMatcher(None, token, word, autojunk=False).ratio()
            ratio = similarity[key]
            substitution = 0 if token == word else (1 - ratio if ratio >= 0.6 else 1.5)
            choices = previous[j - 1] + substitution, previous[j] + 1, current[j - 1] + 1
            choice = min(range(3), key=choices.__getitem__)
            current[j], trace[i][j] = choices[choice], choice
        previous = current
    pairs = [None] * n
    i, j = n, m
    while i or j:
        choice = trace[i][j]
        if choice == 0:
            pairs[i - 1] = j - 1, similarity[tokens[i - 1], transcript[j - 1]]
            i, j = i - 1, j - 1
        elif choice == 1:
            i -= 1
        else:
            j -= 1
    return pairs


def build_line_timing(lines_in, wwords):
    validate_words(wwords)
    source = [w for w in wwords if norm(w["w"])]
    tokens, lines = [], []
    for label, text in lines_in:
        words_text = WORD_RE.findall(text)
        lines.append({"label": label, "text": text, "nwords": len(words_text),
                      "words_text": words_text, "token_offset": len(tokens)})
        tokens.extend(norm(word) for word in words_text)
    if not tokens or not source:
        raise ValueError("Tidak ada token lirik atau transkripsi")
    pairs = align_tokens(tokens, [norm(w["w"]) for w in source])
    times, provenance = [None] * len(tokens), ["estimated"] * len(tokens)
    for i, pair in enumerate(pairs):
        if pair is not None:
            j, ratio = pair
            w = source[j]
            if ratio >= 0.6 and float(w["end"]) > float(w["start"]):
                times[i] = float(w["start"]), float(w["end"])
                provenance[i] = "exact" if tokens[i] == norm(w["w"]) else "fuzzy"
    for line in lines:
        lo, n = line["token_offset"], line["nwords"]
        hi = lo + n
        anchors = [i for i in range(lo, hi) if times[i] is not None]
        if not anchors:
            line.update(wstart=None, wend=None, words=[], issues=["no_anchors"], needs_review=True,
                        matched_words=0, estimated_words=n, provenance=["missing"] * n)
            continue
        issues = []
        aligned_source = [source[pairs[i][0]] for i in range(lo, hi) if pairs[i] is not None]
        if any(float(w["end"]) <= float(w["start"]) for w in aligned_source):
            issues.append("zero_duration_asr_words")
        if len(anchors) / n < 0.6:
            issues.append("few_lexical_anchors")
        # Interpolate each missing run once, between ordered boundaries. Leading
        # and trailing omissions are bounded locally, not across instrumentals.
        cursor = lo
        while cursor < hi:
            if times[cursor] is not None:
                cursor += 1
                continue
            stop = cursor + 1
            while stop < hi and times[stop] is None:
                stop += 1
            count = stop - cursor
            previous_anchor = next((times[k][1] for k in range(cursor - 1, -1, -1) if times[k]), 0.0)
            next_anchor = next((times[k][0] for k in range(stop, len(times)) if times[k]), None)
            left = times[cursor - 1][1] if cursor > lo else max(previous_anchor, times[stop][0] - count * 0.3)
            if stop < hi:
                right = times[stop][0]
            else:
                right = min(left + count * 0.3, next_anchor) if next_anchor is not None else left + count * 0.3
            right = max(left, right)
            if right - left > 2.0 or right - left < count * 0.04:
                issues.append("uncertain_interpolation_span")
            for k in range(count):
                times[cursor + k] = (left + (right - left) * k / count,
                                     left + (right - left) * (k + 1) / count)
            cursor = stop
        word_times, previous_end = [], 0.0
        for i in range(lo, hi):
            start, end = times[i]
            if start < previous_end:
                start, end = previous_end, max(previous_end, end)
                provenance[i] = "estimated"
                issues.append("overlapping_asr_words")
            word_times.append((start * 1000, end * 1000))
            previous_end = end
        estimated = provenance[lo:hi].count("estimated")
        if estimated:
            issues.append("interpolated_words")
        if "exact" not in provenance[lo:hi]:
            issues.append("no_exact_anchors")
        line.update(wstart=word_times[0][0] / 1000, wend=word_times[-1][1] / 1000,
                    words=word_times, matched_words=n - estimated, estimated_words=estimated,
                    provenance=provenance[lo:hi], issues=sorted(set(issues)), needs_review=bool(issues))
    timed = [line for line in lines if line["wstart"] is not None]
    for previous, current in zip(timed, timed[1:]):
        if previous["wend"] > current["wstart"] + 0.000001:
            for line in (previous, current):
                line["needs_review"] = True
                line["issues"] = sorted(set(line["issues"] + ["cross_line_overlap"]))
    return lines
