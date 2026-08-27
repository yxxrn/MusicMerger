"""Prepare a cyclic background with a frame-aligned tail-to-head dissolve."""
from fractions import Fraction
import json
import math
from pathlib import Path
import subprocess
from .encoder import encoder_args, run_encode


def loop_config(duration, rate):
    try:
        fps = Fraction(str(rate))
    except (ValueError, ZeroDivisionError) as exc:
        raise ValueError('Frame rate background tidak valid') from exc
    if not math.isfinite(duration) or duration <= 0 or not 0 < fps <= 240:
        raise ValueError('Durasi/frame rate background tidak valid')
    frames = round(duration * fps)
    if frames < 4:
        raise ValueError('Background terlalu pendek untuk seamless loop; gunakan --loop-mode hard')
    overlap = min(max(1, round(fps)), frames // 4)
    return dict(rate=str(fps), source_frames=frames, fade_frames=overlap,
                cycle_frames=frames-overlap, fade_seconds=float(overlap/fps),
                cycle_seconds=float((frames-overlap)/fps),
                source_start_seconds=float(overlap/fps),
                transition='fade', method='tail_head_crossfade_not_optical_flow')


def loop_command(source, target, config, encoder='libx264'):
    n, fade, rate = config['source_frames'], config['fade_frames'], config['rate']
    offset = (n-2*fade)/Fraction(rate)
    # Start at the end of the head segment. Finish by blending tail -> head.
    # Thus the last frame approaches source frame fade-1, followed by frame fade
    # on the next cycle; no repeated first frame, reverse playback or frozen tail.
    graph = (
        f'[0:v:0]fps={rate},trim=end_frame={n},settb=AVTB,setpts=PTS-STARTPTS,'
        'format=yuv444p,split[body][head];'
        f'[body]trim=start_frame={fade},setpts=PTS-STARTPTS[rest];'
        f'[head]trim=end_frame={fade},setpts=PTS-STARTPTS[first];'
        f'[rest][first]xfade=transition=fade:duration={config["fade_seconds"]:.9f}:'
        f'offset={float(offset):.9f},trim=end_frame={config["cycle_frames"]},'
        'setpts=PTS-STARTPTS,format=yuv420p[cycle]')
    return ['ffmpeg','-hide_banner','-nostdin','-n','-i',str(source),
            '-filter_complex',graph,'-map','[cycle]','-an',
            '-frames:v',str(config['cycle_frames']),*encoder_args(encoder, quality=16),
            '-movflags','+faststart',str(target)]


def prepare_background_loop(source, destination, config, encoding=None):
    assets = Path(destination) / 'assets'
    assets.mkdir(parents=True, exist_ok=True)
    target, partial = assets/'background-loop.mp4', assets/'background-loop.partial.mp4'
    log_path = assets/'background-loop.ffmpeg.log'
    if any(p.exists() for p in (target,partial,log_path)):
        raise ValueError('Background loop sudah ada; gunakan --out baru')
    if encoding is None:
        encoding = dict(requested='cpu', selected='libx264', attempts=[])
    run_encode(lambda name: loop_command(source, partial, config, name), partial, log_path, encoding)
    result = subprocess.run(['ffprobe','-v','error','-show_streams','-of','json',str(partial)],
                            check=True,capture_output=True,text=True)
    streams = json.loads(result.stdout)['streams']
    if (len(streams) != 1 or streams[0]['codec_type'] != 'video' or
            int(streams[0].get('nb_frames',0)) != config['cycle_frames'] or
            Fraction(streams[0]['avg_frame_rate']) != Fraction(config['rate'])):
        raise RuntimeError('Jumlah frame/rate background loop tidak sesuai; file masih partial')
    if target.exists():
        raise ValueError(f'Tujuan loop muncul selama proses; tidak ditimpa: {target}')
    partial.rename(target)
    return target
