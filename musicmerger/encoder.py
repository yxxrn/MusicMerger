"""GPU H.264 encoding with a real device probe and a bounded CPU fallback.

ASS, equalizer, compositing and crossfade filters remain on the CPU. No change
to frame timestamps or audio; only the final video compression is accelerated.
"""
from pathlib import Path
import subprocess

GPU_ENCODERS = ('h264_nvenc', 'h264_amf', 'h264_qsv')
ENCODERS = ('auto', 'cpu', *GPU_ENCODERS)


def frame_size(source_width, source_height, width=None):
    return (width, 2 * round(source_height * width / source_width / 2)) if width else (source_width, source_height)


def encoder_args(name, quality=20):
    if name == 'libx264':
        return ['-c:v', name, '-preset', 'fast', '-crf', str(quality)]
    if name == 'h264_amf':
        return ['-c:v', name, '-usage', 'transcoding', '-quality', 'quality',
                '-rc', 'cqp', '-qp_i', str(quality), '-qp_p', str(quality), '-qp_b', str(quality)]
    if name == 'h264_nvenc':
        return ['-c:v', name, '-preset', 'p5', '-rc', 'constqp', '-qp', str(quality)]
    if name == 'h264_qsv':
        return ['-c:v', name, '-preset', 'medium', '-global_quality', str(quality)]
    raise ValueError(f'Encoder tidak didukung: {name}')


def probe_encoder(name, width, height, rate):
    command = ['ffmpeg', '-hide_banner', '-loglevel', 'error', '-nostdin',
        '-f', 'lavfi', '-i', f'color=c=black:s={width}x{height}:r={rate}',
        '-frames:v', '12', '-an', *encoder_args(name), '-pix_fmt', 'yuv420p', '-f', 'null', '-']
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=20)
        return result.returncode == 0, result.stderr[-2000:]
    except subprocess.TimeoutExpired:
        return False, 'Probe encoder melebihi 20 detik'


def select_encoder(requested, width, height, rate):
    if requested not in ENCODERS:
        raise ValueError('Pilihan encoder tidak valid')
    config = dict(requested=requested, selected='libx264', probes=[], attempts=[],
                  accelerated_stage='video_encoding_only', filters='cpu')
    if requested == 'cpu':
        return config
    for name in GPU_ENCODERS if requested == 'auto' else (requested,):
        ok, reason = probe_encoder(name, width, height, rate)
        config['probes'].append(dict(encoder=name, success=ok, detail=reason))
        if ok:
            config['selected'] = name
            return config
    if requested != 'auto':
        raise RuntimeError(f'Encoder {requested} tidak siap: {config["probes"][-1]["detail"]}')
    return config


def run_encode(command_factory, partial, log_path, config, *, cwd=None):
    """Keep failed GPU artifacts; retry once using CPU only in auto mode."""
    partial, log_path = Path(partial), Path(log_path)
    if partial.exists() or log_path.exists():
        raise ValueError('Output/log encode sudah ada; gunakan tujuan baru')
    selected = config['selected']
    candidates = [selected]
    if config['requested'] == 'auto' and selected != 'libx264':
        candidates.append('libx264')
    for name in candidates:
        command = command_factory(name)
        with log_path.open('a', encoding='utf-8') as stream:
            stream.write(f'\nENCODER ATTEMPT: {name}\n')
            stream.flush()
            result = subprocess.run(command, cwd=cwd, stdout=stream, stderr=subprocess.STDOUT)
        config['attempts'].append(dict(encoder=name, output=str(partial), returncode=result.returncode))
        if result.returncode == 0:
            config['selected'] = name
            return
        if name == candidates[-1]:
            raise RuntimeError(f'Encode gagal; lihat {log_path}. File partial bukan hasil final.')
        if partial.exists():
            failed = partial.with_name(f'{partial.stem}.failed-{name}{partial.suffix}')
            if failed.exists():
                raise ValueError(f'Arsip kegagalan sudah ada: {failed}')
            partial.rename(failed)
        print(f'Encoder {name} gagal; ulangi dengan CPU (libx264). Log: {log_path}', flush=True)
