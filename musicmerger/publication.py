"""Publish a completed full render visibly; retain previous versions in their runs."""
import hashlib
import json
import os
from pathlib import Path
import tempfile


def _not_link(path):
    if path.is_symlink() or (path.exists() and getattr(path.lstat(), 'st_file_attributes', 0) & 0x400):
        raise ValueError(f'Lokasi hasil tidak boleh berupa link/junction: {path}')


def _directory(path):
    _not_link(path)
    path.mkdir(exist_ok=True)


def _leaf(value):
    if (not isinstance(value, str) or value in ('', '.', '..')
            or any(c in value for c in '/\\:') or Path(value).name != value):
        raise ValueError('Nama hasil/riwayat tidak valid')
    return value


def _digest(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def _write_json(path, payload):
    _not_link(path)
    descriptor, name = tempfile.mkstemp(prefix=path.name + '.', suffix='.tmp', dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, 'w', encoding='utf-8') as stream:
            json.dump(payload, stream, ensure_ascii=False, indent=2)
            stream.write('\n')
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def _move(source, destination):
    _not_link(source)
    _not_link(destination)
    if destination.exists():
        raise ValueError(f'Hasil sudah ada, tidak ditimpa: {destination}')
    source.rename(destination)


def _publish_full(staged, run, song_name):
    metadata = run.parent
    visible = metadata.parent / 'HASIL'
    _directory(visible)
    manifest = metadata / 'latest-final.json'
    _not_link(manifest)
    target = visible / (_leaf(song_name) + '-final.mp4')
    _not_link(target)
    previous = archive = status_path = status_before = status_after = None
    if manifest.exists():
        state = json.loads(manifest.read_text(encoding='utf-8'))
        if not isinstance(state, dict) or state.get('schema') != 1:
            raise ValueError('Manifest hasil terakhir tidak valid')
        previous_run = metadata / _leaf(state.get('run'))
        if previous_run == run:
            raise ValueError('Run ini sudah dipublikasikan')
        _not_link(previous_run)
        if not previous_run.is_dir():
            raise ValueError('Folder riwayat hasil terakhir hilang; periksa sebelum melanjutkan')
        previous = visible / _leaf(state.get('filename'))
        _not_link(previous)
        if previous.exists():
            if not previous.is_file() or _digest(previous) != state.get('sha256'):
                raise ValueError('Hasil terakhir telah berubah; tidak dipindah atau ditimpa')
            _directory(previous_run / 'final')
            archive = previous_run / 'final' / previous.name
            _not_link(archive)
            if archive.exists():
                raise ValueError(f'Arsip sudah ada: {archive}')
            status_path = previous_run / 'status.json'
            _not_link(status_path)
            if status_path.exists():
                status_before = status_path.read_bytes()
                status_after = json.loads(status_before)
                if not isinstance(status_after, dict):
                    raise ValueError('Status riwayat tidak valid')
                status_after.update(output=str(archive), archived=True)
        else:
            previous = None  # A user may have removed their previous final.
    if target.exists() and target != previous:
        raise ValueError(f'File HASIL bukan hasil yang dikelola aplikasi: {target}')
    new_state = dict(schema=1, run=run.name, filename=target.name, sha256=_digest(staged))
    archived = published = status_changed = False
    try:
        if previous is not None:
            _move(previous, archive)
            archived = True
        _move(staged, target)
        published = True
        if status_before is not None:
            _write_json(status_path, status_after)
            status_changed = True
        _write_json(manifest, new_state)
    except BaseException:
        # Ordinary I/O errors and Ctrl+C restore the prior visible result.
        if published:
            _move(target, staged)
        if archived:
            _move(archive, previous)
        if status_changed:
            _write_json(status_path, json.loads(status_before))
        raise
    return target


def publish(staged, run, mode, *, song_name=None):
    """Move a finished MP4; full mode also accepts a legacy run/final MP4."""
    if mode not in ('preview', 'full'):
        raise ValueError('Mode publikasi tidak valid')
    if run.parent.name != 'MusicMerger-output':
        raise ValueError('Lokasi run publikasi tidak valid')
    for path in (run.parent.parent, run.parent, run, staged.parent, staged):
        _not_link(path)
    allowed = [run / 'support'] + ([run / 'final'] if mode == 'full' else [])
    if (staged.parent not in allowed or staged.suffix != '.mp4'
            or '.partial' in staged.name or not staged.is_file() or not staged.stat().st_size):
        raise ValueError('Hanya MP4 selesai dari run ini yang boleh dipublikasikan')
    if mode == 'preview':
        destination = run / 'preview'
        _directory(destination)
        target = destination / staged.name
        _move(staged, target)
        return target
    lock = run.parent / '.publish.lock'
    _not_link(lock)
    try:
        stream = lock.open('x', encoding='utf-8')
    except FileExistsError as exc:
        raise RuntimeError(f'Publikasi sedang berjalan atau terputus. Periksa {lock}') from exc
    try:
        with stream:
            stream.write(run.name)
            stream.flush()
            return _publish_full(staged, run, song_name if song_name is not None else staged.stem)
    finally:
        lock.unlink()
