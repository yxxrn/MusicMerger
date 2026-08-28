"""Simple interactive entry point; the original karaoke.py CLI stays available."""
import argparse
import math
from pathlib import Path
import sys


def folder_path(value):
    return Path(value.strip().strip('"').strip("'")).expanduser()


def options(argv=None):
    parser = argparse.ArgumentParser(description='MusicMerger: pilih folder MP3 + MP4 + MD, lalu render.')
    parser.add_argument('folder', nargs='?', type=folder_path)
    parser.add_argument('--mode', choices=('preview', 'full'))
    parser.add_argument('--start', type=float)
    parser.add_argument('--duration', type=float)
    parser.add_argument('--width', type=int, default=1280, help='lebar preview (default 1280)')
    parser.add_argument('--language', default='auto', help='bahasa lagu; default deteksi otomatis')
    parser.add_argument('--encoder', choices=('auto', 'cpu', 'h264_amf', 'h264_nvenc', 'h264_qsv'), default='auto')
    parser.add_argument('--timing-file', type=Path, help='opsional: timing CTC untuk lagu ini')
    parser.add_argument('--align-model', type=Path, help='folder model CTC lokal untuk bahasa selain en')
    parser.add_argument('--vocals', choices=('auto', 'off'), default='auto', help='gunakan Demucs jika tersedia')
    parser.add_argument('--lyric-policy', choices=('auto', 'strict'), default='auto',
                        help='auto: lirik tidak jelas menjadi logo; strict: berhenti jika ada baris tanpa anchor')
    args = parser.parse_args(argv)
    if args.folder is None:
        value = input('Folder berisi MP3, MP4, MD (bisa paste path): ').strip()
        if not value:
            parser.error('Folder belum diisi.')
        args.folder = folder_path(value)
    interactive = args.mode is None
    while args.mode is None:
        choice = input('1. Preview (20 detik)   2. Render penuh   [1]: ').strip() or '1'
        args.mode = {'1': 'preview', '2': 'full'}.get(choice)
        if args.mode is None:
            print('Pilih 1 atau 2.')
    if interactive and args.mode == 'preview':
        try:
            if args.start is None:
                args.start = float(input('Mulai detik [0]: ').strip() or '0')
            if args.duration is None:
                args.duration = float(input('Durasi preview [20]: ').strip() or '20')
        except ValueError:
            parser.error('Waktu harus berupa angka detik.')
    args.start = 0 if args.start is None else args.start
    args.duration = 20 if args.duration is None else args.duration
    if not math.isfinite(args.start) or args.start < 0 or not math.isfinite(args.duration) or args.duration <= 0:
        parser.error('Waktu harus finite; mulai >= 0 dan durasi > 0.')
    if args.width < 16 or args.width % 2:
        parser.error('Lebar preview harus genap, minimal 16.')
    return args


def main(argv=None):
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(errors='replace')
    print('\nMusicMerger | Mirage + karaoke + equalizer | GPU auto / CPU fallback\n')
    try:
        args = options(argv)
        from .workflow import run
        result = run(args)
        print(f'\nSELESAI: {result}\nBuka folder hasil: {result.parent}\n'
              'Upload MP4 saja. File pendukung tetap di MusicMerger-output.')
        return 0
    except (KeyboardInterrupt, EOFError):
        print('\nDibatalkan. Hasil parsial dipertahankan, bukan video final.')
        return 130
    except (OSError, ValueError, RuntimeError) as exc:
        print(f'\nGAGAL: {exc}')
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
