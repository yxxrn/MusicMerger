"""Optional, bounded CTC lyric alignment. Does not modify the legacy ASR cache.

Blank frames are silence/non-character evidence, never the start of a word.
Model probabilities are diagnostics, not calibrated singing timing accuracy.
"""
import copy
import math
from .fallback import POLICIES, validate_partition, omission_windows


def ctc_word_spans(log_probs, words, vocabulary, blank_id):
    import numpy as np
    scores = np.asarray(log_probs)
    if scores.ndim != 2 or not len(scores) or not np.isfinite(scores).all():
        raise ValueError('Emisi CTC harus finite dan tidak kosong')
    tokens, ranges = [], []
    for word in words:
        if tokens:
            tokens.append(vocabulary['|'])
        first = len(tokens)
        for char in word.upper().replace('’', "'"):
            if char not in vocabulary and char.lower() in vocabulary:
                char = char.lower()
            if char not in vocabulary or vocabulary[char] == blank_id:
                raise ValueError(f'Karakter tidak didukung model CTC: {char}')
            tokens.append(vocabulary[char])
        if first == len(tokens):
            raise ValueError('Kata CTC kosong')
        ranges.append((first, len(tokens)))
    if not tokens or blank_id in tokens:
        raise ValueError('Token CTC kosong atau separator sama dengan blank')
    states = np.full(2 * len(tokens) + 1, blank_id, dtype=int)
    states[1::2] = tokens
    if states.min() < 0 or states.max() >= scores.shape[1]:
        raise ValueError('Token CTC di luar vocabulary')
    # Stay, advance one state, or skip a blank between DISTINCT characters.
    can_skip = np.zeros(len(states), dtype=bool)
    can_skip[2:] = (states[2:] != blank_id) & (states[2:] != states[:-2])
    previous = np.full(len(states), -np.inf)
    previous[0] = 0
    trace = np.zeros((len(scores), len(states)), dtype=np.int8)
    for frame, emission in enumerate(scores):
        one = np.r_[-np.inf, previous[:-1]]
        two = np.r_[[-np.inf, -np.inf], previous[:-2]]
        two[~can_skip] = -np.inf
        choices = np.stack((previous, one, two))
        trace[frame] = choices.argmax(axis=0)
        previous = choices.max(axis=0) + emission[states]
    state = len(states) - 1 if previous[-1] >= previous[-2] else len(states) - 2
    if not np.isfinite(previous[state]):
        raise ValueError('Tidak cukup frame untuk alignment CTC')
    path = np.empty(len(scores), dtype=int)
    for frame in range(len(scores) - 1, -1, -1):
        path[frame] = state
        state -= int(trace[frame, state])
    result = []
    for first, stop in ranges:
        frames = np.flatnonzero((path >= first * 2 + 1) &
                                (path <= (stop - 1) * 2 + 1) & (path % 2 == 1))
        if not len(frames):
            raise ValueError('Kata tidak memiliki emisi CTC')
        result.append({'start_frame': int(frames[0]), 'end_frame': int(frames[-1]) + 1,
                       'score': float(np.exp(scores[frames, states[path[frames]]]).mean())})
    return result


