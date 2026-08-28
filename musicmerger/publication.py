"""Publish a completed full render visibly; retain previous versions in their runs."""
import hashlib
import json
import os
from pathlib import Path
import tempfile
from contextlib import contextmanager, nullcontext


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


def _write_bytes(path, content):
    _not_link(path)
    descriptor, name = tempfile.mkstemp(prefix=path.name + '.', suffix='.tmp', dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, 'wb') as stream:
            stream.write(content)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def _write_json(path, payload):
    _write_bytes(path, (json.dumps(payload, ensure_ascii=False, indent=2) + '\n').encode('utf-8'))


@contextmanager
def _extras(attachments, run):
    """Rollback sidecars if any later video publication step fails."""
    names = {'thumbnail.jpg', 'youtube-upload.txt'}
    if set(attachments) != names:
        raise ValueError('Paket thumbnail harus berisi JPEG dan metadata upload')
    visible = run.parent.parent / 'HASIL'
    _directory(visible)
    manifest = run.parent / 'latest-thumbnail.json'
    _not_link(manifest)
    old_manifest = manifest.read_bytes() if manifest.exists() else None
    old = json.loads(old_manifest) if old_manifest else None
    if old is not None and (not isinstance(old, dict) or old.get('schema') != 1
                            or not isinstance(old.get('files'), dict) or set(old['files']) != names):
        raise ValueError('Manifest thumbnail tidak valid')
    before = {}
    for name, staged in attachments.items():
        for path in (staged.parent, staged, visible/name):
            _not_link(path)
        if staged.parent != run/'support' or not staged.is_file() or not staged.stat().st_size:
            raise ValueError('Hanya thumbnail selesai dari support run ini yang boleh dipublikasikan')
        target = visible/name
        before[name] = target.read_bytes() if target.exists() else None
        if before[name] is not None:
            if old and _digest(target) != old['files'][name]:
                raise ValueError(f'Hasil thumbnail/metadata telah berubah: {target}')
            if not old and before[name] != staged.read_bytes():
                raise ValueError(f'File HASIL bukan hasil thumbnail yang dikelola aplikasi: {target}')
    backup = run/'support/previous-thumbnail'
    _not_link(backup)
    if backup.exists():
        raise ValueError('Backup thumbnail run ini sudah ada; gunakan run baru')
    if any(value is not None for value in before.values()) or old_manifest is not None:
        backup.mkdir()
        for name, content in before.items():
            if content is not None:
                (backup/name).write_bytes(content)
        if old_manifest is not None:
            (backup/'latest-thumbnail.json').write_bytes(old_manifest)
    changed = []
    manifest_changed = False
    try:
        for name, staged in attachments.items():
            _write_bytes(visible/name, staged.read_bytes())
            changed.append(name)
        _write_json(manifest, dict(schema=1, run=run.name, files={name:_digest(visible/name) for name in names}))
        manifest_changed = True
        yield visible/'thumbnail.jpg'
    except BaseException:
        for name in reversed(changed):
            if before[name] is None:
                (visible/name).unlink()
            else:
                _write_bytes(visible/name, before[name])
        if manifest_changed:
            if old_manifest is None:
                manifest.unlink()
            else:
                _write_bytes(manifest, old_manifest)
        raise


@contextmanager
def _publication_lock(run):
    if run.parent.name != 'MusicMerger-output':
        raise ValueError('Lokasi run publikasi tidak valid')
    for path in (run.parent.parent, run.parent, run, run/'support'):
        _not_link(path)
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
            yield
    finally:
        lock.unlink()


def publish_thumbnail(attachments, run):
    with _publication_lock(run), _extras(attachments, run) as result:
        return result


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


def publish(staged, run, mode, *, song_name=None, attachments=None):
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
        if attachments:
            raise ValueError('Preview tidak mempublikasikan thumbnail final')
        destination = run / 'preview'
        _directory(destination)
        target = destination / staged.name
        _move(staged, target)
        return target
    with _publication_lock(run), (_extras(attachments, run) if attachments else nullcontext()):
        return _publish_full(staged, run, song_name if song_name is not None else staged.stem)
