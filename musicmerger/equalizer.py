"""Optional audio spectrum overlay. No audio effects or lyric timing changes."""
import math
from fractions import Fraction

MODES = ('off', 'subtle', 'instrumental')


def equalizer_config(mode, width=1920, height=1088, rate='24'):
    if mode not in MODES:
        raise ValueError('Mode equalizer tidak valid')
    if width < 16 or height < 16:
        raise ValueError('Ukuran equalizer tidak valid')
    fps = Fraction(str(rate))
    if not 0 < fps <= 240:
        raise ValueError('Frame rate equalizer tidak valid')
    columns = min(64, max(1, width // 4))
    step = max(1, round(width * .55 / columns))
    return dict(mode=mode, enabled=mode != 'off', width=columns*step,
                height=max(2, round(height*.05)), bottom_margin=round(height*.04),
                columns=columns, step=step, bar_width=max(1, round(step*.35)),
                rate=str(fps), color='white', vocal_opacity=.25, gap_opacity=.55,
                fade_seconds=.5, fft_size=2048, averaging=1, smoothing_frames=3,
                frequency_scale='log', amplitude_scale='log')


def equalizer_overlay_graph(config, windows=(), *, start=0.0, base='base', hidden_windows=()):
    """Consume base and MP3 input 1, produce equalized video and unchanged audio_out.

    Display columns are rasterized FFT bins, not independently aggregated bands.
    Window fades use song time; the spectrum always uses unshifted preview audio.
    """
    if not math.isfinite(start) or start < 0:
        raise ValueError('Awal equalizer tidak valid')
    fades = []
    for begin, end in windows:
        if not all(math.isfinite(v) for v in (begin, end)) or end <= begin:
            raise ValueError('Window equalizer tidak valid')
        fades.append(f'clip(min((T-({begin-start:.6f}))/0.5,(({end-start:.6f})-T)/0.5),0,1)')
    gap = 'clip(' + ('+'.join(fades) or '0') + ',0,1)'
    low = config['vocal_opacity'] if config['mode'] == 'subtle' else 0
    opacity = f'({low}+{config["gap_opacity"]-low}*({gap}))'
    for begin, end in hidden_windows:
        if not all(math.isfinite(v) for v in (begin, end)) or not 0 <= begin < end:
            raise ValueError('Window tersembunyi equalizer tidak valid')
        opacity += f'*(1-gte(T,{begin-start:.6f})*lt(T,{end-start:.6f}))'
    step, bar = config['step'], config['bar_width']
    inset = (step-bar)//2
    mask = f'between(mod(X,{step}),{inset},{inset+bar-1})'
    return (
        '[1:a:0]asplit=2[audio_out][viz_source];'
        '[viz_source]asetpts=PTS-STARTPTS,'
        f'showfreqs=s={config["columns"]}x{config["height"]}:rate={config["rate"]}:'
        'mode=bar:ascale=log:fscale=log:win_size=2048:averaging=1:'
        'colors=white:cmode=combined,'
        f'scale={config["width"]}:{config["height"]}:flags=neighbor,format=gbrap,tmix=frames=3,'
        f"geq=r='255':g='255':b='255':a='alpha(X,Y)*lt(Y,H-1)*{mask}*{opacity}'[bars];"
        f'[{base}][bars]overlay=(W-w)/2:H-h-{config["bottom_margin"]}:'
        'format=auto:eof_action=pass:repeatlast=0[equalized]')
