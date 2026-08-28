"""Deterministic photo harmonies and local contrast, independent of karaoke colors."""
import colorsys
import math

from PIL import Image, ImageChops, ImageFilter

MIN_CONTRAST = 4.5
# Bounded translucent correction; never replace the photo with an opaque panel.
MAX_ALPHA = {'light': 189, 'dark': 140}


def luminance(rgb):
    channels = [c/255/12.92 if c/255 <= .04045 else ((c/255+.055)/1.055)**2.4 for c in rgb]
    return sum(c*w for c, w in zip(channels, (.2126, .7152, .0722)))


def dominant_colors(frame):
    sample = frame.convert('RGB').resize((160, 90))
    quantized = sample.quantize(colors=12, method=Image.Quantize.MEDIANCUT)
    palette = quantized.getpalette()
    result = []
    for count, index in sorted(quantized.getcolors(), key=lambda item: (-item[0], item[1])):
        rgb = palette[3*index:3*index+3]
        hue, saturation, value = colorsys.rgb_to_hsv(*(c/255 for c in rgb))
        kind = 'shadow' if value < .14 else 'neutral' if saturation < .10 else 'chromatic'
        result.append(dict(rgb=rgb, share=count/(160*90), hue=hue,
                           saturation=saturation, value=value, kind=kind))
    return result


def _distance(a, b):
    delta = abs(a-b)
    return min(delta, 1-delta)


def _hue_sources(clusters):
    # Group nearby hues, not RGB brightness bands. A secondary green patch can
    # survive a majority of neutral pixels without turning into average brown.
    groups = {}
    for cluster in clusters:
        if cluster['kind'] != 'chromatic':
            continue
        weight = cluster['share'] * math.sqrt(cluster['saturation']*cluster['value'])
        bucket = round(cluster['hue']*24) % 24
        groups.setdefault(bucket, []).append((cluster['hue'], weight))
    sources = []
    for group in groups.values():
        weight = sum(w for _, w in group)
        x = sum(math.cos(h*math.tau)*w for h, w in group)
        y = sum(math.sin(h*math.tau)*w for h, w in group)
        sources.append((math.atan2(y, x)/math.tau % 1, weight))
    selected = []
    for hue, weight in sorted(sources, key=lambda item: -item[1]):
        if all(_distance(hue, other) >= .06 for other, _ in selected):
            selected.append((hue, weight))
        if len(selected) == 3:
            break
    return selected


def _tinted(hue, saturation, target):
    # Equal luminance across hues prevents yellow/green winning every palette
    # comparison just because they are intrinsically brighter than blue/red.
    low, high = 0., 1.
    for _ in range(20):
        lightness = (low+high)/2
        rgb = tuple(round(c*255) for c in colorsys.hls_to_rgb(hue, lightness, saturation))
        if luminance(rgb) < target:
            low = lightness
        else:
            high = lightness
    return tuple(round(c*255) for c in colorsys.hls_to_rgb(hue, (low+high)/2, saturation))


def _family(hue):
    for limit, name in ((.055, 'rose'), (.11, 'terracotta'), (.16, 'sand'),
                        (.25, 'olive'), (.43, 'sage'), (.53, 'teal'),
                        (.68, 'slate_blue'), (.80, 'lavender'), (.96, 'rose'), (1., 'burgundy')):
        if hue < limit:
            return name


def _candidates(clusters, energetic):
    sources = _hue_sources(clusters)
    exposure = sum(c['share']*c['value'] for c in clusters)
    for source, weight in sources:
        # Bright amber photos suit a cool counterpoint; darker warm photos keep
        # their copper character. Midtones and foliage use a nearby green hue.
        if source < .11 and exposure > .40:
            preference = {'complementary': 0., 'analogous': .16, 'tonal': .25}
        elif source < .11 and exposure < .26:
            preference = {'tonal': 0., 'analogous': .16, 'complementary': .25}
        else:
            preference = {'analogous': 0., 'tonal': .14, 'complementary': .24}
        for harmony, offset in (('tonal', 0.), ('analogous', .12), ('complementary', .5)):
            hue = (source+offset) % 1
            cost = preference[harmony] + .18*(1-weight/sources[0][1])
            for polarity in ('light', 'dark'):
                yield dict(name=_family(hue), harmony=harmony, source_hue=source,
                           hue=hue, polarity=polarity,
                           main=_tinted(hue, .10, .88 if polarity == 'light' else .009),
                           accent=_tinted(hue, .55 if energetic else .32,
                                          .63 if polarity == 'light' else .035)), cost
    for polarity in ('light', 'dark'):
        yield dict(name='neutral', harmony='neutral', hue=None, source_hue=None, polarity=polarity,
                   main=_tinted(0, 0, .88 if polarity == 'light' else .009),
                   accent=_tinted(0, 0, .63 if polarity == 'light' else .035)), .40 if sources else 0.


