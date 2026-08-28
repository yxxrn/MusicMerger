"""Isolated automatic preparation worker. Never changes the source MP3/MP4/MD/cache."""
import argparse
import importlib.util
import json
from pathlib import Path
import sys

from . import renderer as karaoke
from .process import run_command
from .fallback import POLICIES, POLICY, select_lines, supported

from .paths import ROOT


def model_for(language, explicit):
    if explicit is not None:
        model = explicit.resolve()
    elif language == 'en':
        model = ROOT / '.models/wav2vec2-base-960h'
    else:
        raise ValueError(f'Bahasa lagu terdeteksi {language}. Model CTC bawaan hanya untuk en; '
                         'gunakan --align-model dengan model lokal yang sesuai bahasa. '
                         'Model Inggris tidak dipaksakan; transkripsi tersimpan di support.')
    if not (model / 'model.safetensors').is_file() or not (model / 'config.json').is_file():
        raise ValueError(f'Model CTC lokal belum lengkap: {model}. Lihat README bagian dependensi.')
    return model


def analysis_audio(audio, support, mode):
    if mode == 'off' or importlib.util.find_spec('demucs') is None:
        print('Pemisahan vokal tidak aktif/tersedia; alignment memakai MP3 asli.', flush=True)
        return audio, 'unavailable_or_disabled'
    print('Pisahkan vokal untuk analisis saja (Demucs CPU); MP3 final tetap asli.', flush=True)
    destination = support / 'separated'
    try:
        run_command([sys.executable, '-B', '-m', 'demucs.separate', '-n', 'htdemucs',
                     '--two-stems', 'vocals', '-d', 'cpu', '-o', str(destination), str(audio)],
                    support / 'separation.log')
        vocals = destination / 'htdemucs' / audio.stem / 'vocals.wav'
        if not vocals.is_file() or abs(karaoke.ffprobe_duration(vocals) - karaoke.ffprobe_duration(audio)) > .03:
            raise ValueError('Durasi vokal hasil pemisahan tidak cocok dengan MP3 asli')
        return vocals, 'demucs_htdemucs'
    except (OSError, ValueError, RuntimeError) as exc:
        print(f'PERINGATAN: pemisahan gagal ({exc}); analisis memakai audio asli.', flush=True)
        return audio, 'failed_using_original'


def reuse_asr_cache(audio, target, *, language, model_name='small'):
    """Copy a validated previous ASR cache into this run; never edit its source."""
    if target.exists():
        return False
    identity = karaoke.audio_fingerprint(audio)
    for source in sorted(target.parents[2].glob(f'*/support/{target.name}'), reverse=True):
        try:
            data = source.read_bytes()
            cached = json.loads(data.decode('utf-8-sig'))
            if (not isinstance(cached, dict) or cached.get('schema') != 2
                    or cached.get('audio') != identity or cached.get('model') != model_name
                    or cached.get('language') != language):
                continue
            karaoke.validate_words(cached.get('words'))
        except (OSError, ValueError, TypeError, KeyError):
            continue
        with target.open('xb') as stream:
            stream.write(data)
        print(f'Pakai ulang cache ASR {model_name}: {source}', flush=True)
        return True
    return False


def prepare(folder, job, *, language='auto', align_model=None, vocals='auto', lyric_policy='auto'):
    _, audio, md = karaoke.input_files(folder)
    lyrics = karaoke.parse_lyrics(md)
    support = job / 'support'
    for dependency in ('faster_whisper', 'torch', 'transformers', 'numpy'):
        if importlib.util.find_spec(dependency) is None:
            raise ValueError(f'Dependensi {dependency} belum tersedia. Lihat README bagian dependensi.')
    if language != 'auto':
        model_for(language, align_model)
    analysis, separation = analysis_audio(audio, support, vocals)
    asr_cache = support / 'asr-cache.json'
    reuse_asr_cache(analysis, asr_cache, language=language)
    words = karaoke.whisper_words(analysis, asr_cache, language=language)
    cache = json.loads(asr_cache.read_text(encoding='utf-8'))
    detected = cache.get('detected_language') if language == 'auto' else language
    if not detected:
        raise ValueError('Bahasa tidak berhasil dideteksi; jalankan dengan --language en atau bahasa lagu.')
    model = model_for(detected, align_model)
    print(f'Bahasa: {detected}; model CTC: {model.name}', flush=True)
    lines = karaoke.build_line_timing(lyrics, words)
    duration = karaoke.ffprobe_duration(audio)
    small_lines, medium_lines = lines, None
    needs_retry = (any(line.get('wstart') is None for line in lines) if lyric_policy == 'strict'
                   else any(not supported(line, duration) for line in lines))
    if needs_retry:
        print('Ada lirik tanpa anchor/kurang kuat; coba transkripsi medium sekali.', flush=True)
        reuse_asr_cache(analysis, support / 'asr-medium-cache.json', language=detected, model_name='medium')
        words = karaoke.whisper_words(analysis, support / 'asr-medium-cache.json',
                                      language=detected, model_name='medium')
        medium_lines = karaoke.build_line_timing(lyrics, words)
    lines, omitted = select_lines(small_lines, medium_lines, duration, policy=lyric_policy)
    selection = dict(policy=POLICY, source_words=[x['words_text'] for x in lines],
                     source_line_count=len(lines), omitted_lines=omitted) if omitted else None
    if omitted:
        print(f'PERINGATAN: baris {[x["index"]+1 for x in omitted]} dilewati; '
              'bagian ini memakai logo, bukan lirik tebakan.', flush=True)
    reference = support / 'reference.json'
    reference.write_text(json.dumps(dict(audio=karaoke.audio_fingerprint(audio),
        lyrics_sha256=karaoke.audio_fingerprint(md)['sha256'], song_duration=duration,
        lines=lines, language=detected, separation=separation, selection=selection),
        ensure_ascii=False, indent=2), encoding='utf-8')
    command = [sys.executable, '-B', '-m', 'musicmerger.acoustic', str(folder),
               '--reference', str(reference), '--model-dir', str(model), '--full', '--refine',
               '--language', detected, '--out', str(job / 'timing/timing.json')]
    if analysis != audio:
        command += ['--analysis-audio', str(analysis)]
    run_command(command, support / 'alignment.log')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('folder', type=Path)
    parser.add_argument('job', type=Path)
    parser.add_argument('--language', default='auto')
    parser.add_argument('--align-model', type=Path)
    parser.add_argument('--vocals', choices=('auto', 'off'), default='auto')
    parser.add_argument('--lyric-policy', choices=POLICIES, default='auto')
    args = parser.parse_args()
    try:
        prepare(args.folder.resolve(), args.job.resolve(), language=args.language,
                align_model=args.align_model, vocals=args.vocals, lyric_policy=args.lyric_policy)
    except (OSError, ValueError, RuntimeError, ImportError) as exc:
        parser.exit(1, f'ERROR sinkronisasi: {exc}\n')


if __name__ == '__main__':
    main()
