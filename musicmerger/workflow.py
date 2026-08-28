"""Folder-based workflow; inputs are read-only and every run owns its outputs."""
from datetime import datetime
import json
import math
from pathlib import Path
import shutil
import sys

from . import renderer as karaoke
from .acoustic import apply_timing_override
from .process import run_command
from .publication import publish

from .paths import ROOT


def reserve_run(folder, mode, stamp=None):
    if mode not in ('preview', 'full'):
        raise ValueError('Mode tidak valid')
    parent = folder / 'MusicMerger-output'
    if parent.is_symlink() or (parent.exists() and getattr(parent.lstat(), 'st_file_attributes', 0) & 0x400):
        raise ValueError('Folder output tidak boleh berupa link/junction')
    parent.mkdir(exist_ok=True)
    stamp = stamp or datetime.now().strftime('%Y%m%d-%H%M%S')
    for number in range(1, 10000):
        run = parent / f'{stamp}-{mode}-{number:03d}'
        try:
            run.mkdir()
        except FileExistsError:
            continue
        for name in ('support', 'timing', 'preview' if mode == 'preview' else 'final'):
            (run / name).mkdir()
        return run
    raise ValueError('Nama output habis; gunakan folder baru')


def validate_timing(payload, lyrics, audio_hash, lyrics_hash, duration, *, lyric_policy='auto'):
    baseline = []
    for label, text in lyrics:
        words = karaoke.WORD_RE.findall(text)
        baseline.append(dict(label=label, text=text, words_text=words, nwords=len(words),
                             wstart=0, wend=.001, words=[], issues=[], needs_review=True))
    return apply_timing_override(baseline, payload, audio_sha256=audio_hash,
                                 lyrics_sha256=lyrics_hash, start=0,
                                 duration=duration, song_duration=duration, lyric_policy=lyric_policy)


def write_render_cache(path, audio, payload, language):
    # The validated timing is already complete. Do not run ASR again just to render.
    words = [{'w': word, 'start': pair[0] / 1000, 'end': pair[1] / 1000}
             for line in sorted(payload['lines'], key=lambda row: row['index'])
             for word, pair in zip(line['words_text'], line['words'])]
    data = dict(schema=2, audio=karaoke.audio_fingerprint(audio), model=karaoke.MODEL,
                language=language, words=words, source='validated_acoustic_timing')
    with path.open('x', encoding='utf-8') as stream:
        json.dump(data, stream, ensure_ascii=False, indent=2)


def find_timing(folder, lyrics, audio_hash, lyrics_hash, duration, *, candidates=None, lyric_policy='auto'):
    if candidates is None:
        candidates = [*sorted((folder / 'MusicMerger-output/cache').glob('timing-*.json'), reverse=True),
                      *sorted((ROOT / 'outputs').glob('timing-acoustic-full-*.json'), reverse=True)]
    for path in candidates:
        try:
            payload = json.loads(path.read_text(encoding='utf-8-sig'))
            validate_timing(payload, lyrics, audio_hash, lyrics_hash, duration, lyric_policy=lyric_policy)
            return path
        except (OSError, ValueError, TypeError, KeyError, AttributeError):
            continue
    return None


