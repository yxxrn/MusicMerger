"""Deterministic thumbnails from local frames, curated fonts and approved metadata."""
import colorsys
import hashlib
import io
import itertools
import json
import math
from pathlib import Path
import re
import subprocess

from PIL import Image, ImageDraw, ImageEnhance, ImageFilter, ImageFont, ImageStat
from fontTools.ttLib import TTFont, TTLibError

from .paths import ROOT
from .publication import _not_link

SIZE = (1280, 720)
MAX_BYTES = 2 * 1024 * 1024


def digest(path):
    with path.open('rb') as source:
        return hashlib.file_digest(source, 'sha256').hexdigest()


def read_metadata(folder, md):
    path = folder / 'youtube-metadata.json'
    if not path.is_file():
        raise ValueError('Siapkan youtube-metadata.json dengan judul/tag dan hash MD, atau gunakan --no-thumbnail.')
    data = json.loads(path.read_text(encoding='utf-8-sig'))
    if not isinstance(data, dict) or data.get('schema_version') != 1:
        raise ValueError('Schema youtube-metadata.json tidak valid')
    for key, limit in (('thumbnail_title', 200), ('youtube_title', 100)):
        value = data.get(key)
        if (not isinstance(value, str) or not value.strip() or len(value) > limit
                or any(ord(c) < 32 for c in value)):
            raise ValueError(f'{key} harus teks satu baris, 1-{limit} karakter')
    tags = data.get('tags')
    if (not isinstance(tags, list) or any(not isinstance(t, str) or not t.strip()
            or any(ord(c) < 32 for c in t) for t in tags)
            or sum(len(t) + (2 if ' ' in t else 0) for t in tags) + max(0, len(tags)-1) > 500):
        raise ValueError('Tags metadata tidak valid atau melewati 500 karakter')
    if data.get('source_md') != md.name or data.get('source_md_sha256') != digest(md):
        raise ValueError('Metadata tidak cocok dengan MD saat ini; review judul/tag lalu perbarui identitas MD.')
    return data


def style_context(md, tags):
    text = md.read_text(encoding='utf-8-sig')
    marker = re.search(r'^\s*(?:#+\s*)?style(?: prompt)?\s*:?\s*$', text, re.I | re.M)
    prompt = text[marker.end():] if marker else ''
    # Negated prompt clauses must not select the very genre being excluded.
    prompt = re.sub(r'\b(?:no|not|without|avoid|tanpa|bukan)\b[^,;.\n]*', '', prompt, flags=re.I)
    return (' '.join(tags) + ' ' + prompt).casefold()


def matches(term, context):
    return bool(re.search(r'(?<!\w)' + re.escape(term.casefold()) + r'(?!\w)', context))


def is_energetic(context):
    return any(matches(t, context) for t in ('energetic', 'high energy', 'upbeat', 'edm',
        'dance', 'punk', 'heavy metal', 'hard rock', 'energik'))


