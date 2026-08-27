#!/usr/bin/env python
"""Gabungkan video, MP3, dan lirik Markdown menjadi video karaoke.

Paket lagu berada di inputs; output default di outputs. Timing dari ASR adalah
perkiraan, bukan forced alignment. Lihat README untuk preview dan pemeriksaan.
"""
import argparse
import colorsys
import hashlib
import json
import math
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from .timing import WORD_RE, norm, validate_words, build_line_timing
from .phrases import split_phrases
from .logo import DEFAULT_LOGO_FILE, LOGO_SUPERSAMPLE, package_music_logo, logo_overlay_graph
from .equalizer import MODES, equalizer_config, equalizer_overlay_graph
from .loop import loop_config, prepare_background_loop
from .acoustic import apply_timing_override
from .encoder import ENCODERS, encoder_args, select_encoder, run_encode, frame_size

from .paths import ROOT
MODEL = "small"
DEFAULT_FONT = "MADE Mirage"
DEFAULT_EQUALIZER = 'subtle'
DEFAULT_FONT_FILE = ROOT / "assets/fonts/MADE Mirage Bold PERSONAL USE.otf"
FADE_OUT_MS = 180
PROMOTION_MS = 320
PHRASE_FADE_MS = 120
PHRASE_HOLD_MS = 400
PHRASE_FADE_OUT_MS = 300
INSTRUMENTAL_MIN_SECONDS = 5.0
INSTRUMENTAL_PADDING_SECONDS = 0.4
PALETTES = {
    "gold": (255, 224, 120), "cyan": (115, 245, 255),
    "mint": (180, 255, 190), "rose": (255, 188, 230),
}

# ---------------------------------------------------------------- utilities

def log(msg: str):
    print(msg, flush=True)

def ass_ts(sec: float) -> str:
    h, rem = divmod(centiseconds(sec), 360000)
    m, rem = divmod(rem, 6000)
    s, fraction = divmod(rem, 100)
    return f"{h}:{m:02d}:{s:02d}.{fraction:02d}"


def centiseconds(seconds):
    if not math.isfinite(seconds):
        raise ValueError("Timestamp harus finite")
    return max(0, int(math.floor(seconds * 100 + 0.5)))

def esc(s: str) -> str:
    return s.replace("\\", "＼").replace("{", "｛").replace("}", "｝")

# ---------------------------------------------------------------- lyrics.md

STOP_RE = re.compile(r"^(style(?: prompt)?|instructions?|notes?|catatan)\b", re.I)

def parse_lyrics(md_path: Path):
    """Read lyrics without metadata; support numbered sections and Unicode."""
    raw_lines = md_path.read_text(encoding="utf-8-sig").splitlines()
    # Plain lyric files need no synthetic heading. Explicit sections retain the
    # original preamble/metadata behavior.
    structured = any(re.fullmatch(r"\s*(?:#+\s*)?(?:lyrics|lirik)\s*", raw, re.I)
                     or re.fullmatch(r"\s*\[[^\]]+\]\s*", raw) for raw in raw_lines)
    lines, label, started = [], "verse", not structured
    for raw in raw_lines:
        line = raw.strip()
        heading = re.sub(r"^#+\s*", "", line).strip()
        if not line:
            continue
        if heading.casefold() in ("lyrics", "lirik"):
            started = True
            continue
        if started and (STOP_RE.fullmatch(heading.rstrip(":")) or (line.startswith("#") and STOP_RE.match(heading))):
            break
        m = re.fullmatch(r"\[([^\]]+)\]", line)
        if m:
            started = True
            label = re.sub(r"\s+\d+$", "", m[1].casefold()).replace("-", " ")
            continue
        if started and not line.startswith("#") and WORD_RE.search(line):
            lines.append((label, line))
    if not lines:
        raise ValueError(f"Tidak ada lirik di {md_path}; gunakan Lyrics atau [Verse].")
    return lines

# ---------------------------------------------------------------- timing

def audio_fingerprint(path):
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return {"sha256": digest.hexdigest(), "bytes": path.stat().st_size}