def apply_timing_override(lines, payload, *, audio_sha256, lyrics_sha256,
                          start, duration, song_duration, lyric_policy='auto'):
    """Validate identity, complete window coverage and ordered words before use."""
    if (lyric_policy not in POLICIES or type(payload.get('schema')) is not int
            or payload.get('schema') not in (1, 2)
            or payload.get('method') != 'wav2vec2_ctc_forced_alignment'):
        raise ValueError('Format timing acoustic tidak didukung')
    if payload.get('audio_sha256') != audio_sha256 or payload.get('lyrics_sha256') != lyrics_sha256:
        raise ValueError('Timing acoustic bukan untuk audio/lirik ini')
    coverage = payload.get('coverage', [])
    if (len(coverage) != 2 or not all(isinstance(x, (int, float)) and math.isfinite(x) for x in coverage)
            or not 0 <= coverage[0] < coverage[1] <= song_duration + 0.001
            or start < coverage[0] or start + duration > coverage[1] + 0.001):
        raise ValueError('Rentang render di luar cakupan timing acoustic')
    result = copy.deepcopy(lines)
    omitted = set()
    if payload['schema'] == 2:
        if lyric_policy == 'strict':
            raise ValueError('Mode strict menolak timing dengan lirik yang dilewati')
        if coverage[0] != 0 or abs(coverage[1] - song_duration) > .001:
            raise ValueError('Timing fallback memerlukan cakupan seluruh lagu')
        omitted = validate_partition(lines, payload)
        for index in omitted:
            result[index].update(wstart=None, wend=None, words=[], omitted=True,
                                 issues=['lyric_omitted_after_retry'], needs_review=True)
    seen = set()
    for update in payload.get('lines', []):
        index = update.get('index')
        if type(index) is not int or index in seen or not 0 <= index < len(lines):
            raise ValueError('Indeks timing acoustic duplikat/tidak valid')
        seen.add(index)
        line = result[index]
        times = update.get('words', [])
        if update.get('words_text') != line['words_text'] or len(times) != line['nwords']:
            raise ValueError('Kata timing acoustic tidak cocok dengan lirik')
        previous = coverage[0] * 1000
        for pair in times:
            if (not isinstance(pair, (list, tuple)) or len(pair) != 2
                    or not all(isinstance(x, (int, float)) and math.isfinite(x) for x in pair)
                    or not previous <= pair[0] < pair[1] <= coverage[1] * 1000 + 0.001):
                raise ValueError('Timestamp acoustic tidak finite, overlap, atau di luar cakupan')
            previous = pair[1]
        if not times:
            raise ValueError('Timing acoustic kosong')
        line.update(words=times, wstart=times[0][0] / 1000, wend=times[-1][1] / 1000,
                    provenance=['ctc_acoustic'] * len(times), estimated_words=0,
                    matched_words=len(times), issues=['acoustic_alignment_needs_listening_review'],
                    needs_review=True)
    if not seen:
        raise ValueError('Timing acoustic tidak berisi baris')
    # No partly corrected phrase/window, including 120 ms subtitle fade handles.
    for i, line in enumerate(lines):
        if i in omitted or (payload['schema'] == 2 and i in seen):
            continue
        if line.get('wstart') is None:
            raise ValueError('Baris tanpa anchor harus diperiksa sebelum timing override')
        if line['wend'] + 0.12 > coverage[0] and line['wstart'] - 0.12 < coverage[1] and i not in seen:
            raise ValueError('Cakupan timing acoustic belum mencakup semua baris')
    timed = [line for line in result if line.get('wstart') is not None]
    if any(a['wend'] > b['wstart'] + 0.000001 for a, b in zip(timed, timed[1:])):
        raise ValueError('Timing acoustic bertabrakan dengan baris tetangga')
    if payload['schema'] == 2 and payload.get('omission_windows') != omission_windows(payload, song_duration):
        raise ValueError('Rentang omission tidak cocok dengan timing tetangga')
    return result


def alignment_batches(lines, song_duration, preserved=0):
    """Whole lines in <=30s acoustic windows; do not cross long instrumentals."""
    if any(line.get('wstart') is None for line in lines):
        raise ValueError('Alignment penuh membutuhkan anchor setiap baris')
    groups = []
    for index in range(preserved, len(lines)):
        if (not groups or lines[index]['wend'] - lines[groups[-1][0]]['wstart'] > 26
                or lines[index]['wstart'] - lines[index - 1]['wend'] > 4
                or lines[index].get('source_index', index) != lines[index-1].get('source_index', index-1) + 1):
            groups.append([])
        groups[-1].append(index)
    batches = []
    for group in groups:
        first, last = group[0], group[-1]
        start = max(0, lines[first]['wstart'] - .8)
        end = min(song_duration, lines[last]['wend'] + .8)
        if first:
            start = max(start, (lines[first - 1]['wend'] + lines[first]['wstart']) / 2)
        if last + 1 < len(lines):
            end = min(end, (lines[last]['wend'] + lines[last + 1]['wstart']) / 2)
        if not 0 < end - start <= 30:
            raise ValueError('Baris terlalu panjang untuk jendela acoustic 30 detik')
        batches.append(dict(start=round(start, 6), end=round(end, 6), indices=group))
    return batches