def select_font(directory, title, context):
    directory = Path(directory)
    catalog = directory / 'font-catalog.json'
    if not catalog.is_file():
        catalog = Path(__file__).with_name('thumbnail-fonts.json')
    payload = json.loads(catalog.read_text(encoding='utf-8-sig'))
    if not isinstance(payload, dict) or payload.get('schema_version') != 1 or not isinstance(payload.get('fonts'), list):
        raise ValueError('Katalog font tidak valid')
    candidates, skipped = [], []
    for index, entry in enumerate(payload['fonts']):
        name = entry.get('file') if isinstance(entry, dict) else None
        if not isinstance(name, str) or Path(name).name != name or any(c in name for c in '/\\:'):
            raise ValueError('Path font katalog tidak valid')
        path = directory / name
        _not_link(path)
        if not path.is_file():
            skipped.append(dict(file=name, reason='missing_file')); continue
        try:
            with TTFont(path) as font:
                missing = sorted({c for c in title if ord(c) not in (font.getBestCmap() or {})})
        except (OSError, TTLibError) as exc:
            raise ValueError(f'Font tidak dapat dibaca: {name}') from exc
        if missing:
            skipped.append(dict(file=name, reason='missing_characters', characters=''.join(missing))); continue
        genres, moods = entry.get('genre_hints', []), entry.get('mood_hints', [])
        if not isinstance(genres,list) or not isinstance(moods,list):
            raise ValueError('Petunjuk genre/mood katalog harus berupa daftar')
        hints = genres + moods
        if any(not isinstance(h, str) for h in hints):
            raise ValueError('Petunjuk genre/mood katalog tidak valid')
        matched = [h for h in hints if matches(h, context)]
        score = sum(2 + len(h.split()) for h in matched)
        # Decorative manuscript faces are only selected for their intended mood.
        if 'morris' in name.casefold() and not matched:
            score -= 20
        if len(title) > 32 and 'orphans' in name.casefold():
            score += 2
        candidates.append((score, -index, dict(path=path, file=name, family=entry.get('family', path.stem),
                           matched_hints=matched, score=score, sha256=digest(path))))
    if not candidates:
        raise ValueError(f'Tidak ada font lokal yang mendukung semua karakter judul di {directory}. Periksa --font-dir.')
    selected = max(candidates, key=lambda item: item[:2])[2]
    selected['skipped'] = skipped
    return selected


def layout_title(title, font_path):
    words = title.split()
    if not words:
        raise ValueError('Judul thumbnail kosong')
    if len(words) > 30:
        raise ValueError('Judul thumbnail terlalu panjang; ringkas thumbnail_title')
    options = []
    for count in ([1] if len(words) == 1 else range(2, min(3, len(words)) + 1)):
        for cuts in itertools.combinations(range(1, len(words)), count-1):
            edges = (0, *cuts, len(words))
            lines = [' '.join(words[edges[i]:edges[i+1]]) for i in range(count)]
            rows = []
            for i, text in enumerate(lines):
                size = 228 if i == count-1 else 112
                while size >= 40:
                    box = ImageFont.truetype(str(font_path), size).getbbox(text)
                    if box[2]-box[0] <= 1040:
                        break
                    size -= 1
                rows.append(dict(text=text, size=size, glyph_box=box))
            while sum(r['glyph_box'][3]-r['glyph_box'][1] for r in rows) + 18*(count-1) > 340:
                for r in rows:
                    r['size'] -= 1
                    r['glyph_box'] = ImageFont.truetype(str(font_path), max(1,r['size'])).getbbox(r['text'])
            if min(r['size'] for r in rows) < 40:
                continue
            score = min(r['size'] for r in rows) + .2*max(r['size'] for r in rows) - 12*(count-2)
            if lines[-1].split()[0].casefold() in ('the', 'a', 'an', 'of', 'and', 'to', 'in'):
                score -= 24
            options.append((score, rows))
    if not options:
        raise ValueError('Judul tidak muat dengan ukuran terbaca; ringkas thumbnail_title')
    rows = max(options, key=lambda item: item[0])[1]
    height = sum(r['glyph_box'][3]-r['glyph_box'][1] for r in rows) + 18*(len(rows)-1)
    y = round(385-height/2)
    for row in rows:
        left, top, right, bottom = row.pop('glyph_box')
        x = round((1280-(right-left))/2)
        row.update(origin=(x-left, y-top), box=(x,y,x+right-left,y+bottom-top))
        y += bottom-top+18
    return rows


def choose_colors(frame, context):
    mean = ImageStat.Stat(frame.resize((32,18)).convert('RGB')).mean
    hue, saturation, _ = colorsys.rgb_to_hsv(*(c/255 for c in mean))
    name = ('gold' if saturation < .12 or hue < .16 or hue > .94 else
            'mint' if hue < .46 else 'blue' if hue < .72 else 'rose')
    calm = dict(gold=(235,193,126), mint=(176,210,177), blue=(170,204,227), rose=(223,176,192))
    vivid = dict(gold=(255,207,99), mint=(139,237,165), blue=(126,215,255), rose=(253,159,197))
    energy = is_energetic(context)
    return dict(name=name, mood='energetic' if energy else 'calm',
                accent=(vivid if energy else calm)[name], main=(249,237,207) if name=='gold' else (241,243,236))


