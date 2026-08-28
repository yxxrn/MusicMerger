"""Composite the user's original JPG as a timed video overlay, without editing it."""
import math
import shutil
from pathlib import Path


from .paths import ROOT

DEFAULT_LOGO_FILE = ROOT / 'assets/images/music logo.jpg'
LOGO_SUPERSAMPLE = 4


def package_music_logo(destination, source=DEFAULT_LOGO_FILE):
    if not source.is_file():
        raise ValueError(f'Gambar musik tidak ditemukan: {source}')
    target = destination / 'assets' / 'music-logo.jpg'
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists() and target.read_bytes() != source.read_bytes():
        raise ValueError(f'Gambar output berbeda dan tidak akan ditimpa: {target}')
    if not target.exists():
        shutil.copyfile(source, target)
    return target


def logo_overlay_graph(windows, *, start=0.0, icon_height=180, fill_rgb=(255, 224, 120)):
    """Consume [base] and input 2 (original JPG), output video [vout].

    Key the baked-in checkerboard before tinting the original silhouette.
    Match the lyric accent with a black contour; animate alpha on the song clock.
    """
    if not math.isfinite(start) or not isinstance(icon_height, int) or icon_height < 16:
        raise ValueError('Waktu/ukuran overlay musik tidak valid')
    if (not isinstance(fill_rgb, (tuple, list)) or len(fill_rgb) != 3 or
            any(type(c) is not int or not 0 <= c <= 255 for c in fill_rgb)):
        raise ValueError('Warna overlay musik harus RGB 0–255')
    red, green, blue = fill_rgb
    fades = []
    for begin, end in windows:
        if not all(math.isfinite(v) for v in (begin, end)) or end <= begin:
            raise ValueError('Window overlay musik tidak valid')
        fade = min(.3, (end-begin)/2)
        fades.append(f'clip(min((T-({begin-start:.6f}))/{fade:g},(({end-start:.6f})-T)/{fade:g}),0,1)')
    opacity = '+'.join(fades) or '0'
    # Key at source resolution, then build a one-output-pixel outline at 4x.
    # Reducing the finished RGBA overlay preserves fractional edge coverage.
    # Keying after the final resize would turn these smooth edges into stairs.
    outline = ','.join(['dilation=threshold0=0:threshold1=0:threshold2=0'] * LOGO_SUPERSAMPLE)
    return (
        '[2:v]setpts=PTS-STARTPTS,format=rgba,colorkey=0xFFFFFF:0.15:0.05,'
        f'format=gbrap,scale=-2:{icon_height*LOGO_SUPERSAMPLE}:flags=lanczos,split[ink][edge];'
        f'[ink]lutrgb=r={red}:g={green}:b={blue}[tinted];'
        '[edge]lutrgb=r=0:g=0:b=0,'
        f'{outline}[contour];'
        '[contour][tinted]overlay=0:0:format=auto,format=gbrap,'
        f'scale=-2:{icon_height}:flags=area,format=gbrap,'
        f"geq=r='r(X,Y)':g='g(X,Y)':b='b(X,Y)':a='alpha(X,Y)*clip({opacity},0,1)'[logo];"
        '[base][logo]overlay=(W-w)/2:(H-h)/2:format=auto:shortest=1[vout]')