def better_candidate(original, candidate):
    """Conservative diagnostic comparison, not a guarantee of better onset accuracy."""
    if len(original) != len(candidate) or not candidate:
        return False
    if any(x['start_frame'] >= x['end_frame'] or not math.isfinite(x['score']) for x in candidate):
        return False
    if any(a['end_frame'] > b['start_frame'] for a, b in zip(candidate, candidate[1:])):
        return False
    before = sum(x['end_frame'] - x['start_frame'] <= 1 for x in original)
    after = sum(x['end_frame'] - x['start_frame'] <= 1 for x in candidate)
    old_score = sum(x['score'] for x in original) / len(original)
    new_score = sum(x['score'] for x in candidate) / len(candidate)
    return after <= before and new_score > old_score + .05


def timing_quality(lines):
    durations = [(b - a) / 1000 for row in lines for a, b in row['words']]
    scores = [score for row in lines for score in row.get('scores', [])]
    return dict(words=len(durations), very_short_words=sum(x < .04 for x in durations),
                long_words=sum(x > 5 for x in durations), low_score_words=sum(x < .5 for x in scores),
                review_required=True, score_is_accuracy=False)


def main(argv=None):
    """Generate a preview or full-song timing using bounded local CTC windows."""
    import argparse
    import json
    from pathlib import Path
    import subprocess
    import sys
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('folder', type=Path)
    parser.add_argument('--reference', required=True, type=Path, help='laporan alignment baseline')
    parser.add_argument('--model-dir', required=True, type=Path, help='model lokal wav2vec2-base-960h')
    parser.add_argument('--start', type=float)
    parser.add_argument('--duration', type=float)
    parser.add_argument('--full', action='store_true', help='seluruh lagu, diproses per jendela <=30 detik')
    parser.add_argument('--reuse-intro', type=Path, help='pertahankan sidecar intro yang sudah direview')
    parser.add_argument('--language', default='en', help='bahasa model CTC yang dipakai')
    parser.add_argument('--analysis-audio', type=Path, help='vokal terpisah berdurasi sama; audio final tidak berubah')
    parser.add_argument('--refine', action='store_true', help='coba ulang baris meragukan sekali dengan jendela lebih kecil')
    parser.add_argument('--out', required=True, type=Path)
    args = parser.parse_args(argv)
    if args.full and (args.start is not None or args.duration is not None):
        parser.error('--full tidak boleh digabung dengan --start/--duration')
    if not args.full and (args.start is None or args.duration is None
            or not math.isfinite(args.start + args.duration) or args.start < 0
            or not 0 < args.duration <= 30):
        parser.error('Alignment ini hanya untuk potongan 0 < durasi <= 30 detik')
    if args.reuse_intro and not args.full:
        parser.error('--reuse-intro hanya untuk --full')
    if args.out.exists():
        parser.error('Output sudah ada; pilih nama baru')
    from . import renderer as karaoke
    _, audio_path, lyrics_path = karaoke.input_files(args.folder.resolve())
    audio_id = karaoke.audio_fingerprint(audio_path)['sha256']
    lyrics_id = karaoke.audio_fingerprint(lyrics_path)['sha256']
    reference = json.loads(args.reference.read_text(encoding='utf-8'))
    if reference['audio']['sha256'] != audio_id or reference['lyrics_sha256'] != lyrics_id:
        parser.error('Referensi berbeda dari audio/lirik input')
    lines = reference['lines']
    source_lyrics = karaoke.parse_lyrics(lyrics_path)
    if [line['words_text'] for line in lines] != [karaoke.WORD_RE.findall(text) for _, text in source_lyrics]:
        parser.error('Kata referensi berbeda dari lirik input')
    source_lines = lines
    selection = reference.get('selection')
    source_indices = list(range(len(lines)))
    if selection:
        if not args.full or args.reuse_intro:
            parser.error('Referensi fallback memerlukan --full tanpa --reuse-intro')
        omitted_indices = {x['index'] for x in selection['omitted_lines']}
        retained = [dict(index=i, words_text=x['words_text']) for i, x in enumerate(lines)
                    if i not in omitted_indices]
        validate_partition(lines, dict(selection, lines=retained))
        source_indices = [x['index'] for x in retained]
        lines = [dict(lines[i], source_index=i) for i in source_indices]
    song_duration = karaoke.ffprobe_duration(audio_path)
    analysis_path = args.analysis_audio.resolve() if args.analysis_audio else audio_path
    if analysis_path != audio_path:
        analysis_duration = karaoke.ffprobe_duration(analysis_path)
        if not math.isfinite(analysis_duration) or abs(analysis_duration - song_duration) > .03:
            parser.error('Audio analisis harus memiliki durasi sama dengan MP3 asli')
    if args.full:
        args.start, args.duration = 0, song_duration
    end = args.start + args.duration
    if end > song_duration:
        parser.error('Potongan melampaui akhir audio')
    reused = []
    if args.reuse_intro:
        intro = json.loads(args.reuse_intro.read_text(encoding='utf-8'))
        apply_timing_override(lines, intro, audio_sha256=audio_id, lyrics_sha256=lyrics_id,
            start=intro['coverage'][0], duration=intro['coverage'][1] - intro['coverage'][0], song_duration=song_duration)
        reused = intro['lines']
        if intro['coverage'][0] != 0 or [row['index'] for row in reused] != list(range(len(reused))):
            parser.error('Sidecar reuse harus berupa urutan baris intro mulai indeks 0')
    if args.full:
        batches = alignment_batches(lines, song_duration, preserved=len(reused))
    else:
        indices = [i for i, line in enumerate(lines) if line.get('wstart') is not None
                   and line['wend'] + .12 > args.start and line['wstart'] - .12 < end]
        if not indices or any(lines[i]['wstart'] < args.start or lines[i]['wend'] > end for i in indices):
            parser.error('Pilih jendela yang memuat baris utuh dengan jeda di kedua tepinya')
        batches = [dict(start=args.start, end=end, indices=indices)]
    # The worker uses FFmpeg for audio I/O, not torchaudio. Disable that optional
    # import ONLY in this CLI process: this machine has a mismatched torchaudio DLL.
    sys.modules['torchaudio'] = None
    import numpy as np
    import torch
    from transformers import Wav2Vec2ForCTC, Wav2Vec2Processor
    torch.set_num_threads(6)
    processor = Wav2Vec2Processor.from_pretrained(args.model_dir, local_files_only=True)
    model = Wav2Vec2ForCTC.from_pretrained(args.model_dir, local_files_only=True).eval()
    # Model convolution stride, not crop_duration/logit_count (which drifts).
    frame_seconds = math.prod(model.config.conv_stride) / 16000
    payload = {'schema': 1, 'method': 'wav2vec2_ctc_forced_alignment',
        'model': 'facebook/wav2vec2-base-960h' if args.model_dir.name == 'wav2vec2-base-960h' else str(args.model_dir.resolve()),
        'language': args.language, 'analysis_audio_sha256': karaoke.audio_fingerprint(analysis_path)['sha256'],
        'separation': reference.get('separation', 'not_requested'),
        'model_sha256': karaoke.audio_fingerprint(args.model_dir / 'model.safetensors')['sha256'],
        'audio_sha256': audio_id, 'lyrics_sha256': lyrics_id,
        'reference_sha256': karaoke.audio_fingerprint(args.reference)['sha256'],
        'coverage': [args.start, end], 'frame_seconds': frame_seconds,
        'review_required': True, 'lines': copy.deepcopy(reused), 'windows': [],
        'limitations': 'Speech-trained model; singing onsets need listening review. Scores are not timing accuracy.',
        'reused_intro_sha256': karaoke.audio_fingerprint(args.reuse_intro)['sha256'] if args.reuse_intro else None,
        'refinement': {'enabled': args.refine, 'attempted': 0, 'accepted': 0,
                       'criterion': 'diagnostic_improvement_not_accuracy_guarantee'}}
    def infer(begin, stop, tokens, source):
        pcm = subprocess.check_output(['ffmpeg', '-v', 'error', '-nostdin', '-ss', str(begin), '-i', str(source),
            '-t', str(stop - begin), '-ac', '1', '-ar', '16000', '-f', 'f32le', 'pipe:1'])
        audio = np.frombuffer(pcm, dtype=np.float32)
        with torch.inference_mode():
            logits = model(**processor(audio, sampling_rate=16000, return_tensors='pt')).logits[0]
        return (ctc_word_spans(logits.log_softmax(-1).numpy(), tokens,
                              processor.tokenizer.get_vocab(), model.config.pad_token_id),
                processor.decode(logits.argmax(-1).tolist()))
    for number, batch in enumerate(batches, 1):
        print(f'Alignment {number}/{len(batches)}: {batch["start"]:.2f}-{batch["end"]:.2f}s', flush=True)
        tokens = [word for index in batch['indices'] for word in lines[index]['words_text']]
        spans, transcript = infer(batch['start'], batch['end'], tokens, analysis_path)
        payload['windows'].append(dict(batch, greedy_transcript=transcript))
        cursor = 0
        for index in batch['indices']:
            line = lines[index]
            count = line['nwords']
            items = spans[cursor:cursor + count]
            times = [[round((batch['start'] + s['start_frame'] * frame_seconds) * 1000, 3),
                      round((batch['start'] + s['end_frame'] * frame_seconds) * 1000, 3)] for s in items]
            payload['lines'].append({'index': index, 'words_text': line['words_text'], 'words': times,
                'old_words': line['words'], 'scores': [round(s['score'], 4) for s in items],
                'low_score_words': [word for word, s in zip(line['words_text'], items) if s['score'] < .5]})
            cursor += count
        if args.refine:
            batch_rows = payload['lines'][-len(batch['indices']):]
            for row in batch_rows:
                if (sum(row['scores']) / len(row['scores']) >= .5
                        and all(b-a >= 40 for a, b in row['words'])):
                    continue
                position = next(i for i, x in enumerate(payload['lines']) if x is row)
                previous_end = payload['lines'][position-1]['words'][-1][1] / 1000 if position else 0
                next_start = (payload['lines'][position+1]['words'][0][0] / 1000
                              if position+1 < len(payload['lines']) else batch['end'])
                begin = max(batch['start'], previous_end, row['words'][0][0]/1000 - .6)
                stop = min(batch['end'], next_start, row['words'][-1][1]/1000 + .6)
                if stop - begin < .1:
                    continue
                payload['refinement']['attempted'] += 1
                print(f'  Periksa ulang baris {row["index"]+1}', flush=True)
                try:
                    candidate, _ = infer(begin, stop, row['words_text'], audio_path)
                except ValueError:
                    continue
                absolute = [dict(s, start_frame=s['start_frame']+begin/frame_seconds,
                                 end_frame=s['end_frame']+begin/frame_seconds) for s in candidate]
                original = [dict(start_frame=a/1000/frame_seconds, end_frame=b/1000/frame_seconds, score=score)
                            for (a, b), score in zip(row['words'], row['scores'])]
                if (absolute[0]['start_frame'] * frame_seconds < begin
                        or absolute[-1]['end_frame'] * frame_seconds > stop
                        or not better_candidate(original, absolute)):
                    continue
                row['words'] = [[round(s['start_frame']*frame_seconds*1000, 3),
                                 round(s['end_frame']*frame_seconds*1000, 3)] for s in absolute]
                row['scores'] = [round(s['score'], 4) for s in absolute]
                row['low_score_words'] = [w for w, s in zip(row['words_text'], absolute) if s['score'] < .5]
                payload['refinement']['accepted'] += 1
    payload['quality'] = timing_quality(payload['lines'])
    if selection:
        payload.update(selection, schema=2)
        for row in payload['lines']:
            row['index'] = source_indices[row['index']]
        for window in payload['windows']:
            window['indices'] = [source_indices[i] for i in window['indices']]
        payload['omission_windows'] = omission_windows(payload, song_duration)
    print(f'Diagnostik timing: {payload["quality"]}; retry {payload["refinement"]}', flush=True)
    apply_timing_override(source_lines, payload, audio_sha256=audio_id, lyrics_sha256=lyrics_id,
                          start=args.start, duration=args.duration, song_duration=song_duration)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open('x', encoding='utf-8') as stream:
        json.dump(payload, stream, ensure_ascii=False, indent=2)
    print(f'Timing: {args.out} (perlu review audio)', flush=True)


if __name__ == '__main__':
    main()