def run(args):
    folder = args.folder.resolve()
    _, audio, md = karaoke.input_files(folder)
    lyrics = karaoke.parse_lyrics(md)
    for executable in ('ffmpeg', 'ffprobe'):
        if not shutil.which(executable):
            raise ValueError(f'{executable} belum ditemukan pada PATH; lihat README.')
    for asset in (karaoke.DEFAULT_FONT_FILE, karaoke.DEFAULT_LOGO_FILE):
        if not asset.is_file():
            raise ValueError(f'Aset wajib tidak ditemukan: {asset}')
    duration = karaoke.ffprobe_duration(audio)
    if not math.isfinite(duration) or duration <= 0:
        raise ValueError('Durasi MP3 tidak valid')
    if args.mode == 'preview' and args.start >= duration:
        raise ValueError('Awal preview melewati akhir MP3')
    audio_hash = karaoke.audio_fingerprint(audio)['sha256']
    lyrics_hash = karaoke.audio_fingerprint(md)['sha256']
    selected = args.timing_file
    if selected is not None:
        selected = selected.resolve()
        validate_timing(json.loads(selected.read_text(encoding='utf-8-sig')), lyrics,
                        audio_hash, lyrics_hash, duration, lyric_policy=args.lyric_policy)
    else:
        selected = find_timing(folder, lyrics, audio_hash, lyrics_hash, duration, lyric_policy=args.lyric_policy)
    job = reserve_run(folder, args.mode)
    state = dict(status='running', folder=str(folder), mode=args.mode,
                 audio_sha256=audio_hash, lyrics_sha256=lyrics_hash)
    def checkpoint(stage, **details):
        state.update(stage=stage, **details)
        (job / 'status.json').write_text(json.dumps(state, indent=2), encoding='utf-8')
    print(f'Folder hasil: {job}', flush=True)
    try:
        checkpoint('timing')
        timing = job / 'timing/timing.json'
        if selected:
            print('[1/3] Timing cocok dengan audio/lirik; pakai ulang tanpa transkripsi.', flush=True)
            shutil.copyfile(selected, timing)
        else:
            print('[1/3] Sinkronisasi otomatis. Proses pertama bisa lama; model Whisper mungkin diunduh.', flush=True)
            command = [sys.executable, '-B', '-m', 'musicmerger.sync', str(folder), str(job),
                       '--language', args.language, '--vocals', args.vocals,
                       '--lyric-policy', args.lyric_policy]
            if args.align_model:
                command += ['--align-model', str(args.align_model.resolve())]
            run_command(command, job / 'support/synchronization.log')
        payload = json.loads(timing.read_text(encoding='utf-8-sig'))
        validate_timing(payload, lyrics, audio_hash, lyrics_hash, duration, lyric_policy=args.lyric_policy)
        omissions = payload.get('omitted_lines', [])
        if omissions:
            numbers = [x['index'] + 1 for x in omissions]
            print(f'PERINGATAN: lirik {numbers} tidak ditampilkan; bagian terkait memakai logo.', flush=True)
            checkpoint('timing', omitted_lyric_lines=numbers, omission_windows=payload['omission_windows'])
        language = payload.get('language') or 'en'
        cache = job / 'support/render-cache.json'
        write_render_cache(cache, audio, payload, language)
        shared = job.parent / 'cache'
        if shared.exists() and shared.resolve() != job.parent.resolve() / 'cache':
            raise ValueError('Cache output tidak boleh diarahkan keluar lewat link')
        shared.mkdir(exist_ok=True)
        if not selected or selected.parent != shared:
            saved = shared / f'timing-{job.name}.json'
            with saved.open('xb') as stream:
                stream.write(timing.read_bytes())
        print('Timing otomatis tetap dapat meleset; tidak ada jaminan presisi 100%.', flush=True)
        print('[2/3] Render dengan style terakhir. Pemilihan GPU/CPU muncul di bawah.', flush=True)
        checkpoint('render', timing=str(timing), reused_timing=str(selected) if selected else None)
        command = [sys.executable, '-B', '-m', 'musicmerger.renderer', str(folder),
                   '--out', str(job / 'support'), '--timing-file', str(timing),
                   '--cache-file', str(cache), '--language', language,
                   '--allow-estimated-timing', '--encoder', args.encoder,
                   '--lyric-policy', args.lyric_policy]
        target_seconds = duration
        if args.mode == 'preview':
            target_seconds = min(args.duration, duration - args.start)
            command += ['--start', str(args.start), '--duration', str(target_seconds), '--width', str(args.width)]
        run_command(command, job / 'support/render-console.log',
                    watch_log=job / 'support' / f'{folder.name}.ffmpeg.log', target_seconds=target_seconds)
        print('[3/3] Pisahkan MP4 selesai dari subtitle dan file pendukung.', flush=True)
        result = publish(job / 'support' / f'{folder.name}.mp4', job, args.mode, song_name=audio.stem)
        checkpoint('done', status='complete', output=str(result))
        return result
    except BaseException as exc:
        checkpoint('stopped', status='cancelled' if isinstance(exc, KeyboardInterrupt) else 'failed',
                   error=str(exc))
        raise
