"""Read-only identity and media checks for resumable batch publication."""
import hashlib
import json
import math
from pathlib import Path
import subprocess


def digest(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def safe_path(path):
    path = Path(path).absolute()
    for part in (path, *path.parents):
        if part.is_symlink() or (part.exists() and getattr(part.lstat(), 'st_file_attributes', 0) & 0x400):
            raise ValueError(f'Links/junctions are not allowed: {part}')
    return path.resolve()


def read_json(path):
    return json.loads(Path(path).read_text(encoding='utf-8-sig'))


def source_snapshot(folder):
    folder = safe_path(folder)
    if not folder.is_dir():
        raise ValueError(f'Input folder missing: {folder}')
    sources = {}
    for extension in ('.mp4', '.mp3', '.md'):
        found = [p for p in folder.iterdir() if p.is_file() and p.suffix.lower() == extension]
        if len(found) != 1:
            raise ValueError(f'{folder}: expected exactly one {extension}, found {len(found)}')
        path = safe_path(found[0])
        sources[extension[1:]] = dict(path=str(path), size=path.stat().st_size, sha256=digest(path))
    metadata = safe_path(folder / 'youtube-metadata.json')
    data = read_json(metadata)
    md = sources['md']
    if (data.get('schema_version') != 1 or data.get('source_md') != Path(md['path']).name
            or data.get('source_md_sha256') != md['sha256']):
        raise ValueError(f'Source metadata does not match Markdown: {metadata}')
    for key, limit in (('thumbnail_title', 200), ('youtube_title', 100)):
        value = data.get(key)
        if (not isinstance(value, str) or not value.strip() or len(value) > limit
                or any(ord(c) < 32 for c in value)):
            raise ValueError(f'Invalid source metadata {key}: {metadata}')
    tags = data.get('tags')
    if not isinstance(tags, list) or any(not isinstance(t, str) or not t.strip() or
            any(ord(c) < 32 for c in t) for t in tags):
        raise ValueError(f'Invalid source metadata tags: {metadata}')
    sources['metadata'] = dict(path=str(metadata), size=metadata.stat().st_size, sha256=digest(metadata))
    return sources


def check_sources(row):
    try:
        current = source_snapshot(Path(row['path']))
    except (OSError, ValueError) as exc:
        raise ValueError(f'Source changed or unavailable in {row["path"]}: {exc}') from exc
    if current != row['sources']:
        raise ValueError(f'Source changed in {row["path"]}; create a separately reviewed job')


def has_package(row):
    folder = Path(row['path'])
    visible = safe_path(folder / 'HASIL')
    if any(safe_path(folder / 'MusicMerger-output' / name).exists()
           for name in ('latest-final.json', 'latest-thumbnail.json')):
        return True
    if not visible.exists():
        return False
    entries = list(visible.iterdir())
    # Metadata preparation can precede rendering. This one approved sidecar is
    # not an interrupted publication, and must remain untouched by the operator.
    if len(entries) == 1 and entries[0].name == 'youtube-upload.txt':
        txt = safe_path(entries[0])
        if txt.is_file() and txt.read_text(encoding='utf-8') == _upload_text(row):
            return False
    return bool(entries)


def _upload_text(row):
    data = read_json(row['sources']['metadata']['path'])
    return f"JUDUL YOUTUBE\n{data['youtube_title']}\n\nTAGS\n{', '.join(data['tags'])}\n"


def _leaf(value):
    if (not isinstance(value, str) or not value or value in ('.', '..') or
            any(c in value for c in '/\\:') or Path(value).name != value):
        raise ValueError('Invalid publication manifest leaf')
    return value


def _probe(path, *, count=False):
    command = ['ffprobe', '-v', 'error'] + (['-count_frames'] if count else [])
    result = subprocess.run(command + ['-show_format', '-show_streams', '-of', 'json', str(path)],
                            capture_output=True, text=True, timeout=600)
    if result.returncode:
        raise ValueError(f'ffprobe failed: {result.stderr[-2000:]}')
    return json.loads(result.stdout)


def verify_package(row, log_path):
    """Return evidence only after hashes, source provenance, full decode and JPEG pass."""
    from PIL import Image
    check_sources(row)
    folder = Path(row['path'])
    output = safe_path(folder / 'MusicMerger-output')
    final_manifest = safe_path(output / 'latest-final.json')
    thumbnail_manifest = safe_path(output / 'latest-thumbnail.json')
    if not final_manifest.is_file() or not thumbnail_manifest.is_file():
        raise ValueError('Unknown output: complete final and thumbnail manifests are required')
    final, thumbnail = read_json(final_manifest), read_json(thumbnail_manifest)
    if final.get('schema') != 1 or thumbnail.get('schema') != 1:
        raise ValueError('Invalid publication manifest schema')
    run = safe_path(output / _leaf(final.get('run')))
    thumbnail_run = safe_path(output / _leaf(thumbnail.get('run')))
    visible = safe_path(folder / 'HASIL')
    filename = _leaf(final.get('filename'))
    if filename != Path(row['sources']['mp3']['path']).stem + '-final.mp4':
        raise ValueError('Publication filename does not match source audio')
    expected = {filename, 'thumbnail.jpg', 'youtube-upload.txt'}
    if not visible.is_dir() or {p.name for p in visible.iterdir()} != expected:
        raise ValueError('Unknown or missing files in HASIL; preserve them for operator review')
    video = safe_path(visible / filename)
    jpg = safe_path(visible / 'thumbnail.jpg')
    txt = safe_path(visible / 'youtube-upload.txt')
    for path in (video, jpg, txt):
        if not path.is_file() or not path.stat().st_size:
            raise ValueError(f'Empty or missing publication: {path}')
    hashes = {p.name: digest(p) for p in (video, jpg, txt)}
    if hashes[filename] != final.get('sha256') or thumbnail.get('files') != {
            name: hashes[name] for name in ('thumbnail.jpg', 'youtube-upload.txt')}:
        raise ValueError('Publication manifest hash mismatch')
    status = read_json(safe_path(run / 'status.json'))
    if (status.get('mode') != 'full' or status.get('audio_sha256') != row['sources']['mp3']['sha256']
            or status.get('lyrics_sha256') != row['sources']['md']['sha256']):
        raise ValueError('Publication source status does not match job')
    # Do not require status=complete: publication can finish before its final checkpoint.
    report = read_json(safe_path(thumbnail_run / 'support/thumbnail-report.json'))
    report_inputs = {str(Path(p).resolve()): h for p, h in report.get('inputs', {}).items()}
    for source in row['sources'].values():
        if report_inputs.get(source['path']) != source['sha256']:
            raise ValueError('Thumbnail source report does not match job sources')
    if report.get('sha256') != hashes['thumbnail.jpg']:
        raise ValueError('Thumbnail report hash mismatch')
    if txt.read_text(encoding='utf-8') != _upload_text(row):
        raise ValueError('Upload TXT does not match approved source metadata')
    with Image.open(jpg) as image:
        image.load()
        if image.format != 'JPEG' or image.size != (1280, 720) or jpg.stat().st_size >= 2 * 1024 * 1024:
            raise ValueError('Invalid published JPEG format, dimensions or size')
    audio_info = _probe(row['sources']['mp3']['path'])
    media = _probe(video, count=True)
    song_duration = float(audio_info['format']['duration'])
    duration = float(media['format']['duration'])
    if not all(math.isfinite(d) and d > 0 for d in (song_duration, duration)) or abs(duration - song_duration) > .25:
        raise ValueError(f'Final duration {duration} does not match source MP3 {song_duration}')
    videos = [s for s in media['streams'] if s.get('codec_type') == 'video']
    audios = [s for s in media['streams'] if s.get('codec_type') == 'audio']
    if len(videos) != 1 or len(audios) != 1 or videos[0].get('codec_name') != 'h264' or audios[0].get('codec_name') != 'aac':
        raise ValueError('Expected one H.264 video and one AAC audio stream')
    stream = videos[0]
    numerator, denominator = map(int, stream['avg_frame_rate'].split('/'))
    fps = numerator / denominator
    frames = int(stream.get('nb_read_frames', 0))
    if stream.get('width', 0) <= 0 or stream.get('height', 0) <= 0 or not math.isfinite(fps) or fps <= 0 or frames < max(1, duration * fps - 3):
        raise ValueError('Invalid or incomplete decoded video frames')
    for stream in (videos[0], audios[0]):
        if abs(float(stream.get('duration', 0)) - song_duration) > .25:
            raise ValueError('Published stream duration does not match source MP3')
    with Path(log_path).open('ab', buffering=0) as log:
        result = subprocess.run(['ffmpeg', '-nostdin', '-v', 'error', '-xerror', '-i', str(video),
                                 '-map', '0:v:0', '-map', '0:a:0', '-f', 'null', '-'],
                                stdout=log, stderr=log, timeout=3600)
    if result.returncode:
        raise ValueError(f'Full decode failed; see {log_path}')
    check_sources(row)
    for path in (video, jpg, txt):
        if digest(path) != hashes[path.name]:
            raise ValueError('Publication changed during verification')
    return dict(run=run.name, thumbnail_run=thumbnail_run.name, video=str(video), thumbnail=str(jpg),
                upload_txt=str(txt), hashes=hashes, duration=duration, source_duration=song_duration,
                video_codec='h264', audio_codec='aac', frames=frames, full_decode=True,
                manifests={str(p): digest(p) for p in (final_manifest, thumbnail_manifest)})
