import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

from musicmerger import batch


class BatchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.folder = self.root / '12'
        self.folder.mkdir()
        for name in ('song.mp3', 'scene.mp4', 'song.md'):
            (self.folder / name).write_bytes(name.encode())
        self.metadata = dict(schema_version=1, thumbnail_title='Song', youtube_title='Song',
                             tags=['song'], source_md='song.md',
                             source_md_sha256=batch.digest(self.folder / 'song.md'))
        (self.folder / 'youtube-metadata.json').write_text(json.dumps(self.metadata))
        self.job = self.root / 'job'

    def create(self):
        return batch.create_job(self.root, ['12'], self.job)

    def test_new_job_is_durable_and_never_reuses_directory(self):
        state = self.create()
        self.assertEqual(state['folders'][0]['status'], 'pending')
        self.assertEqual(batch.read_state(self.job), state)
        with self.assertRaises(FileExistsError):
            self.create()

    def test_rejects_path_escape_and_duplicate_folders_without_creating_job(self):
        for folders in (['../12'], ['12', '12'], []):
            with self.assertRaises(ValueError):
                batch.create_job(self.root, folders, self.job)
        self.assertFalse(self.job.exists())

    def test_changed_sources_block_before_launch(self):
        self.create()
        (self.folder / 'song.md').write_text('changed')
        with self.assertRaisesRegex(ValueError, 'Source changed'):
            batch.execute(self.job)
        self.assertEqual(batch.read_state(self.job)['folders'][0]['attempts'], [])

    def test_atomic_replace_failure_preserves_state(self):
        original = self.create()
        with patch.object(os, 'replace', side_effect=OSError('disk error')):
            with self.assertRaises(OSError):
                batch.atomic_json(self.job / 'job.json', {'broken': True})
        self.assertEqual(batch.read_state(self.job), original)

    def test_live_owner_lock_rejects_second_owner(self):
        self.create()
        with batch.Lease(self.job / 'owner.lock'):
            with self.assertRaises(batch.BusyError):
                batch.execute(self.job)

    def test_live_child_lease_blocks_even_with_dead_owner(self):
        state = self.create()
        state['owner'] = {'pid': 999999999, 'created': 'dead'}
        batch.atomic_json(self.job / 'job.json', state)
        with batch.Lease(self.job / 'child.lock'):
            with self.assertRaises(batch.BusyError):
                batch.execute(self.job)

    def test_creation_identity_detects_reused_pid(self):
        identity = batch.process_identity(os.getpid())
        self.assertTrue(batch.process_alive(identity))
        self.assertFalse(batch.process_alive(dict(identity, created='different')))

    def test_completed_output_is_adopted_after_crash_without_launch(self):
        state = self.create()
        state['folders'][0].update(status='running', token='old', attempts=[{'token': 'old'}])
        batch.atomic_json(self.job / 'job.json', state)
        with patch.object(batch, 'verify_package', return_value={'run': 'published', 'hashes': {'video': 'abc'}}), \
                patch.object(batch, 'has_package', return_value=True), \
                patch.object(batch, 'launch_guardian', side_effect=AssertionError('must not launch')):
            result = batch.execute(self.job)
        self.assertEqual(result['folders'][0]['status'], 'verified')
        self.assertEqual(result['folders'][0]['verification']['run'], 'published')

    def test_dead_owner_completed_failure_requires_explicit_retry(self):
        state = self.create()
        record = self.job / 'logs/old-process.json'
        state['folders'][0].update(status='running', token='old',
            attempts=[dict(token='old', process_record=str(record), log=str(self.job / 'logs/old.log'))])
        batch.atomic_json(record, dict(finished=batch.now(), returncode=7))
        batch.atomic_json(self.job / 'job.json', state)
        with patch.object(batch, 'launch_guardian', side_effect=AssertionError('must not retry')):
            result = batch.execute(self.job)
        self.assertEqual(result['folders'][0]['status'], 'failed')
        self.assertEqual(result['folders'][0]['attempts'][0]['returncode'], 7)

    def test_recorded_live_child_blocks_even_if_finished_flag_is_wrong(self):
        state = self.create()
        record = self.job / 'logs/old-process.json'
        state['folders'][0].update(status='running', token='old',
            attempts=[dict(token='old', process_record=str(record), log=str(self.job / 'logs/old.log'))])
        batch.atomic_json(record, dict(finished=batch.now(), returncode=1, child=batch.process_identity(os.getpid())))
        batch.atomic_json(self.job / 'job.json', state)
        with self.assertRaises(batch.BusyError):
            batch.execute(self.job)

    def test_failed_child_is_not_retried_implicitly(self):
        self.create()
        command = lambda folder: [sys.executable, '-c', 'raise SystemExit(7)']
        result = batch.execute(self.job, command_factory=command)
        row = result['folders'][0]
        self.assertEqual(row['status'], 'failed')
        self.assertEqual(row['attempts'][0]['returncode'], 7)
        result = batch.execute(self.job, command_factory=command)
        self.assertEqual(len(result['folders'][0]['attempts']), 1)
        result = batch.execute(self.job, retry_failed=True, command_factory=command)
        self.assertEqual(len(result['folders'][0]['attempts']), 2)

    def test_successful_child_without_package_is_not_verified(self):
        self.create()
        result = batch.execute(self.job, command_factory=lambda folder: [sys.executable, '-c', 'print("done")'])
        row = result['folders'][0]
        self.assertEqual(row['status'], 'failed')
        self.assertIn('verification', row['error'].lower())
        self.assertIn('done', Path(row['attempts'][0]['log']).read_text())

    def test_verified_output_hash_change_blocks_resume(self):
        state = self.create()
        state['folders'][0].update(status='verified', verification={'run': 'one', 'hashes': {'video': 'old'}})
        batch.atomic_json(self.job / 'job.json', state)
        with patch.object(batch, 'has_package', return_value=True), \
                patch.object(batch, 'verify_package', return_value={'run': 'one', 'hashes': {'video': 'new'}}):
            with self.assertRaisesRegex(ValueError, 'Verified output changed'):
                batch.execute(self.job)

    def test_verified_manifest_change_blocks_resume(self):
        state = self.create()
        evidence = dict(run='one', hashes={'video': 'same'}, manifests={'final': 'old'})
        state['folders'][0].update(status='verified', verification=evidence)
        batch.atomic_json(self.job / 'job.json', state)
        with patch.object(batch, 'has_package', return_value=True), \
                patch.object(batch, 'verify_package', return_value=dict(evidence, manifests={'final': 'new'})):
            with self.assertRaisesRegex(ValueError, 'Verified output changed'):
                batch.execute(self.job)

    def test_stale_guardian_token_cannot_launch(self):
        self.create()
        with self.assertRaisesRegex(ValueError, 'stale launch'):
            batch.guardian(self.job, 'cancelled-token')
        self.assertFalse(list((self.job / 'logs').iterdir()))

    def test_foreground_interrupt_marks_interrupted_and_stops_only_owned_guardian(self):
        self.create()
        process = Mock(pid=os.getpid())
        process.poll.return_value = None
        process.wait.side_effect = [KeyboardInterrupt(), 0]
        with patch.object(batch, 'launch_guardian', return_value=process):
            with self.assertRaises(KeyboardInterrupt):
                batch.execute(self.job)
        row = batch.read_state(self.job)['folders'][0]
        self.assertEqual(row['status'], 'interrupted')
        process.terminate.assert_called_once()

    def test_status_reports_live_child_when_owner_has_died(self):
        state = self.create()
        record_path = self.job / 'logs/record.json'
        row = state['folders'][0]
        row.update(status='running', attempts=[dict(log='child.log', process_record=str(record_path))])
        batch.atomic_json(record_path, dict(child=batch.process_identity(os.getpid())))
        batch.atomic_json(self.job / 'job.json', state)
        report = batch.status(self.job)
        self.assertFalse(report['owner_alive'])
        self.assertTrue(report['folders'][0]['child_alive'])

    def test_unknown_existing_output_is_never_rendered_over(self):
        self.create()
        (self.folder / 'HASIL').mkdir()
        output = self.folder / 'HASIL/unmanaged.mp4'
        output.write_bytes(b'keep')
        with self.assertRaisesRegex(ValueError, 'manifest|Unknown|unknown'):
            batch.execute(self.job)
        self.assertEqual(output.read_bytes(), b'keep')

    def test_approved_precreated_upload_txt_allows_fresh_child_without_touching_txt(self):
        self.create()
        visible = self.folder / 'HASIL'
        visible.mkdir()
        txt = visible / 'youtube-upload.txt'
        txt.write_bytes(b'JUDUL YOUTUBE\nSong\n\nTAGS\nsong\n')
        original = txt.read_bytes()
        row = batch.read_state(self.job)['folders'][0]
        self.assertFalse(batch.has_package(row))
        result = batch.execute(self.job, command_factory=lambda folder: [sys.executable, '-c', 'raise SystemExit(7)'])
        self.assertEqual(result['folders'][0]['attempts'][0]['returncode'], 7)
        self.assertEqual(txt.read_bytes(), original)

    def test_mismatched_precreated_upload_txt_still_blocks(self):
        self.create()
        visible = self.folder / 'HASIL'
        visible.mkdir()
        txt = visible / 'youtube-upload.txt'
        txt.write_text('unreviewed title', encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'Unknown output'):
            batch.execute(self.job)
        self.assertEqual(txt.read_text(), 'unreviewed title')

    def test_approved_txt_with_manifest_or_extra_file_is_not_fresh(self):
        state = self.create()
        visible = self.folder / 'HASIL'
        visible.mkdir()
        (visible / 'youtube-upload.txt').write_bytes(b'JUDUL YOUTUBE\nSong\n\nTAGS\nsong\n')
        extra = visible / 'unknown.jpg'
        extra.write_bytes(b'keep')
        self.assertTrue(batch.has_package(state['folders'][0]))
        extra.unlink()  # Test fixture only.
        output = self.folder / 'MusicMerger-output'
        output.mkdir()
        for name in ('latest-final.json', 'latest-thumbnail.json'):
            with self.subTest(manifest=name):
                manifest = output / name
                manifest.write_text('{}')
                self.assertTrue(batch.has_package(state['folders'][0]))
                manifest.unlink()  # Test fixture only.

    def wait_for(self, predicate, timeout=10):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(.05)
        self.fail('Timed out waiting for harmless child checkpoint')

    def test_dead_owner_does_not_stop_live_child_and_resume_cannot_duplicate(self):
        self.create()
        marker = self.root / 'child-finished.txt'
        release = self.root / 'release-child.txt'
        command = [sys.executable, '-c',
                   'import time\nfrom pathlib import Path\n'
                   f'release = Path({str(release)!r})\ndeadline = time.monotonic() + 15\n'
                   'while not release.exists() and time.monotonic() < deadline: time.sleep(.05)\n'
                   f'Path({str(marker)!r}).write_text("finished")']
        owner_code = ('from musicmerger.batch import execute; from pathlib import Path; '
                      f'execute(Path({str(self.job)!r}), command_factory=lambda folder: {command!r})')
        owner = subprocess.Popen([sys.executable, '-B', '-c', owner_code], cwd=batch.ROOT,
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.addCleanup(lambda: owner.poll() is None and owner.kill())
        self.wait_for(lambda: bool(list((self.job / 'logs').glob('*-process.json'))))
        record_path = next((self.job / 'logs').glob('*-process.json'))
        self.wait_for(lambda: 'child' in json.loads(record_path.read_text()) or
                      'error' in json.loads(record_path.read_text()))
        self.assertIn('child', json.loads(record_path.read_text()), record_path.read_text())
        owner.kill()
        owner.wait(timeout=5)
        with self.assertRaises(batch.BusyError):
            batch.execute(self.job)
        release.touch()
        self.wait_for(marker.exists)
        self.wait_for(lambda: not batch.process_alive(json.loads(record_path.read_text())['guardian']))
        self.assertEqual(marker.read_text(), 'finished')

    @unittest.skipUnless(os.name == 'nt', 'Windows JobObject ownership integration')
    def test_guardian_death_kills_owned_child_tree(self):
        self.create()
        marker = self.root / 'must-not-exist.txt'
        grandchild_pid_file = self.root / 'grandchild-pid.txt'
        grandchild = [sys.executable, '-c',
                      'import os, time\nfrom pathlib import Path\n'
                      f'Path({str(grandchild_pid_file)!r}).write_text(str(os.getpid()))\n'
                      f'time.sleep(30)\nPath({str(marker)!r}).write_text("bad")']
        command = [sys.executable, '-c', f'import subprocess; subprocess.run({grandchild!r})']
        unrelated = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'],
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.addCleanup(lambda: (unrelated.terminate(), unrelated.wait(timeout=5)))
        owner_code = ('from musicmerger.batch import execute; from pathlib import Path; '
                      f'execute(Path({str(self.job)!r}), command_factory=lambda folder: {command!r})')
        owner = subprocess.Popen([sys.executable, '-B', '-c', owner_code], cwd=batch.ROOT,
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.addCleanup(lambda: owner.poll() is None and owner.kill())
        self.wait_for(lambda: bool(list((self.job / 'logs').glob('*-process.json'))))
        record_path = next((self.job / 'logs').glob('*-process.json'))
        self.wait_for(lambda: 'child' in json.loads(record_path.read_text()) or
                      'error' in json.loads(record_path.read_text()))
        self.assertIn('child', json.loads(record_path.read_text()), record_path.read_text())
        self.wait_for(grandchild_pid_file.exists)
        grandchild_identity = batch.process_identity(int(grandchild_pid_file.read_text()))
        record = json.loads(record_path.read_text())
        subprocess.run(['taskkill', '/PID', str(record['guardian']['pid']), '/F'],
                       check=True, capture_output=True)
        owner.wait(timeout=10)
        self.wait_for(lambda: not batch.process_alive(record['child']))
        self.wait_for(lambda: not batch.process_alive(grandchild_identity))
        self.assertIsNone(unrelated.poll())
        self.assertFalse(marker.exists())

    def make_package(self):
        from PIL import Image
        commands = [
            ['ffmpeg', '-v', 'error', '-y', '-f', 'lavfi', '-i', 'sine=frequency=440:duration=1',
             '-c:a', 'libmp3lame', str(self.folder / 'song.mp3')],
            ['ffmpeg', '-v', 'error', '-y', '-f', 'lavfi', '-i', 'color=c=black:s=160x90:r=24:d=1',
             '-f', 'lavfi', '-i', 'sine=frequency=440:duration=1', '-c:v', 'libx264', '-pix_fmt', 'yuv420p',
             '-c:a', 'aac', '-shortest', str(self.folder / 'scene.mp4')]]
        for command in commands:
            subprocess.run(command, check=True, capture_output=True)
        self.create()
        row = batch.read_state(self.job)['folders'][0]
        output = self.folder / 'MusicMerger-output'
        run = output / 'test-full-001'
        (run / 'support').mkdir(parents=True)
        visible = self.folder / 'HASIL'
        visible.mkdir()
        shutil.copyfile(self.folder / 'scene.mp4', visible / 'song-final.mp4')
        Image.new('RGB', (1280, 720)).save(visible / 'thumbnail.jpg', format='JPEG')
        (visible / 'youtube-upload.txt').write_text('JUDUL YOUTUBE\nSong\n\nTAGS\nsong\n', encoding='utf-8')
        batch.atomic_json(output / 'latest-final.json', dict(schema=1, run=run.name,
                          filename='song-final.mp4', sha256=batch.digest(visible / 'song-final.mp4')))
        batch.atomic_json(output / 'latest-thumbnail.json', dict(schema=1, run=run.name,
                          files={name: batch.digest(visible / name) for name in ('thumbnail.jpg', 'youtube-upload.txt')}))
        batch.atomic_json(run / 'status.json', dict(mode='full', status='running', stage='render',
                          audio_sha256=row['sources']['mp3']['sha256'], lyrics_sha256=row['sources']['md']['sha256']))
        batch.atomic_json(run / 'support/thumbnail-report.json', dict(schema=1,
                          sha256=batch.digest(visible / 'thumbnail.jpg'),
                          inputs={s['path']: s['sha256'] for s in row['sources'].values()}))
        return row

    def test_real_media_verified_and_completed_resume_keeps_run_and_hashes(self):
        row = self.make_package()
        first = batch.execute(self.job)
        evidence = first['folders'][0]['verification']
        self.assertTrue(evidence['full_decode'])
        self.assertEqual(evidence['frames'], 24)
        second = batch.execute(self.job)
        self.assertEqual(second['folders'][0]['verification'], evidence)
        self.assertEqual(second['folders'][0]['attempts'], [])

    def test_real_media_bad_source_provenance_rejected(self):
        row = self.make_package()
        report_path = self.folder / 'MusicMerger-output/test-full-001/support/thumbnail-report.json'
        report = json.loads(report_path.read_text())
        report['inputs'][row['sources']['mp4']['path']] = 'wrong'
        batch.atomic_json(report_path, report)
        with self.assertRaisesRegex(ValueError, 'source report'):
            batch.execute(self.job)

    def test_real_detached_worker_completes_after_launcher_exits(self):
        self.make_package()
        launcher_code = ('from musicmerger.batch import detach; from pathlib import Path; '
                         f'detach(Path({str(self.job)!r}))')
        launcher = subprocess.run([sys.executable, '-B', '-c', launcher_code], cwd=batch.ROOT,
                                  capture_output=True, text=True, timeout=10)
        self.assertEqual(launcher.returncode, 0, launcher.stderr)
        launch_record = json.loads((self.job / 'launcher.json').read_text())
        self.assertTrue(batch.process_alive(launch_record['worker']))
        self.wait_for(lambda: batch.read_state(self.job)['folders'][0]['status'] == 'verified', timeout=20)
        self.wait_for(lambda: not batch.process_alive(launch_record['worker']))
        self.assertEqual(batch.read_state(self.job)['folders'][0]['attempts'], [])


if __name__ == '__main__':
    unittest.main()