def luminance(rgb):
    channels = [c/255/12.92 if c/255 <= .04045 else ((c/255+.055)/1.055)**2.4 for c in rgb]
    return sum(c*w for c,w in zip(channels, (.2126,.7152,.0722)))


def contrast_backdrop(frame, rows, colors):
    """Darken the photo, never add a panel; test the brightest sampled text background."""
    base = ImageEnhance.Brightness(frame.convert('RGB')).enhance(.88)
    mask = Image.new('L',frame.size)
    boxes = [r['box'] for r in rows]
    region = (min(b[0] for b in boxes)-140, min(b[1] for b in boxes)-100,
              max(b[2] for b in boxes)+140, max(b[3] for b in boxes)+100)
    ImageDraw.Draw(mask).rounded_rectangle(region, radius=110, fill=255)
    mask = mask.filter(ImageFilter.GaussianBlur(65))
    for brightness in (.68, .60, .52, .44, .36, .28, .20):
        darker = ImageEnhance.Brightness(frame.convert('RGB')).enhance(brightness)
        shaded = Image.composite(darker, base, mask)
        ratios = []
        for index, row in enumerate(rows):
            sample = shaded.crop(row['box']).resize((100,32))
            pixels = (sample.getpixel((x,y)) for y in range(sample.height) for x in range(sample.width))
            background = max(luminance(p) for p in pixels)
            color = colors['accent'] if index == len(rows)-1 else colors['main']
            ratios.append((luminance(color)+.05)/(background+.05))
        if min(ratios) >= 4.5:
            return shaded, dict(brightness=brightness, ratios=ratios, minimum=4.5, backdrop='soft_local_shade',
                                method='brightest_resampled_pixel_in_text_boxes')
    raise ValueError('Kontras thumbnail tidak memenuhi batas')


def extract_frame(video, output):
    probe = subprocess.run(['ffprobe','-v','error','-show_entries','format=duration','-of','json',str(video)],
                           check=True, capture_output=True, timeout=30)
    duration = float(json.loads(probe.stdout)['format']['duration'])
    if not math.isfinite(duration) or duration <= 0:
        raise ValueError('Durasi video thumbnail tidak valid')
    candidates = []
    # Prefer the approved 40% position when frames have comparable quality.
    for fraction in (.40, .25, .65):
        timestamp = min(duration*fraction, max(0,duration-.1))
        result = subprocess.run(['ffmpeg','-hide_banner','-loglevel','error','-nostdin','-ss',str(timestamp),
            '-i',str(video),'-frames:v','1','-vf','scale=1280:720:force_original_aspect_ratio=increase,crop=1280:720',
            '-f','image2pipe','-vcodec','png','pipe:1'], check=True,capture_output=True,timeout=60)
        frame = Image.open(io.BytesIO(result.stdout)).convert('RGB')
        sample = frame.resize((128,72)).convert('L')
        mean = ImageStat.Stat(sample).mean[0]
        score = ImageStat.Stat(sample).stddev[0] - abs(mean-120)*.2
        if mean < 12 or mean > 245:
            score -= 100
        candidates.append((score, frame, timestamp))
    best = max(candidates, key=lambda item:item[0])
    _, frame, timestamp = candidates[0] if candidates[0][0] >= best[0]-5 else best
    return frame, timestamp


