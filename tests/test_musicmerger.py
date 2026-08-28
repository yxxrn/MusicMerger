"""User-facing CLI and isolated output workflow regression tests."""
import contextlib
import copy
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from musicmerger import cli as musicmerger
from musicmerger import workflow as workflow


class CliTests(unittest.TestCase):
    def test_quoted_windows_folder_is_accepted(self):
        self.assertEqual(musicmerger.folder_path(' "D:\\Music folder" '), Path('D:\\Music folder'))

    def test_interactive_defaults_are_preview_and_twenty_seconds(self):
        with mock.patch('builtins.input', side_effect=['"D:\\song"', '', '', '']):
            options = musicmerger.options([])
        self.assertEqual(options.folder, Path('D:\\song'))
        self.assertEqual((options.mode, options.start, options.duration), ('preview', 0, 20))

    def test_full_mode_needs_no_interactive_answers(self):
        with mock.patch('builtins.input', side_effect=AssertionError('Unexpected prompt')):
            options = musicmerger.options(['D:\\song', '--mode', 'full'])
        self.assertEqual(options.mode, 'full')

    def test_invalid_menu_choice_is_retried(self):
        with mock.patch('builtins.input', side_effect=['bad', '2']):
            self.assertEqual(musicmerger.options(['D:\\song']).mode, 'full')

    def test_preview_rejects_non_finite_and_negative_numbers(self):
        for extra in [['--start', 'nan'], ['--start', '-1'], ['--duration', '0']]:
            with self.subTest(extra=extra), contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                musicmerger.options(['D:\\song', '--mode', 'preview', *extra])

    def test_cancel_returns_nonzero_without_starting_work(self):
        with mock.patch('builtins.input', side_effect=KeyboardInterrupt), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(musicmerger.main([]), 130)