def whisper_words(mp3_path, cache_path, *, language="en", model_name=MODEL,
                  trust_legacy=False, refresh=False):
    identity = audio_fingerprint(mp3_path)
    if cache_path.exists() and not refresh:
        data = json.loads(cache_path.read_text(encoding="utf-8-sig"))
        if not isinstance(data, dict):
            raise ValueError("Format cache harus berupa objek JSON")
        if data.get("schema") == 2:
            if (data.get("audio") != identity or data.get("model") != model_name
                    or data.get("language") != language):
                raise ValueError("Cache tidak cocok dengan audio/model/bahasa. Gunakan --refresh-cache.")
        elif trust_legacy and "schema" not in data and data.get("model") == model_name:
            log("PERINGATAN: cache lama tanpa identitas audio dipakai atas permintaan eksplisit.")
        else:
            raise ValueError("Cache lama/tidak dikenal. Gunakan --refresh-cache atau --trust-legacy-cache untuk review.")
        return validate_words(data.get("words"))
    backup = cache_path.with_name(cache_path.name + ".bak")
    if cache_path.exists() and backup.exists():
        raise ValueError(f"Backup cache sudah ada: {backup}; simpan/rename sebelum refresh lagi.")
    from faster_whisper import WhisperModel
    log(f"Transkripsi {model_name} CPU; model dapat diunduh jika belum tersedia...")
    model = WhisperModel(model_name, device="cpu", compute_type="int8")
    segments, transcription_info = model.transcribe(str(mp3_path), word_timestamps=True, beam_size=5,
                                   language=None if language == "auto" else language)
    words = [{"w": w.word.strip(), "start": w.start, "end": w.end}
             for segment in segments for w in (segment.words or []) if norm(w.word)]
    validate_words(words)
    data = {"schema": 2, "audio": identity, "model": model_name, "language": language, "words": words,
            "detected_language": getattr(transcription_info, 'language', None)}
    if cache_path.exists():
        with backup.open("xb") as stream:
            stream.write(cache_path.read_bytes())
    temporary = cache_path.with_name(cache_path.name + ".tmp")
    with temporary.open("x", encoding="utf-8") as stream:
        json.dump(data, stream, ensure_ascii=False, indent=2)
    temporary.replace(cache_path)
    return words

# ---------------------------------------------------------------- ASS

def display_words(text):
    """Display only: preserve token count and timing, remove punctuation."""
    return [word.replace("'", "").replace("’", "") for word in WORD_RE.findall(text)]


def palette_color(name):
    r, g, b = PALETTES[name]
    return {"name": name, "highlight": f"&H00{b:02X}{g:02X}{r:02X}&"}