def generate(folder, video, audio, md, output, *, font_dir=None):
    """Create support artifacts only. Publication owns replacement/rollback."""
    for name in ('thumbnail.jpg','youtube-upload.txt','thumbnail-report.json','thumbnail-frame.png'):
        target = output / name
        _not_link(target)
        if target.exists():
            raise ValueError(f'Output thumbnail sudah ada: {target}')
    data = read_metadata(folder, md)
    title = ' '.join(data['thumbnail_title'].split())
    context = style_context(md, data['tags'])
    font = select_font(font_dir or ROOT/'listfont', title, context)
    rows = layout_title(title, font['path'])
    sources = {p: digest(p) for p in (video,audio,md,folder/'youtube-metadata.json',font['path'])}
    try:
        frame, timestamp = extract_frame(video, output)
    except subprocess.SubprocessError as exc:
        raise RuntimeError(f'FFmpeg/ffprobe gagal saat mengambil frame thumbnail: {exc}') from exc
    colors = choose_colors(frame, context)
    caption = ' & '.join(font['matched_hints'][:2]).upper()
    caption_font = ImageFont.load_default(size=20)
    caption_box = None
    if caption and len(caption) <= 40 and caption.isascii():
        caption_box = ImageDraw.Draw(frame).textbbox((640,150), caption, font=caption_font, anchor='mt')
    else:
        caption = ''
    contrast_rows = ([dict(box=caption_box)] if caption_box else []) + rows
    canvas, contrast = contrast_backdrop(frame, contrast_rows, colors)
    shadow = Image.new('RGBA',SIZE)
    shadow_draw = ImageDraw.Draw(shadow)
    for row in rows:
        face = ImageFont.truetype(str(font['path']), row['size'])
        x,y = row['origin']
        shadow_draw.text((x,y+4),row['text'],font=face,fill=(10,15,9,150))
    canvas = Image.alpha_composite(canvas.convert('RGBA'), shadow.filter(ImageFilter.GaussianBlur(7)))
    draw = ImageDraw.Draw(canvas)
    for index,row in enumerate(rows):
        draw.text(row['origin'],row['text'],font=ImageFont.truetype(str(font['path']),row['size']),
                  fill=colors['accent'] if index==len(rows)-1 else colors['main'])
    # Genre label is optional and uses only the explicitly matched catalog hints.
    if caption:
        draw.text((640,150),caption,font=caption_font,anchor='mt',fill=colors['main'])
    target = output/'thumbnail.jpg'
    for quality in (95,90,85):
        buffer = io.BytesIO()
        canvas.convert('RGB').save(buffer,format='JPEG',quality=quality,subsampling=0)
        if buffer.tell() < MAX_BYTES:
            break
    else:
        raise ValueError('Thumbnail melebihi 2 MiB')
    with target.open('xb') as stream:
        stream.write(buffer.getvalue())
    with Image.open(target) as check:
        check.load()
        if check.size != SIZE or check.format != 'JPEG':
            raise ValueError('Verifikasi JPEG gagal')
    frame.save(output/'thumbnail-frame.png')
    upload = output/'youtube-upload.txt'
    with upload.open('x',encoding='utf-8',newline='\n') as stream:
        stream.write(f"JUDUL YOUTUBE\n{data['youtube_title']}\n\nTAGS\n{', '.join(data['tags'])}\n")
    for path,before in sources.items():
        if digest(path) != before:
            raise ValueError(f'Input berubah selama thumbnail dibuat: {path}')
    report = dict(schema=1, title=title, font={k:str(v) if isinstance(v,Path) else v for k,v in font.items()},
                  frame_seconds=timestamp, colors=colors, contrast=contrast, layout=rows, caption=caption,
                  caption_box=caption_box,
                  size=SIZE, bytes=target.stat().st_size, sha256=digest(target),
                  inputs={str(p):h for p,h in sources.items()}, method='local_frame_pillow_no_llm')
    with (output/'thumbnail-report.json').open('x',encoding='utf-8') as stream:
        json.dump(report,stream,ensure_ascii=False,indent=2)
    return {'thumbnail.jpg':target,'youtube-upload.txt':upload}