class WorkflowTests(unittest.TestCase):
    def test_run_names_never_overwrite_and_stay_in_selected_folder(self):
        with tempfile.TemporaryDirectory() as folder:
            first = workflow.reserve_run(Path(folder), 'preview', stamp='test')
            second = workflow.reserve_run(Path(folder), 'preview', stamp='test')
            self.assertNotEqual(first, second)
            self.assertEqual(first.parent, Path(folder) / 'MusicMerger-output')

    def test_publication_keeps_subtitles_away_from_mp4(self):
        with tempfile.TemporaryDirectory() as folder:
            run = workflow.reserve_run(Path(folder), 'full', stamp='test')
            staged = run / 'support' / 'song.mp4'
            staged.write_bytes(b'complete video')
            (run / 'support' / 'song.ass').write_text('subtitle')
            final = workflow.publish(staged, run, 'full')
            self.assertEqual(final, Path(folder) / 'HASIL' / 'song-final.mp4')
            self.assertEqual(list(final.parent.iterdir()), [final])
            self.assertTrue((run / 'support' / 'song.ass').exists())

    def test_partial_video_is_never_published(self):
        with tempfile.TemporaryDirectory() as folder:
            run = workflow.reserve_run(Path(folder), 'full', stamp='test')
            partial = run / 'support' / 'song.partial.mp4'
            partial.write_bytes(b'incomplete')
            with self.assertRaises(ValueError):
                workflow.publish(partial, run, 'full')
            self.assertTrue(partial.exists())

    def test_new_final_archives_previous_and_updates_its_status(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            first = workflow.reserve_run(folder, 'full', stamp='first')
            staged = first / 'support/song.mp4'; staged.write_bytes(b'old video')
            latest = workflow.publish(staged, first, 'full', song_name='my song')
            (first / 'status.json').write_text(json.dumps({'status': 'complete', 'output': str(latest)}))
            second = workflow.reserve_run(folder, 'full', stamp='second')
            staged = second / 'support/song.mp4'; staged.write_bytes(b'new video')
            result = workflow.publish(staged, second, 'full', song_name='my song')
            self.assertEqual(result, latest)
            self.assertEqual(result.read_bytes(), b'new video')
            previous = json.loads((first / 'status.json').read_text())
            self.assertEqual(Path(previous['output']).read_bytes(), b'old video')
            self.assertEqual(Path(previous['output']).parent, first / 'final')
            self.assertEqual(list((folder / 'HASIL').iterdir()), [latest])

    def test_preview_does_not_replace_latest_final(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            full = workflow.reserve_run(folder, 'full')
            staged = full / 'support/song.mp4'; staged.write_bytes(b'full')
            latest = workflow.publish(staged, full, 'full')
            preview = workflow.reserve_run(folder, 'preview')
            staged = preview / 'support/song.mp4'; staged.write_bytes(b'preview')
            result = workflow.publish(staged, preview, 'preview')
            self.assertEqual(result.parent, preview / 'preview')
            self.assertEqual(latest.read_bytes(), b'full')

    def test_unknown_result_collision_is_not_overwritten(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory); (folder / 'HASIL').mkdir()
            target = folder / 'HASIL/song-final.mp4'; target.write_bytes(b'user file')
            run = workflow.reserve_run(folder, 'full')
            staged = run / 'support/song.mp4'; staged.write_bytes(b'new')
            with self.assertRaises(ValueError):
                workflow.publish(staged, run, 'full')
            self.assertEqual(target.read_bytes(), b'user file')
            self.assertTrue(staged.exists())

    def test_legacy_final_is_moved_without_copying_or_reencoding(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            run = workflow.reserve_run(folder, 'full')
            legacy = run / 'final/song.mp4'; legacy.write_bytes(b'original video')
            result = workflow.publish(legacy, run, 'full', song_name='Maple Season')
            self.assertEqual(result, folder / 'HASIL/Maple Season-final.mp4')
            self.assertEqual(result.read_bytes(), b'original video')
            self.assertFalse(legacy.exists())

    def test_empty_final_keeps_previous_result(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            first = workflow.reserve_run(folder, 'full')
            staged = first / 'support/song.mp4'; staged.write_bytes(b'old')
            latest = workflow.publish(staged, first, 'full')
            second = workflow.reserve_run(folder, 'full')
            empty = second / 'support/song.mp4'; empty.touch()
            with self.assertRaises(ValueError):
                workflow.publish(empty, second, 'full')
            self.assertEqual(latest.read_bytes(), b'old')

    def test_result_folder_does_not_become_a_second_video_input(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            for name in ('source.mp4', 'song.mp3', 'lyrics.md'):
                (folder / name).write_bytes(b'original')
            run = workflow.reserve_run(folder, 'full')
            staged = run / 'support/song.mp4'; staged.write_bytes(b'output')
            workflow.publish(staged, run, 'full')
            self.assertEqual(workflow.karaoke.input_files(folder)[0], folder / 'source.mp4')

    def test_publication_failure_restores_previous_video_and_status(self):
        from musicmerger import publication
        for failure in ('move', 'manifest'):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as directory:
                folder = Path(directory)
                first = workflow.reserve_run(folder, 'full')
                staged = first / 'support/song.mp4'; staged.write_bytes(b'old')
                latest = workflow.publish(staged, first, 'full')
                old_status = {'status': 'complete', 'output': str(latest)}
                (first / 'status.json').write_text(json.dumps(old_status))
                manifest = first.parent / 'latest-final.json'
                old_manifest = manifest.read_bytes()
                second = workflow.reserve_run(folder, 'full')
                staged = second / 'support/song.mp4'; staged.write_bytes(b'new')
                original_move, original_write = publication._move, publication._write_json
                def move(source, destination):
                    if failure == 'move' and source == staged:
                        raise OSError('simulated move failure')
                    original_move(source, destination)
                def write(path, value):
                    if failure == 'manifest' and path == manifest:
                        raise OSError('simulated metadata failure')
                    original_write(path, value)
                with mock.patch.object(publication, '_move', side_effect=move), \
                        mock.patch.object(publication, '_write_json', side_effect=write), \
                        self.assertRaises(OSError):
                    workflow.publish(staged, second, 'full')
                self.assertEqual(latest.read_bytes(), b'old')
                self.assertEqual(staged.read_bytes(), b'new')
                self.assertEqual(manifest.read_bytes(), old_manifest)
                self.assertEqual(json.loads((first / 'status.json').read_text()), old_status)
                self.assertEqual(list((first / 'final').iterdir()), [])
                self.assertFalse((first.parent / '.publish.lock').exists())

    def test_publication_refuses_changed_owned_video(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            first = workflow.reserve_run(folder, 'full')
            staged = first / 'support/song.mp4'; staged.write_bytes(b'old')
            latest = workflow.publish(staged, first, 'full')
            latest.write_bytes(b'edited by user')
            second = workflow.reserve_run(folder, 'full')
            staged = second / 'support/song.mp4'; staged.write_bytes(b'new')
            with self.assertRaisesRegex(ValueError, 'berubah'):
                workflow.publish(staged, second, 'full')
            self.assertEqual(latest.read_bytes(), b'edited by user')
            self.assertTrue(staged.exists())

    def test_publication_lock_does_not_get_removed_by_a_second_publisher(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            run = workflow.reserve_run(folder, 'full')
            staged = run / 'support/song.mp4'; staged.write_bytes(b'new')
            lock = run.parent / '.publish.lock'; lock.write_text('other job')
            with self.assertRaises(RuntimeError):
                workflow.publish(staged, run, 'full')
            self.assertEqual(lock.read_text(), 'other job')
            self.assertTrue(staged.exists())

    def test_publication_rejects_unsafe_names_and_manifest_paths(self):
        for name in ('../outside', '..', 'a/b', 'a\\b', 'C:outside'):
            with self.subTest(name=name), tempfile.TemporaryDirectory() as directory:
                folder = Path(directory)
                run = workflow.reserve_run(folder, 'full')
                staged = run / 'support/song.mp4'; staged.write_bytes(b'new')
                with self.assertRaises(ValueError):
                    workflow.publish(staged, run, 'full', song_name=name)
                manifest = run.parent / 'latest-final.json'
                manifest.write_text(json.dumps({'schema': 1, 'run': name, 'filename': 'song-final.mp4'}))
                with self.assertRaises(ValueError):
                    workflow.publish(staged, run, 'full')
                self.assertTrue(staged.exists())

    def test_publication_refuses_linked_result_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            run = workflow.reserve_run(folder, 'full')
            staged = run / 'support/song.mp4'; staged.write_bytes(b'new')
            with mock.patch.object(Path, 'is_symlink', autospec=True,
                                   side_effect=lambda path: path == folder / 'HASIL'):
                with self.assertRaisesRegex(ValueError, 'link/junction'):
                    workflow.publish(staged, run, 'full')
            self.assertTrue(staged.exists())

    def test_changed_song_name_archives_previous_without_cluttering_results(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            first = workflow.reserve_run(folder, 'full')
            staged = first / 'support/song.mp4'; staged.write_bytes(b'old')
            previous = workflow.publish(staged, first, 'full', song_name='first')
            second = workflow.reserve_run(folder, 'full')
            staged = second / 'support/song.mp4'; staged.write_bytes(b'new')
            latest = workflow.publish(staged, second, 'full', song_name='second')
            self.assertEqual(list((folder / 'HASIL').iterdir()), [latest])
            self.assertEqual((first / 'final' / previous.name).read_bytes(), b'old')

    @staticmethod
    def payload():
        return {'schema': 1, 'method': 'wav2vec2_ctc_forced_alignment',
                'audio_sha256': 'audio', 'lyrics_sha256': 'lyrics', 'coverage': [0, 10],
                'lines': [{'index': 0, 'words_text': ['Hello', 'world'],
                           'words': [[1000, 2000], [2200, 3000]], 'scores': [.9, .8]}]}

    def test_reusable_timing_validates_identity_and_complete_lyrics(self):
        payload = self.payload()
        lines = workflow.validate_timing(payload, [('verse', 'Hello world')], 'audio', 'lyrics', 10)
        self.assertEqual(lines[0]['wstart'], 1)
        for key, value in [('audio_sha256', 'other'), ('coverage', [0, 3]), ('lines', [])]:
            bad = dict(payload, **{key: value})
            with self.subTest(key=key), self.assertRaises(ValueError):
                workflow.validate_timing(bad, [('verse', 'Hello world')], 'audio', 'lyrics', 10)

    @staticmethod
    def fallback_payload(omitted_index=4):
        lines = SyncTests.fallback_lines()
        omitted = dict(index=omitted_index, words_text=lines[omitted_index]['words_text'],
                       reason='unsupported_after_retry', evidence=[
                           dict(model=model, matched_words=0, total_words=5) for model in ('small', 'medium')])
        start = lines[omitted_index-1]['wend'] if omitted_index else 0
        end = lines[omitted_index+1]['wstart'] if omitted_index+1 < len(lines) else 40
        return dict(schema=2, method='wav2vec2_ctc_forced_alignment', audio_sha256='audio',
                    lyrics_sha256='lyrics', coverage=[0, 40], source_line_count=5,
                    source_words=[x['words_text'] for x in lines],
                    policy=dict(version=1, min_line_support=.8, min_retained_tokens=.75),
                    lines=[dict(index=i, words_text=x['words_text'], words=x['words'], scores=[.9]*5)
                           for i, x in enumerate(lines) if i != omitted_index],
                    omitted_lines=[omitted], omission_windows=[[start, end]])

    def test_schema_two_partition_preserves_omitted_and_repeated_source_indices(self):
        lyrics = [('verse', x['text']) for x in SyncTests.fallback_lines()]
        for index in (0, 2, 4):
            with self.subTest(index=index):
                payload = self.fallback_payload(index)
                rows = workflow.validate_timing(payload, lyrics, 'audio', 'lyrics', 40)
                self.assertEqual(len(rows), 5)
                self.assertIsNone(rows[index]['wstart'])
                self.assertEqual(rows[index]['words'], [])
                if index < 4:
                    self.assertEqual(rows[index+1]['wstart'], 2+7*(index+1))
                with self.assertRaises(ValueError):
                    workflow.validate_timing(payload, lyrics, 'audio', 'lyrics', 40, lyric_policy='strict')

    def test_schema_two_rejects_incomplete_corrupt_or_overlapping_omissions(self):
        lyrics = [('verse', x['text']) for x in SyncTests.fallback_lines()]
        mutations = [lambda p: p['lines'].pop(),
                     lambda p: p['omitted_lines'].append(copy.deepcopy(p['omitted_lines'][0])),
                     lambda p: p['omitted_lines'][0].update(index=True),
                     lambda p: p['omitted_lines'][0].update(reason='guess'),
                     lambda p: p['omitted_lines'][0].update(evidence=[]),
                     lambda p: p['omitted_lines'][0].update(words=[[1, 2]]),
                     lambda p: p.update(omission_windows=[[0, 40]]),
                     lambda p: p.update(source_words=[]),
                     lambda p: p.update(coverage=[0, 39]),
                     lambda p: p['lines'][0].update(words=[[float('nan'), 3]]*5),
                     lambda p: p.update(policy=dict(version=9)),
                     lambda p: p['policy'].update(version=True),
                     lambda p: p.update(audio_sha256='wrong')]
        for mutate in mutations:
            payload = self.fallback_payload(); mutate(payload)
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                workflow.validate_timing(payload, lyrics, 'audio', 'lyrics', 40)

    def test_reusable_timing_rejects_lyric_mismatch_and_overlap(self):
        with self.assertRaises(ValueError):
            workflow.validate_timing(self.payload(), [('verse', 'Different words')], 'audio', 'lyrics', 10)
        bad = self.payload()
        bad['lines'][0]['words'][1] = [1500, 3000]
        with self.assertRaises(ValueError):
            workflow.validate_timing(bad, [('verse', 'Hello world')], 'audio', 'lyrics', 10)

    def test_render_cache_is_fingerprinted_and_does_not_trust_legacy(self):
        with tempfile.TemporaryDirectory() as folder:
            audio = Path(folder) / 'audio.mp3'
            audio.write_bytes(b'audio')
            cache = Path(folder) / 'prepared-cache.json'
            workflow.write_render_cache(cache, audio, self.payload(), 'en')
            from musicmerger import renderer as karaoke
            words = karaoke.whisper_words(audio, cache, language='en')
            self.assertEqual(words[0]['start'], 1)
            self.assertEqual(words[1]['w'], 'world')

    def test_render_accepts_isolated_cache_and_keeps_original_cache_untouched(self):
        from musicmerger import renderer as karaoke
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            folder = root / 'song'; folder.mkdir()
            (folder / 'v.mp4').write_bytes(b'video')
            audio = folder / 'a.mp3'; audio.write_bytes(b'audio')
            (folder / 'lyrics.md').write_text('Hello world')
            original = folder / '.karaoke_cache.json'; original.write_text('not a trusted cache')
            cache = root / 'render-cache.json'
            workflow.write_render_cache(cache, audio, self.payload(), 'en')
            info = {'streams': [{'codec_type': 'video', 'width': 640, 'height': 360}]}
            with mock.patch.object(karaoke, 'probe', return_value=info), \
                    mock.patch.object(karaoke, 'ffprobe_duration', return_value=10):
                ass = karaoke.render(folder, root / 'support', cache_path=cache,
                                     subtitles_only=True, instrumental_icon=False, palette='cyan')
            self.assertIn('Hello', ass.read_text(encoding='utf-8'))
            self.assertEqual(original.read_text(), 'not a trusted cache')

    def test_schema_two_renderer_never_realigns_skipped_repeated_words_as_asr(self):
        from musicmerger import renderer as karaoke
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); folder = root/'song'; folder.mkdir()
            (folder/'v.mp4').write_bytes(b'video')
            audio = folder/'a.mp3'; audio.write_bytes(b'audio')
            md = folder/'lyrics.md'; md.write_text('\n'.join(x['text'] for x in SyncTests.fallback_lines()))
            payload = self.fallback_payload(2)
            payload.update(audio_sha256=karaoke.audio_fingerprint(audio)['sha256'],
                           lyrics_sha256=karaoke.audio_fingerprint(md)['sha256'])
            timing = root/'timing.json'; timing.write_text(json.dumps(payload))
            info = dict(streams=[dict(codec_type='video', width=640, height=360, avg_frame_rate='24')],
                        format=dict(duration='3'))
            with mock.patch.object(karaoke, 'probe', return_value=info), \
                    mock.patch.object(karaoke, 'ffprobe_duration', return_value=40), \
                    mock.patch.object(karaoke, 'whisper_words', side_effect=AssertionError('No ASR rematching')):
                ass = karaoke.render(folder, root/'support', timing_file=timing,
                                     subtitles_only=True, palette='cyan')
            report = json.loads((ass.parent/'song.alignment.json').read_text())
            self.assertIsNone(report['lines'][2]['wstart'])
            self.assertEqual(report['lines'][3]['wstart'], 23)
            windows = report['lyric_fallback']['display_windows']
            self.assertEqual(len(windows), 1)
            self.assertGreaterEqual(windows[0][0], 11.7)
            self.assertLessEqual(windows[0][1], 22.88)
            self.assertEqual(report['style']['equalizer']['hidden_windows'], windows)
            self.assertEqual(md.read_text().splitlines(), [x['text'] for x in SyncTests.fallback_lines()])

    def test_find_timing_ignores_wrong_identity_and_prefers_song_cache(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            cache = folder / 'MusicMerger-output' / 'cache'
            cache.mkdir(parents=True)
            wrong = dict(self.payload(), audio_sha256='different')
            (cache / 'timing-wrong.json').write_text(json.dumps(wrong))
            correct = cache / 'timing-correct.json'
            correct.write_text(json.dumps(self.payload()))
            self.assertEqual(workflow.find_timing(folder, [('verse', 'Hello world')],
                'audio', 'lyrics', 10, candidates=[cache / 'timing-wrong.json', correct]), correct)

    def test_child_failure_is_reported_and_log_retained(self):
        import sys
        from musicmerger.process import run_command
        with tempfile.TemporaryDirectory() as folder:
            log = Path(folder) / 'worker.log'
            with self.assertRaises(RuntimeError):
                run_command([sys.executable, '-c', 'print("failure details");raise SystemExit(7)'], log)
            self.assertIn('failure details', log.read_text(encoding='utf-8'))

    def test_unicode_worker_progress_does_not_crash_a_legacy_windows_console(self):
        import sys
        from musicmerger.process import run_command
        with tempfile.TemporaryDirectory() as folder:
            log = Path(folder) / 'worker.log'
            output = io.BytesIO()
            terminal = io.TextIOWrapper(output, encoding='ascii')
            with contextlib.redirect_stdout(terminal):
                run_command([sys.executable, '-c', 'print(chr(0x2588))'], log)
            self.assertIn('\u2588', log.read_text(encoding='utf-8'))

    def test_workflow_reuses_verified_timing_without_touching_source_cache(self):
        from musicmerger import renderer as karaoke
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            (folder / 'video.mp4').write_bytes(b'video')
            audio = folder / 'music.mp3'
            audio.write_bytes(b'audio')
            lyrics = folder / 'lyrics.md'
            lyrics.write_text('Hello world')
            original_cache = folder / '.karaoke_cache.json'
            original_cache.write_text('do not touch')
            payload = self.payload()
            payload.update(audio_sha256=karaoke.audio_fingerprint(audio)['sha256'],
                           lyrics_sha256=karaoke.audio_fingerprint(lyrics)['sha256'])
            timing = folder / 'known.json'
            timing.write_text(json.dumps(payload))
            args = musicmerger.options([str(folder), '--mode', 'preview', '--timing-file', str(timing)])
            def render(command, log, **kwargs):
                self.assertIn('--cache-file', command)
                self.assertNotIn('--trust-legacy-cache', command)
                output = Path(command[command.index('--out') + 1])
                (output / (folder.name + '.mp4')).write_bytes(b'finished')
                (output / (folder.name + '.ass')).write_text('subtitle')
            with mock.patch.object(karaoke, 'ffprobe_duration', return_value=10), \
                    mock.patch('musicmerger.workflow.run_command', side_effect=render) as worker:
                result = workflow.run(args)
            self.assertEqual(worker.call_count, 1)
            self.assertEqual(original_cache.read_text(), 'do not touch')
            self.assertEqual(result.read_bytes(), b'finished')
            status = json.loads((result.parent.parent / 'status.json').read_text())
            self.assertEqual(status['status'], 'complete')

    def test_fresh_workflow_prepares_timing_before_render(self):
        from musicmerger import renderer as karaoke
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            (folder / 'video.mp4').write_bytes(b'video')
            audio = folder / 'music.mp3'; audio.write_bytes(b'audio')
            lyrics = folder / 'lyrics.md'; lyrics.write_text('Hello world')
            args = musicmerger.options([str(folder), '--mode', 'full'])
            def worker(command, log, **kwargs):
                if command[3] == 'musicmerger.sync':
                    run = Path(command[5])
                    payload = self.payload()
                    payload.update(audio_sha256=karaoke.audio_fingerprint(audio)['sha256'],
                                   lyrics_sha256=karaoke.audio_fingerprint(lyrics)['sha256'])
                    (run / 'timing/timing.json').write_text(json.dumps(payload))
                else:
                    output = Path(command[command.index('--out') + 1])
                    (output / (folder.name + '.mp4')).write_bytes(b'finished')
            with mock.patch.object(karaoke, 'ffprobe_duration', return_value=10), \
                    mock.patch('musicmerger.workflow.run_command', side_effect=worker) as process:
                result = workflow.run(args)
            self.assertEqual(process.call_count, 2)
            self.assertEqual(result, folder / 'HASIL/music-final.mp4')

    def test_failed_worker_records_status_without_publishing(self):
        from musicmerger import renderer as karaoke
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            (folder / 'video.mp4').write_bytes(b'video')
            (folder / 'music.mp3').write_bytes(b'audio')
            (folder / 'lyrics.md').write_text('Hello world')
            args = musicmerger.options([str(folder), '--mode', 'full'])
            with mock.patch.object(karaoke, 'ffprobe_duration', return_value=10), \
                    mock.patch('musicmerger.workflow.run_command', side_effect=RuntimeError('worker failed')):
                with self.assertRaises(RuntimeError):
                    workflow.run(args)
            jobs = list((folder / 'MusicMerger-output').iterdir())
            status = json.loads((jobs[0] / 'status.json').read_text())
            self.assertEqual(status['status'], 'failed')
            self.assertEqual(list((jobs[0] / 'final').iterdir()), [])


class SyncTests(unittest.TestCase):
    def test_asr_cache_reuse_checks_identity_language_and_keeps_source_untouched(self):
        from musicmerger.sync import reuse_asr_cache
        from musicmerger import renderer as karaoke
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); audio = root/'a.mp3'; audio.write_bytes(b'audio')
            old = root/'old/support/asr-cache.json'; old.parent.mkdir(parents=True)
            new = root/'new/support/asr-cache.json'; new.parent.mkdir(parents=True)
            data = dict(schema=2, audio=karaoke.audio_fingerprint(audio), model='small', language='auto',
                        words=[dict(w='hello', start=1, end=2)])
            old.write_text(json.dumps(data)); original = old.read_bytes()
            with mock.patch.object(karaoke, 'whisper_words', side_effect=AssertionError('Cache lookup must not run ASR')):
                self.assertFalse(reuse_asr_cache(audio, new, language='en'))
                self.assertTrue(reuse_asr_cache(audio, new, language='auto'))
            self.assertEqual(new.read_bytes(), original)
            self.assertEqual(old.read_bytes(), original)

    @staticmethod
    def fallback_lines():
        return [dict(label='verse', text='these are five clear words',
                     words_text=['these', 'are', 'five', 'clear', 'words'], nwords=5,
                     wstart=2 + 7*i, wend=4 + 7*i,
                     words=[[(2+7*i+j*.4)*1000, (2+7*i+(j+1)*.4)*1000] for j in range(5)],
                     matched_words=5, estimated_words=0, provenance=['exact']*5,
                     issues=[], needs_review=False) for i in range(5)]

    def test_second_model_rescues_weak_first_model_without_mutating_source(self):
        from musicmerger.fallback import select_lines
        small = self.fallback_lines(); medium = copy.deepcopy(small)
        small[0].update(matched_words=2, estimated_words=3)
        original = copy.deepcopy(small)
        chosen, omitted = select_lines(small, medium, 40)
        self.assertEqual(chosen[0]['matched_words'], 5)
        self.assertEqual(omitted, [])
        self.assertEqual(small, original)

    def test_rescued_line_uses_compatible_neighbor_model_without_clipping_times(self):
        from musicmerger.fallback import select_lines
        small = self.fallback_lines(); medium = copy.deepcopy(small)
        small[0].update(matched_words=2, estimated_words=3)
        medium[0]['wend'] = 4.2
        small[1]['wstart'] = 4.1
        medium[1]['wstart'] = 4.2
        chosen, omitted = select_lines(small, medium, 40)
        self.assertEqual(omitted, [])
        self.assertEqual(chosen[1], medium[1])
        self.assertEqual(chosen[0]['wend'], 4.2)

    def test_local_weak_or_missing_section_keeps_original_indices_and_later_times(self):
        from musicmerger.fallback import select_lines
        for index in (0, 2, 4):
            with self.subTest(index=index):
                small = self.fallback_lines(); medium = copy.deepcopy(small)
                small[index].update(matched_words=0, wstart=None, wend=None, words=[])
                medium[index].update(matched_words=3, estimated_words=2)
                selected, omitted = select_lines(small, medium, 40)
                self.assertEqual([r['index'] for r in omitted], [index])
                self.assertIsNone(selected[index]['wstart'])
                for i in set(range(5)) - {index}:
                    self.assertEqual(selected[i]['wstart'], small[i]['wstart'])

    def test_mostly_wrong_lyrics_and_unresolvable_chronology_still_stop(self):
        from musicmerger.fallback import select_lines
        lines = self.fallback_lines()
        for row in lines[-2:]:
            row.update(matched_words=0, wstart=None, wend=None, words=[])
        with self.assertRaisesRegex(ValueError, '75%'):
            select_lines(lines, copy.deepcopy(lines), 40)
        lines = self.fallback_lines(); lines[2]['wstart'] = 1
        with self.assertRaises(ValueError):
            select_lines(lines, copy.deepcopy(lines), 40)

    def test_strict_policy_rejects_missing_lines_and_cli_exposes_policy(self):
        from musicmerger.fallback import select_lines
        lines = self.fallback_lines(); lines[-1]['wstart'] = None
        with self.assertRaises(ValueError):
            select_lines(lines, copy.deepcopy(lines), 40, policy='strict')
        self.assertEqual(musicmerger.options(['D:/song', '--mode', 'full']).lyric_policy, 'auto')
        self.assertEqual(musicmerger.options(['D:/song', '--mode', 'full', '--lyric-policy', 'strict']).lyric_policy, 'strict')

    def test_ctc_supports_lowercase_vocabularies(self):
        from musicmerger.acoustic import ctc_word_spans
        spans = ctc_word_spans([[-10, -10, 0], [0, -10, -10]], ['A'], {'|': 1, 'a': 2}, 0)
        self.assertEqual(spans[0]['start_frame'], 0)
    def test_refinement_does_not_accept_a_worse_or_overlapping_candidate(self):
        from musicmerger.acoustic import better_candidate
        old = [{'start_frame': 1, 'end_frame': 5, 'score': .4},
               {'start_frame': 7, 'end_frame': 10, 'score': .4}]
        self.assertFalse(better_candidate(old, [dict(x, score=.3) for x in old]))
        self.assertFalse(better_candidate(old, [dict(old[0], end_frame=9, score=.9), dict(old[1], score=.9)]))
        self.assertTrue(better_candidate(old, [dict(x, score=.6) for x in old]))

    def test_quality_report_is_diagnostic_not_an_accuracy_percentage(self):
        from musicmerger.acoustic import timing_quality
        payload = WorkflowTests.payload()
        payload['lines'][0]['words'][0] = [1000, 1020]
        payload['lines'][0]['scores'][0] = .2
        report = timing_quality(payload['lines'])
        self.assertEqual(report['very_short_words'], 1)
        self.assertEqual(report['low_score_words'], 1)
        self.assertTrue(report['review_required'])

    def test_unsupported_language_never_silently_uses_english_model(self):
        from musicmerger.sync import model_for
        with self.assertRaisesRegex(ValueError, 'id'):
            model_for('id', None)
        self.assertTrue(model_for('en', None).is_dir())