def choose_palette(rgb_bytes):
    """Choose an accent once per video; text outline supports local contrast."""
    if not rgb_bytes or len(rgb_bytes) % 3:
        raise ValueError("Sampel RGB background tidak valid")
    rgb = [sum(rgb_bytes[c::3]) / (len(rgb_bytes) // 3) / 255 for c in range(3)]
    hue, saturation, value = colorsys.rgb_to_hsv(*rgb)
    if saturation < 0.15 or value < 0.08:
        return palette_color("gold")

    def distance(name):
        candidate_hue = colorsys.rgb_to_hsv(*(c / 255 for c in PALETTES[name]))[0]
        delta = abs(candidate_hue - hue)
        return min(delta, 1 - delta)

    return palette_color(max(PALETTES, key=distance))


def analyze_background(video, duration):
    interval = max(float(duration) / 6, 0.1)
    command = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-i", str(video),
               "-vf", f"fps=1/{interval},crop=iw:ih*0.40:0:ih*0.35,scale=32:12",
               "-frames:v", "6", "-f", "rawvideo", "-pix_fmt", "rgb24", "pipe:1"]
    result = subprocess.run(command, check=True, capture_output=True)
    selected = choose_palette(result.stdout)
    selected["sampled_pixels"] = len(result.stdout) // 3
    return selected


def package_font(destination, font):
    """Load supplied font locally without changing Windows font installation."""
    directory = destination / "fonts"
    directory.mkdir(parents=True, exist_ok=True)
    if font == DEFAULT_FONT:
        if not DEFAULT_FONT_FILE.is_file():
            raise ValueError(f"Font Mirage tidak ditemukan: {DEFAULT_FONT_FILE}")
        target = directory / "Mirage.otf"
        if target.exists() and target.read_bytes() != DEFAULT_FONT_FILE.read_bytes():
            raise ValueError(f"Font output berbeda dan tidak akan ditimpa: {target}")
        if not target.exists():
            shutil.copyfile(DEFAULT_FONT_FILE, target)
    return directory


def karaoke_tags(line) -> str:
    visible = display_words(line["text"])
    if len(visible) != len(line["words"]):
        raise ValueError("Jumlah kata dan timing berbeda")
    result, cursor = [], 0
    origin = centiseconds(line.get("event_start", line["wstart"]))
    for i, (word, (start_ms, end_ms)) in enumerate(zip(visible, line["words"])):
        start = max(cursor, centiseconds(start_ms / 1000) - origin)
        end = max(start, centiseconds(end_ms / 1000) - origin)
        if start > cursor:
            result.append(f"{{\\k{start - cursor}}}")
        space = " " if i + 1 < len(visible) else ""
        result.append(f"{{\\kf{end - start}}}{word}{space}")
        cursor = end
    return "".join(result)

def phrase_font_size(width, height):
    return max(12, round(min(height * 0.14, width * 0.12)))


def instrumental_windows(lines, *, song_duration=None, offset=0.0, lyric_tail_seconds=0.0):
    """Long gaps between known lyric lines, not acoustic vocal detection.

    An unaligned line blocks the neighboring gap. Outro requires full audio
    duration, never the end of a requested preview window.
    """
    if (not math.isfinite(lyric_tail_seconds) or lyric_tail_seconds < 0
            or not math.isfinite(offset)) or (song_duration is not None and
            (not math.isfinite(song_duration) or song_duration <= 0)):
        raise ValueError('Durasi/offset indikator instrumental tidak valid')
    if not lines:
        return []
    gaps = []
    if lines[0].get('wstart') is not None:
        gaps.append((0, lines[0]['wstart'] + offset, False))
    for previous, following in zip(lines, lines[1:]):
        if previous.get('wend') is not None and following.get('wstart') is not None:
            gaps.append((previous['wend'] + offset, following['wstart'] + offset, True))
    if song_duration is not None and lines[-1].get('wend') is not None:
        gaps.append((lines[-1]['wend'] + offset, song_duration, True))
    windows = []
    for start, end, after_lyric in gaps:
        start = max(0, start)
        if song_duration is not None:
            end = min(end, song_duration)
        if end - start >= INSTRUMENTAL_MIN_SECONDS:
            padding = (max(INSTRUMENTAL_PADDING_SECONDS, lyric_tail_seconds + .1)
                       if after_lyric else INSTRUMENTAL_PADDING_SECONDS)
            begin, stop = round(start + padding, 2), round(end - INSTRUMENTAL_PADDING_SECONDS, 2)
            if stop > begin:
                windows.append((begin, stop))
    return windows


def build_phrase_ass(lines, out_path, *, width, height, font, offset, highlight):
    phrases = split_phrases(lines)
    size, margin = phrase_font_size(width, height), round(width * 0.07)
    outline = max(1, round(size * 0.022, 1))
    center, middle = width // 2, round(height * 0.5)
    header = f'''[Script Info]
ScriptType: v4.00+
PlayResX: {width}
PlayResY: {height}
WrapStyle: 0
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Main,{font},{size},{highlight},&H00FFFFFF,&H00090909,&H80000000,-1,0,0,0,100,100,0,0,1,{outline},1,5,{margin},{margin},0,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
'''
    records = []
    for phrase in phrases:
        if phrase['wend'] + offset <= 0:
            continue
        first = centiseconds(phrase['wstart'] + offset)
        last = centiseconds(phrase['wend'] + offset)
        records.append((phrase, first, last))
    body = []
    for i, (phrase, first, last) in enumerate(records):
        # Keep the existing white lead-in. Spend remaining silence on a fully
        # highlighted hold, then fade. Never move a word onset or overlap text.
        lead = min(PHRASE_FADE_MS // 10, first)
        tail = (PHRASE_HOLD_MS + PHRASE_FADE_OUT_MS) // 10
        if i:
            lead = min(lead, max(0, (first - records[i - 1][2]) // 2))
        if i + 1 < len(records):
            gap = max(0, records[i + 1][1] - last)
            next_lead = min(PHRASE_FADE_MS // 10, gap // 2)
            tail = min(tail, gap - next_lead)
        fade_out = min(PHRASE_FADE_OUT_MS // 10, tail // 2)
        start, end = first - lead, last + tail
        if end <= start:
            continue
        line = dict(phrase, event_start=start / 100)
        line['words'] = [(s + offset * 1000, e + offset * 1000) for s, e in phrase['words']]
        tags = f'{{\\an5\\pos({center},{middle})\\fad({lead * 10},{fade_out * 10})}}'
        body.append(f'Dialogue: 2,{ass_ts(start / 100)},{ass_ts(end / 100)},Main,,0,0,0,,{tags}{karaoke_tags(line)}')
    out_path.write_text(header + '\n'.join(body) + '\n', encoding='utf-8')


def build_ass(lines, out_path: Path, *, width=1920, height=1088, font=DEFAULT_FONT,
              show_next=True, offset=0.0, highlight='&H006BDFFF&', layout='phrases'):
    timed = [line for line in lines if line.get('wstart') is not None]
    if any(a['wend'] > b['wstart'] + 0.000001 for a, b in zip(timed, timed[1:])):
        raise ValueError('Timing antarbaris tumpang tindih; koreksi timing sebelum membuat subtitle')
    if width < 16 or height < 16 or not math.isfinite(offset):
        raise ValueError('Resolusi/offset subtitle tidak valid')
    if any(char in font for char in (',', '\r', '\n', '{', '}', '\\')):
        raise ValueError('Nama font tidak valid')
    if not re.fullmatch(r'&H00[0-9a-fA-F]{6}&', highlight):
        raise ValueError('Warna highlight harus berupa warna ASS opaque')
    if layout not in ('phrases', 'lines'):
        raise ValueError('Layout subtitle tidak valid')
    if layout == 'phrases':
        build_phrase_ass(lines, out_path, width=width, height=height, font=font,
                         offset=offset, highlight=highlight)
        return
    size = max(12, round(min(height * 0.085, width * 0.075)))
    small, margin = max(10, round(size * 0.72)), round(width * 0.07)
    outline = max(1, round(size * 0.035, 1))
    main_y, next_y, center = round(height * 0.50), round(height * 0.65), width // 2
    header = f'''[Script Info]
ScriptType: v4.00+
PlayResX: {width}
PlayResY: {height}
WrapStyle: 0
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Main,{font},{size},{highlight},&H00FFFFFF,&H00090909,&H80000000,-1,0,0,0,100,100,0,0,1,{outline},1,5,{margin},{margin},0,1
Style: Next,{font},{small},&H00FFFFFF,&H00FFFFFF,&H00090909,&H80000000,-1,0,0,0,100,100,0,0,1,{outline},1,5,{margin},{margin},0,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
'''
    records = []
    for index, original in enumerate(lines):
        if original.get('wstart') is None or not original.get('words'):
            continue
        line = dict(original)
        line['wstart'], line['wend'] = original['wstart'] + offset, original['wend'] + offset
        line['words'] = [(s + offset * 1000, e + offset * 1000) for s, e in original['words']]
        if line['wend'] <= 0:
            continue
        previous = lines[index - 1] if index else None
        promote = bool(show_next and previous and previous.get('wend') is not None
                       and previous['wend'] + offset >= 0
                       and 0 <= original['wstart'] - previous['wend'] < 4)
        start = (centiseconds(previous['wend'] + offset) / 100 if promote
                 else centiseconds(line['wstart'] - 0.15) / 100)
        end = (centiseconds(line['wend']) + FADE_OUT_MS // 10) / 100
        if end <= start:
            continue
        move_ms = min(PROMOTION_MS, max(10, round((end - start) * 1000) - FADE_OUT_MS)) if promote else 0
        line['event_start'] = start
        records.append({'index': index, 'line': line, 'start': start, 'end': end,
                        'promote': promote, 'move_ms': move_ms})

    # A very short predecessor may have no time to display the next preview.
    # Do not promote a line the viewer never saw; use a guarded static handoff.
    for previous, current in zip(records, records[1:]):
        preview_start = previous['start'] + previous['move_ms'] / 1000 + 0.1
        if current['promote'] and centiseconds(preview_start) >= centiseconds(current['start']):
            current['promote'], current['move_ms'] = False, 0
        if not current['promote']:
            previous['end'] = min(previous['end'], centiseconds(current['line']['wstart']) / 100)
            current['start'] = max(current['start'], previous['end'])
            current['line']['event_start'] = current['start']

    body = []
    for index, record in enumerate(records):
        line, start, end = record['line'], record['start'], record['end']
        fade_out = min(FADE_OUT_MS, max(0, round((end - line['wend']) * 1000)))
        if record['promote']:
            move_ms = record['move_ms']
            # Same anchor, font weight, color and starting size as the preceding
            # Next event. Its exact endpoint is this event's start: no duplicate.
            lead = (f'{{\\an5\\fs{small}\\move({center},{next_y},{center},{main_y},0,{move_ms})'
                    f'\\t(0,{move_ms},\\fs{size})\\fad(0,{fade_out})}}')
        else:
            fade_in = min(100, max(0, round((end - start) * 1000) - fade_out))
            lead = f'{{\\an5\\pos({center},{main_y})\\fad({fade_in},{fade_out})}}'
        body.append(f'Dialogue: 2,{ass_ts(start)},{ass_ts(end)},Main,,0,0,0,,{lead}{karaoke_tags(line)}')
        if show_next and index + 1 < len(records):
            following = records[index + 1]
            if following['promote'] and following['index'] == record['index'] + 1:
                preview_start = start + record['move_ms'] / 1000 + 0.1
                preview_end = following['start']
                if centiseconds(preview_end) > centiseconds(preview_start):
                    lead_next = f'{{\\an5\\pos({center},{next_y})\\fad(100,0)}}'
                    text = ' '.join(display_words(following['line']['text']))
                    body.append(f'Dialogue: 1,{ass_ts(preview_start)},{ass_ts(preview_end)},Next,,0,0,0,,{lead_next}{text}')
    out_path.write_text(header + '\n'.join(body) + '\n', encoding='utf-8')


# ---------------------------------------------------------------- ffmpeg

def probe(path):
    result = subprocess.run(['ffprobe', '-v', 'error', '-show_streams', '-show_format', '-of', 'json', str(path)],
                            check=True, capture_output=True, text=True, encoding='utf-8', errors='replace')
    return json.loads(result.stdout)


def ffprobe_duration(path):
    duration = float(probe(path)['format']['duration'])
    if not math.isfinite(duration) or duration <= 0:
        raise ValueError(f'Durasi tidak valid: {path}')
    return duration


def input_files(folder):
    if not folder.is_dir():
        raise ValueError(f'Folder input tidak ada: {folder}')
    files = []
    for extension in ('.mp4', '.mp3', '.md'):
        found = sorted(p for p in folder.iterdir() if p.is_file() and p.suffix.lower() == extension)
        if len(found) != 1:
            raise ValueError(f'{folder}: perlu tepat satu {extension}, ditemukan {len(found)}')
        files.append(found[0].resolve())
    return tuple(files)


def render_command(mp4, mp3, output, *, start=0.0, duration, width=None,
                   icon_file=None, instrumental_intervals=(), icon_height=180,
                   equalizer='off', equalizer_intervals=(), height=1088, rate='24', video_width=1920,
                   icon_rgb=(255, 224, 120), encoder='libx264'):
    config = equalizer_config(equalizer, width or video_width, height, rate)
    filters = [f'scale={width}:-2'] if width else []
    # Seeking resets PTS; restore song time for ASS, then reset for output.
    filters += [f'setpts=PTS-STARTPTS+{start}/TB', 'ass=lyrics.ass:fontsdir=fonts', 'setpts=PTS-STARTPTS']
    command = ['ffmpeg', '-hide_banner', '-nostdin', '-n', '-stream_loop', '-1',
               '-ss', str(start), '-i', str(mp4), '-ss', str(start), '-i', str(mp3)]
    windows = [(a,b) for a,b in instrumental_intervals if b > start and a < start + duration]
    has_logo = icon_file is not None and bool(windows)
    if has_logo:
        command += ['-loop', '1', '-framerate', '30', '-i', str(icon_file)]
        graph = '[0:v]' + ','.join(filters) + '[base];'
        graph += logo_overlay_graph(windows, start=start, icon_height=icon_height, fill_rgb=icon_rgb)
    elif equalizer != 'off':
        graph = '[0:v]' + ','.join(filters) + '[base]'
    if equalizer != 'off':
        graph += ';' + equalizer_overlay_graph(config, equalizer_intervals, start=start,
                                               base='vout' if has_logo else 'base')
        command += ['-filter_complex', graph, '-map', '[equalized]']
    elif has_logo:
        command += ['-filter_complex', graph, '-map', '[vout]']
    else:
        command += ['-vf', ','.join(filters), '-map', '0:v:0']
    command += ['-map', '[audio_out]' if equalizer != 'off' else '1:a:0', '-t', str(duration),
                *encoder_args(encoder), '-pix_fmt', 'yuv420p',
                '-c:a', 'aac', '-b:a', '192k', '-movflags', '+faststart', str(output)]
    return command


def render(folder, out_dir, *, language='en', model_name=MODEL, trust_legacy=False,
           refresh=False, allow_estimated=False, subtitles_only=False, start=0.0,
           duration=None, width=None, offset=0.0, font=DEFAULT_FONT, show_next=True, palette='auto', layout='phrases',
           instrumental_icon=True, equalizer=DEFAULT_EQUALIZER, loop_mode='seamless', timing_file=None, encoder='auto',
           cache_path=None):
    folder, out_dir = folder.resolve(), out_dir.resolve()
    mp4, mp3, md = input_files(folder)
    if out_dir == folder:
        raise ValueError('Output harus terpisah dari folder input')
    info = probe(mp4)
    video = next((s for s in info['streams'] if s['codec_type'] == 'video'), None)
    if not video:
        raise ValueError('MP4 tidak berisi video')
    song_duration = ffprobe_duration(mp3)
    if not math.isfinite(start) or start < 0 or start >= song_duration:
        raise ValueError('--start harus berada dalam durasi lagu')
    if duration is not None and (not math.isfinite(duration) or duration <= 0):
        raise ValueError('--duration harus lebih dari nol')
    if width is not None and (width < 16 or width % 2):
        raise ValueError('--width harus genap dan minimal 16')
    duration = min(duration, song_duration - start) if duration is not None else song_duration - start
    out_dir.mkdir(parents=True, exist_ok=True)
    output, ass = out_dir / f'{folder.name}.mp4', out_dir / f'{folder.name}.ass'
    report_path = out_dir / f'{folder.name}.alignment.json'
    if any(p.exists() for p in (output, ass, report_path)):
        raise ValueError(f'Hasil sudah ada di {out_dir}; gunakan --out berbeda agar hasil lama tetap aman')
    if palette != 'auto' and palette not in PALETTES:
        raise ValueError('Pilihan palet tidak valid')
    if layout not in ('phrases', 'lines'):
        raise ValueError('Layout subtitle tidak valid')
    if equalizer not in MODES:
        raise ValueError('Mode equalizer tidak valid')
    if loop_mode not in ('seamless', 'hard'):
        raise ValueError('Mode loop tidak valid')
    if encoder not in ENCODERS:
        raise ValueError('Pilihan encoder tidak valid')
    fonts_dir = package_font(out_dir, font)
    selected_palette = (analyze_background(mp4, float(info['format']['duration']))
                        if palette == 'auto' else palette_color(palette))
    log(f"Font: {font}; palet: {selected_palette['name']} ({palette})")
    words = whisper_words(mp3, Path(cache_path) if cache_path is not None else folder / '.karaoke_cache.json', language=language,
                          model_name=model_name, trust_legacy=trust_legacy, refresh=refresh)
    lines = build_line_timing(parse_lyrics(md), words)
    timing_payload = None
    if timing_file is not None:
        timing_payload = json.loads(Path(timing_file).read_text(encoding='utf-8'))
        lines = apply_timing_override(lines, timing_payload,
            audio_sha256=audio_fingerprint(mp3)['sha256'], lyrics_sha256=audio_fingerprint(md)['sha256'],
            start=start, duration=duration, song_duration=song_duration)
    for line in lines:
        if line.get('wend') is not None and line['wend'] > song_duration:
            line['issues'].append('timing_past_audio_end')
            line['needs_review'] = True
    suspect = [i + 1 for i, line in enumerate(lines) if line['needs_review']]
    report = {'audio': audio_fingerprint(mp3), 'lyrics_sha256': audio_fingerprint(md)['sha256'],
              'source': 'legacy_cache_unverified' if trust_legacy and not refresh else 'fingerprinted_cache_or_transcription',
              'method': 'global_text_alignment_to_asr_not_forced_alignment', 'song_duration': song_duration,
              'needs_review_lines': suspect, 'offset_seconds': offset,
              'render_window': [start, start + duration], 'lines': lines,
              'style': {'font': font, 'palette': selected_palette, 'palette_mode': palette,
                        'font_sha256': audio_fingerprint(DEFAULT_FONT_FILE)['sha256'] if font == DEFAULT_FONT else None,
                        'punctuation': 'hidden_display_only', 'layout': layout,
                        'promotion_ms': PROMOTION_MS if layout == 'lines' and show_next else 0,
                        'fade_out_ms': PHRASE_FADE_OUT_MS if layout == 'phrases' else FADE_OUT_MS,
                        'post_vocal_hold_ms': PHRASE_HOLD_MS if layout == 'phrases' else 0,
                        'exit_policy': 'hold_then_fade_capped_before_next_lead_in' if layout == 'phrases' else 'legacy',
                        'backing': 'none', 'main_y_ratio': 0.50,
                        'next_y_ratio': 0.65 if layout == 'lines' and show_next else None}}
    if timing_payload is not None:
        full_coverage = timing_payload['coverage'][0] == 0 and timing_payload['coverage'][1] >= song_duration
        report['method'] = ('ctc_forced_alignment_full_song' if full_coverage
                            else 'ctc_forced_alignment_in_preview_window_legacy_asr_outside')
        report['acoustic_timing'] = {'file': str(Path(timing_file).resolve()),
            'sha256': audio_fingerprint(Path(timing_file))['sha256'],
            'coverage': timing_payload['coverage'], 'model': timing_payload.get('model'),
            'review_required': True, 'global_delay_added': False}
    if layout == 'phrases':
        report['display_phrases'] = split_phrases(lines)
        report['style']['phrase_limits'] = {'max_words': 5, 'max_chars': 26}
        report['style']['phrase_splitter'] = 'punctuation_pause_boundary_heuristic'
        report['style']['font_size'] = phrase_font_size(int(video['width']), int(video['height']))
    report['style']['instrumental_icon'] = {
        'enabled': instrumental_icon, 'shape': 'user_jpg_overlay',
        'minimum_gap_seconds': INSTRUMENTAL_MIN_SECONDS,
        'padding_seconds': INSTRUMENTAL_PADDING_SECONDS, 'fade_ms': 300,
        'method': 'known_lyric_gaps_not_acoustic_detection'}
    lyric_tail_seconds = (PHRASE_HOLD_MS + PHRASE_FADE_OUT_MS) / 1000 if layout == 'phrases' else 0.0
    report['style']['instrumental_icon']['after_lyric_padding_seconds'] = max(
        INSTRUMENTAL_PADDING_SECONDS, lyric_tail_seconds + .1)
    report['instrumental_windows'] = (instrumental_windows(lines, song_duration=song_duration, offset=offset,
                                                          lyric_tail_seconds=lyric_tail_seconds)
                                      if instrumental_icon else [])
    icon_file = package_music_logo(out_dir) if report['instrumental_windows'] else None
    output_width, output_height = frame_size(int(video['width']), int(video['height']), width)
    eq_windows = instrumental_windows(lines, song_duration=song_duration, offset=offset) if equalizer != 'off' else []
    rate = video.get('avg_frame_rate', '24')
    background_duration = video.get('duration') or info.get('format', {}).get('duration')
    if loop_mode == 'seamless' and not background_duration and not subtitles_only:
        raise ValueError('Durasi video tidak tersedia untuk seamless loop')
    background_config = (loop_config(float(background_duration), rate)
                         if loop_mode == 'seamless' and background_duration else {})
    report['background_loop'] = dict(background_config, mode=loop_mode,
        source=audio_fingerprint(mp4), prepared=False, rendered=False,
        status='subtitles_only' if subtitles_only else 'pending_render')
    eq_config = equalizer_config(equalizer, output_width, output_height, rate)
    report['style']['equalizer'] = dict(eq_config, rendered=False,
        status='subtitles_only' if subtitles_only else 'pending_render',
        audio_sha256=report['audio']['sha256'], source='MP3 input 1, unchanged output audio',
        window_method='known_lyric_gaps_not_acoustic_detection', windows=eq_windows)
    icon_height = max(16, round(min(output_height * 0.27, output_width * 0.5)))
    icon_rgb = PALETTES[selected_palette['name']]
    report['style']['instrumental_icon'].update({
        'source_file': str(DEFAULT_LOGO_FILE),
        'source_sha256': audio_fingerprint(icon_file)['sha256'] if icon_file else None,
        'asset': str(icon_file.relative_to(out_dir)) if icon_file else None,
        'height_pixels': icon_height,
        'fill_rgb': list(icon_rgb), 'fill_source': 'lyric_highlight_palette',
        'outline_rgb': [0, 0, 0],
        'supersample': LOGO_SUPERSAMPLE, 'edge_downsample': 'area',
        'compositing': 'source_jpg_colorkey_supersampled_palette_tint_black_contour'})
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    if suspect:
        log(f'PERINGATAN: timing perlu ditinjau pada baris {suspect}; lihat {report_path.name}')
    if suspect and not allow_estimated and not subtitles_only:
        raise ValueError('Render dihentikan karena timing tidak andal. Tinjau laporan; --allow-estimated-timing hanya untuk preview sadar risiko.')
    build_ass(lines, ass, width=int(video['width']), height=int(video['height']),
              offset=offset, font=font, show_next=show_next, highlight=selected_palette['highlight'], layout=layout)
    if subtitles_only:
        log(f'ASS + laporan: {out_dir}')
        return ass
    encoding = select_encoder(encoder, output_width, output_height, rate)
    report['encoding'] = encoding
    log(f"Encoder: {encoding['selected']} ({encoder}); filter lirik/equalizer tetap CPU")
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    background = mp4
    if loop_mode == 'seamless':
        log(f"Siapkan background loop dengan crossfade {background_config['fade_seconds']:.2f}s")
        background = prepare_background_loop(mp4, out_dir, background_config, encoding=encoding)
        report['background_loop'].update(prepared=True, status='prepared',
            asset=str(background.relative_to(out_dir)), asset_sha256=audio_fingerprint(background)['sha256'])
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    # Controlled cwd avoids Windows ASS filter path quoting hazards.
    with tempfile.TemporaryDirectory(prefix='karaoke-render-') as temporary:
        staging = Path(temporary)
        (staging / 'lyrics.ass').write_bytes(ass.read_bytes())
        shutil.copytree(fonts_dir, staging / 'fonts')
        partial = out_dir / f'{folder.name}.partial.mp4'
        if partial.exists():
            raise ValueError(f'Render parsial sudah ada: {partial}; gunakan --out baru')
        def command_for(name):
            return render_command(background, mp3, partial, start=start, duration=duration, width=width,
                                 icon_file=icon_file, instrumental_intervals=report['instrumental_windows'],
                                 icon_height=icon_height, equalizer=equalizer, equalizer_intervals=eq_windows,
                                 height=output_height, rate=rate, video_width=output_width, icon_rgb=icon_rgb,
                                 encoder=name)
        log(f'Render {duration:.2f}s mulai {start:.2f}s -> {output.name}')
        log_path = out_dir / f'{folder.name}.ffmpeg.log'
        try:
            run_encode(command_for, partial, log_path, encoding, cwd=staging)
        finally:
            report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
        actual = ffprobe_duration(partial)
        if abs(actual - duration) > 0.3:
            raise RuntimeError(f'Durasi hasil {actual:.3f}s berbeda dari target {duration:.3f}s; hasil masih .partial')
        if output.exists():
            raise ValueError(f'Tujuan muncul selama render; tidak ditimpa: {output}')
        partial.rename(output)
    report['style']['equalizer'].update(rendered=equalizer != 'off', status='rendered' if equalizer != 'off' else 'disabled')
    report['background_loop'].update(rendered=True, status='rendered')
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    log(f'OK: {output}')
    return output


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('folder', nargs='?', help='folder berisi satu MP4 + MP3 + Markdown')
    mode.add_argument('--all', action='store_true', help='semua paket dalam inputs')
    mode.add_argument('--only', help='nama paket di inputs, dipisahkan koma')
    parser.add_argument('--out', type=Path, default=ROOT / 'outputs')
    parser.add_argument('--language', default='en', help='kode bahasa Whisper atau auto')
    parser.add_argument('--model', default=MODEL)
    parser.add_argument('--trust-legacy-cache', action='store_true')
    parser.add_argument('--refresh-cache', action='store_true')
    parser.add_argument('--allow-estimated-timing', action='store_true')
    parser.add_argument('--subtitles-only', action='store_true')
    parser.add_argument('--start', type=float, default=0.0, help='awal preview dalam detik')
    parser.add_argument('--duration', type=float, help='durasi preview dalam detik')
    parser.add_argument('--width', type=int, help='lebar preview, genap')
    parser.add_argument('--offset', type=float, default=0.0, help='geser teks; positif = lebih lambat')
    parser.add_argument('--timing-file', type=Path, help='timing acoustic beridentitas; render harus dalam cakupannya')
    parser.add_argument('--cache-file', type=Path, help='cache ASR terpisah (opsional; input asli tidak diubah)')
    parser.add_argument('--encoder', choices=ENCODERS, default='auto',
                        help='auto: uji GPU lalu fallback CPU; cpu: libx264; atau paksa encoder GPU')
    parser.add_argument('--font', default=DEFAULT_FONT)
    parser.add_argument('--palette', choices=['auto', *PALETTES], default='auto',
                        help='pilih aksen berdasarkan background atau palet tetap')
    parser.add_argument('--layout', choices=['phrases', 'lines'], default='phrases',
                        help='phrases: frasa besar dengan fade; lines: layout dua baris lama')
    parser.add_argument('--no-next-line', action='store_true', help='sembunyikan Next pada --layout lines')
    parser.add_argument('--no-instrumental-icon', action='store_true', help='sembunyikan simbol musik pada jeda panjang')
    parser.add_argument('--loop-mode', choices=['seamless','hard'], default='seamless',
                        help='seamless: crossfade video latar (default); hard: pengulangan langsung lama')
    parser.add_argument('--equalizer', choices=MODES, default=DEFAULT_EQUALIZER,
                        help='bar spektrum: subtle sepanjang lagu (default), instrumental hanya jeda, off untuk mematikan')
    args = parser.parse_args(argv)
    try:
        if not math.isfinite(args.offset):
            raise ValueError('Offset harus finite')
        if args.folder:
            folders = [Path(args.folder)]
        elif args.only:
            names = [name.strip() for name in args.only.split(',')]
            if any(not name or name in ('.', '..') or '/' in name or '\\' in name or ':' in name for name in names):
                raise ValueError('--only hanya menerima nama folder dalam inputs')
            folders = [ROOT / 'inputs' / name for name in names]
        else:
            folders = sorted((p for p in (ROOT / 'inputs').iterdir() if p.is_dir()), key=lambda p: p.name)
        if not folders:
            raise ValueError('Tidak ada paket input')
        for folder in folders:
            render(folder, args.out, language=args.language, model_name=args.model,
                   trust_legacy=args.trust_legacy_cache, refresh=args.refresh_cache,
                   allow_estimated=args.allow_estimated_timing, subtitles_only=args.subtitles_only,
                   start=args.start, duration=args.duration, width=args.width, offset=args.offset,
                   font=args.font, show_next=not args.no_next_line, palette=args.palette, layout=args.layout,
                   instrumental_icon=not args.no_instrumental_icon, equalizer=args.equalizer,
                   loop_mode=args.loop_mode, timing_file=args.timing_file, encoder=args.encoder, cache_path=args.cache_file)
    except (ValueError, OSError, RuntimeError, subprocess.CalledProcessError) as exc:
        parser.exit(1, f'ERROR: {exc}\n')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
