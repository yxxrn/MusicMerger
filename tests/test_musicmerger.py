"""User-facing CLI and isolated output workflow regression tests."""
import contextlib
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
            self.assertEqual(final.parent.name, 'final')
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
            self.assertEqual(result.parent.name, 'final')

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