def _samples(frame, rows, text_mask=None):
    if not rows:
        raise ValueError('Area teks thumbnail kosong')
    if text_mask is not None and (text_mask.mode != 'L' or text_mask.size != frame.size):
        raise ValueError('Ukuran atau mode text mask tidak valid')
    result = []
    for row in rows:
        left, top, right, bottom = row['box']
        if not (0 <= left < right <= frame.width and 0 <= top < bottom <= frame.height):
            raise ValueError('Area teks thumbnail di luar frame')
        if text_mask is None:
            sample = frame.crop(row['box']).resize((100, 32)).convert('RGB')
            pixels = {rgb for _, rgb in sample.getcolors(maxcolors=sample.width*sample.height)}
        else:
            # Nearest sampling keeps color and actual glyph coverage aligned;
            # averaging would blend the untouched spaces into the text samples.
            sample = frame.crop(row['box']).convert('RGB').resize((100, 32), Image.Resampling.NEAREST)
            ink = text_mask.crop(row['box']).resize(sample.size, Image.Resampling.NEAREST)
            pixels = {sample.getpixel((x, y)) for y in range(sample.height) for x in range(sample.width)
                      if ink.getpixel((x, y)) >= 192}
            if not pixels:
                raise ValueError('Text mask tidak memiliki sampel glyph terbaca')
        # Unique RGB values preserve the minimum contrast with less work.
        result.append(pixels)
    return result


def _ratios(samples, colors, alpha=0):
    target = 0 if colors['polarity'] == 'light' else 255
    ratios = []
    for index, pixels in enumerate(samples):
        foreground = luminance(colors['accent'] if index == len(samples)-1 else colors['main'])
        worst = math.inf
        for rgb in pixels:
            if alpha:
                rgb = tuple((c*(255-alpha)+target*alpha+127)//255 for c in rgb)
            background = luminance(rgb)
            worst = min(worst, (max(foreground, background)+.05)/(min(foreground, background)+.05))
        ratios.append(worst)
    return ratios


def _required_alpha(samples, colors):
    # A small guard band allows RGB rounding after full-resolution compositing.
    def passes(alpha):
        return min(_ratios(samples, colors, alpha)) >= 4.55
    if passes(0):
        return 0
    high = MAX_ALPHA[colors['polarity']]
    if not passes(high):
        return None
    low = 0
    while low+1 < high:
        middle = (low+high)//2
        if passes(middle):
            high = middle
        else:
            low = middle
    return high


def choose_palette(frame, *, energetic=False, rows=None, text_mask=None):
    if rows is None:
        rows = [dict(box=(frame.width//5, frame.height//3, frame.width*4//5, frame.height*2//3))]
    # Use the same pixels as final contrast verification. Box interpolation can
    # smooth away dark details and select a palette the glyph check cannot use.
    samples = _samples(frame, rows, text_mask)
    clusters = dominant_colors(frame)
    choices = []
    for candidate, harmony_cost in _candidates(clusters, energetic):
        alpha = _required_alpha(samples, candidate)
        if alpha is not None:
            # The principal cost is altering the photo, with a smaller harmony term.
            cost = 3*alpha/255 + harmony_cost
            choices.append((cost, candidate, alpha))
    if not choices:
        raise ValueError('Tidak ada palet thumbnail dengan kontras aman')
    _, colors, alpha = min(choices, key=lambda item: item[0])
    return dict(colors, mood='energetic' if energetic else 'calm', dominant_colors=clusters,
                estimated_strength=alpha/255, algorithm='dominant_harmony_local_v2')


def _mask(size, rows):
    boxes = [r['box'] for r in rows]
    left, top = min(b[0] for b in boxes), min(b[1] for b in boxes)
    right, bottom = max(b[2] for b in boxes), max(b[3] for b in boxes)

    def falloff(position, start, end, spread):
        distance = max(start-position, 0, position-end)
        return round(255*math.exp(-.5*(distance/spread)**2))

    x = Image.new('L', (size[0], 1))
    x.putdata([falloff(i, left, right, 60) for i in range(size[0])])
    y = Image.new('L', (1, size[1]))
    y.putdata([falloff(i, top, bottom, 45) for i in range(size[1])])
    return ImageChops.multiply(x.resize(size), y.resize(size))


def contrast_backdrop(frame, rows, colors, *, text_mask=None):
    frame = frame.convert('RGB')
    samples = _samples(frame, rows, text_mask)
    alpha = _required_alpha(samples, colors)
    if alpha is None:
        raise ValueError('Kontras thumbnail tidak memenuhi batas koreksi lokal')
    if alpha:
        if text_mask is None:
            support = _mask(frame.size, rows)
        else:
            # Protect the letters with a narrow feathered halo, not a rectangle
            # spanning the caption, line gaps and empty spaces between words.
            feather = text_mask.filter(ImageFilter.MaxFilter(7)).filter(ImageFilter.GaussianBlur(2))
            core = text_mask.point(lambda v: 255 if v >= 192 else 0)
            support = ImageChops.lighter(feather, core)
        mask = support.point(lambda v: round(v*alpha/255))
        target = 'black' if colors['polarity'] == 'light' else 'white'
        canvas = Image.composite(Image.new('RGB', frame.size, target), frame, mask)
        support_area = 1-mask.histogram()[0]/(frame.width*frame.height)
    else:
        canvas = frame.copy()
        support_area = 0.
    ratios = _ratios(_samples(canvas, rows, text_mask), colors)
    if min(ratios) < MIN_CONTRAST:
        raise ValueError('Verifikasi kontras thumbnail setelah compositing gagal')
    return canvas, dict(strength=alpha/255, ratios=ratios, minimum=MIN_CONTRAST,
                        polarity=colors['polarity'], global_brightness=1.,
                        coverage='glyphs' if text_mask is not None else 'boxes',
                        support_area_fraction=support_area,
                        backdrop=('soft_glyph_halo' if text_mask is not None else
                                  'soft_local_shade' if colors['polarity'] == 'light'
                                  else 'soft_local_lift') if alpha else 'unchanged',
                        method='worst_nearest_glyph_core_pixel' if text_mask is not None
                               else 'worst_resampled_pixel_in_text_boxes')
