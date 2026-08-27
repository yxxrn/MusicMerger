"""Offline regression coverage for subtitle parsing, alignment and ASS timing."""
import hashlib
import copy
import json
import re
import shutil
import subprocess
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

from musicmerger import renderer as karaoke


class AssTimestampTests(unittest.TestCase):
    def test_centiseconds_are_preserved(self):
        self.assertEqual(karaoke.ass_ts(12.34), "0:00:12.34")

    def test_rounding_carries_into_minutes_and_hours(self):
        self.assertEqual(karaoke.ass_ts(59.999), "0:01:00.00")
        self.assertEqual(karaoke.ass_ts(3599.999), "1:00:00.00")

    def test_negative_time_is_clamped(self):
        self.assertEqual(karaoke.ass_ts(-0.5), "0:00:00.00")


class LyricsParsingTests(unittest.TestCase):
    def test_plain_markdown_lyrics_work_without_section_markers(self):
        self.assertEqual(self.parse('Hello world\nSing with me\n'),
                         [('verse', 'Hello world'), ('verse', 'Sing with me')])

    def test_plain_lyrics_do_not_include_title_or_instruction_section(self):
        self.assertEqual(self.parse('# My song\nHello world\n## Instructions\nDo not sing this\n'),
                         [('verse', 'Hello world')])
    def test_ordinary_lyrics_starting_with_notes_are_not_metadata(self):
        result = self.parse('Lyrics\n[Verse]\nMorning comes\nNotes of love fill the air\nWe sing again\nStyle Prompt\nblues')
        self.assertEqual([text for _, text in result], ['Morning comes', 'Notes of love fill the air', 'We sing again'])

    def parse(self, content):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "lyrics.md"
            path.write_text(content, encoding="utf-8")
            return karaoke.parse_lyrics(path)

    def test_numbered_sections_without_heading_are_accepted(self):
        result = self.parse("[Verse 1]\nMorning comes\n[Chorus]\nSing along\n")
        self.assertEqual([text for _, text in result], ["Morning comes", "Sing along"])

    def test_lyrics_heading_and_style_prompt_boundary(self):
        result = self.parse(
            "# Song title\nIgnore this preamble\n## Lyrics\n[Verse 1]\n"
            "Morning comes\n[Chorus]\nSing along\n## Style Prompt\nNever display this\n"
        )
        self.assertEqual([text for _, text in result], ["Morning comes", "Sing along"])


class KaraokeTagTests(unittest.TestCase):
    @staticmethod
    def line(text="Hello, world!", words=None):
        return {
            "label": "verse", "text": text, "nwords": 2,
            "wstart": 1.0, "wend": 4.5,
            "words": words or [(1000.0, 1500.0), (4000.0, 4500.0)],
            "words_text": ["Hello", "world"],
        }

    def test_karaoke_overrides_have_ass_braces(self):
        tags = karaoke.karaoke_tags(self.line())
        self.assertRegex(tags, r"\{\\kf?\d+\}")
        # A backslash-k outside override braces is displayed literally by libass.
        visible = re.sub(r"\{[^}]*\}", "", tags)
        self.assertNotIn("\\k", visible)

    def test_visible_lyrics_remove_punctuation_but_keep_spaces(self):
        tags = karaoke.karaoke_tags(self.line())
        visible = re.sub(r"\{[^}]*\}", "", tags)
        self.assertEqual(visible, "Hello world")

    def test_display_tokens_keep_contractions_and_split_hyphenated_words(self):
        self.assertEqual(karaoke.display_words("Don't sing worn-out words!"),
                         ["Dont", "sing", "worn", "out", "words"])
        self.assertEqual(karaoke.display_words("“Don’t”—sing… café！"),
                         ["Dont", "sing", "café"])

    def test_display_cleanup_preserves_source_and_word_timing(self):
        line = {"label": "verse", "text": "“Don’t” sing—worn-out… words!",
                "nwords": 5, "wstart": 1.0, "wend": 5.5,
                "words": [(1000, 1500), (2000, 2500), (3000, 3500),
                          (4000, 4500), (5000, 5500)],
                "words_text": ["Don’t", "sing", "worn", "out", "words"]}
        source = copy.deepcopy(line)
        tags = karaoke.karaoke_tags(line)
        self.assertEqual(re.sub(r"\{[^}]*\}", "", tags), "Dont sing worn out words")
        cursor = 0
        starts = []
        for part in re.finditer(r"\{\\kf?(\d+)\}([^{}]*)", tags):
            if part.group(2).strip():
                starts.append(cursor)
            cursor += int(part.group(1))
        self.assertEqual(starts, [0, 100, 200, 300, 400])
        self.assertEqual(line, source)

    def test_silence_does_not_advance_next_word_early(self):
        line = self.line(words=[(1200.0, 1500.0), (4000.0, 4500.0)])
        tags = karaoke.karaoke_tags(line)
        parts = list(re.finditer(r"\{\\kf?(\d+)\}([^{}]*)", tags))
        self.assertTrue(parts, "No valid ASS karaoke tags were generated")
        cursor_cs = 0
        starts = {}
        for part in parts:
            text = part.group(2).strip(" ,!")
            if text:
                starts[text] = cursor_cs
            cursor_cs += int(part.group(1))
        self.assertEqual(starts, {"Hello": 20, "world": 300})
        self.assertEqual(cursor_cs, 350)


class AlignmentTests(unittest.TestCase):
    def test_fuzzy_only_line_is_not_confident(self):
        result = karaoke.build_line_timing([('verse', 'cat')], [self.word('bat', 10, 11)])
        self.assertTrue(result[0]['needs_review'])

    def test_interpolated_word_requires_review(self):
        result = karaoke.build_line_timing([('verse', 'hello moon world')],
                                          [self.word('hello', 1, 2), self.word('world', 3, 4)])
        self.assertTrue(result[0]['needs_review'])

    def test_cross_line_overlap_is_flagged_and_cannot_silently_drop_lyrics(self):
        result = karaoke.build_line_timing([('verse', 'hello'), ('verse', 'world')],
                                          [self.word('hello', 1, 5), self.word('world', 2, 3)])
        self.assertTrue(all(line['needs_review'] for line in result))
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(ValueError):
                karaoke.build_ass(result, Path(directory) / 'overlap.ass')

    @staticmethod
    def word(text, start, end):
        return {"w": text, "start": start, "end": end}

    def test_more_than_five_missing_opening_words_keep_later_anchors(self):
        result = karaoke.build_line_timing(
            [("verse", "alpha bravo charlie delta echo foxtrot golden silver")],
            [self.word("golden", 7.0, 7.5), self.word("silver", 8.0, 8.5)],
        )
        self.assertEqual(result[0]["words"][-2:], [(7000.0, 7500.0), (8000.0, 8500.0)])

    def test_repeated_chorus_uses_distinct_occurrences_in_order(self):
        lines = [("chorus", "shine bright"), ("verse", "river flows"),
                 ("chorus", "shine bright")]
        words = [self.word(text, float(i * 2), float(i * 2 + 1))
                 for i, text in enumerate("shine bright river flows shine bright".split())]
        result = karaoke.build_line_timing(lines, words)
        self.assertEqual([line["wstart"] for line in result], [0.0, 4.0, 8.0])
        self.assertEqual(result[2]["words"], [(8000.0, 9000.0), (10000.0, 11000.0)])

    def test_entirely_unmatched_line_has_no_fabricated_timing(self):
        try:
            result = karaoke.build_line_timing(
                [("verse", "xylophone zebra")], [self.word("moon", 10.0, 11.0)]
            )
        except ValueError:
            return  # Refusing an unsupported alignment is also safe.
        self.assertTrue(all(line.get("wstart") is None and not line.get("words")
                            for line in result),
                        "Unmatched lyrics must not receive invented playback times")

    def test_interpolated_word_intervals_are_nonnegative_and_ordered(self):
        result = karaoke.build_line_timing(
            [("verse", "zero one two three four five")],
            [self.word("one", 0.0, 0.2), self.word("four", 0.5, 0.7),
             self.word("five", 0.8, 1.0)],
        )
        words = result[0]["words"]
        previous_end = 0.0
        for start, end in words:
            self.assertGreaterEqual(start, previous_end)
            self.assertGreaterEqual(end, start)
            previous_end = end

    def test_zero_duration_asr_word_requires_review(self):
        result = karaoke.build_line_timing(
            [("verse", "shine bright")],
            [self.word("shine", 1.0, 1.0), self.word("bright", 2.0, 2.5)],
        )
        self.assertTrue(result[0]["needs_review"])


class CacheTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.audio = Path(directory.name) / "song.mp3"
        self.audio.write_bytes(b"synthetic audio bytes; not a real mp3")
        self.cache = Path(directory.name) / ".karaoke_cache.json"
        self.words = [{"w": "hello", "start": 1.0, "end": 1.5}]
        self.fingerprint = {"sha256": hashlib.sha256(self.audio.read_bytes()).hexdigest(),
                            "bytes": self.audio.stat().st_size}
        self.provider = mock.Mock(side_effect=AssertionError("Provider calls forbidden in offline tests"))
        patch = mock.patch.dict("sys.modules", {
            "faster_whisper": types.SimpleNamespace(WhisperModel=self.provider)
        })
        patch.start()
        self.addCleanup(patch.stop)

    def write_cache(self, data):
        self.cache.write_text(json.dumps(data), encoding="utf-8")

    def schema2(self, **overrides):
        data = {"schema": 2, "audio": self.fingerprint,
                "model": karaoke.MODEL, "language": "en", "words": self.words}
        data.update(overrides)
        return data

    def test_fingerprint_includes_file_content_and_size(self):
        self.assertEqual(karaoke.audio_fingerprint(self.audio), self.fingerprint)

    def test_legacy_cache_requires_explicit_trust(self):
        self.write_cache({"model": karaoke.MODEL, "words": self.words})
        with self.assertRaises(ValueError):
            karaoke.whisper_words(self.audio, self.cache)
        self.provider.assert_not_called()

    def test_explicitly_trusted_legacy_cache_preserves_words(self):
        self.write_cache({"model": karaoke.MODEL, "words": self.words})
        self.assertEqual(karaoke.whisper_words(self.audio, self.cache, trust_legacy=True), self.words)
        self.provider.assert_not_called()

    def test_matching_schema2_cache_is_accepted(self):
        self.write_cache(self.schema2())
        self.assertEqual(karaoke.whisper_words(self.audio, self.cache), self.words)
        self.provider.assert_not_called()

    def test_non_object_cache_is_rejected_without_inference(self):
        self.write_cache([])
        with self.assertRaises(ValueError):
            karaoke.whisper_words(self.audio, self.cache)
        self.provider.assert_not_called()

    def test_different_audio_is_rejected_even_with_legacy_trust(self):
        self.write_cache(self.schema2(audio={"sha256": "0" * 64, "bytes": 1}))
        with self.assertRaises(ValueError):
            karaoke.whisper_words(self.audio, self.cache, trust_legacy=True)
        self.provider.assert_not_called()

    def test_different_model_or_language_is_rejected(self):
        for override in ({"model": "different-model"}, {"language": "id"}):
            with self.subTest(override=override):
                self.write_cache(self.schema2(**override))
                with self.assertRaises(ValueError):
                    karaoke.whisper_words(self.audio, self.cache)
                self.provider.assert_not_called()


class InputSelectionTests(unittest.TestCase):
    def test_one_input_per_type_is_selected_case_insensitively(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            expected = tuple(folder / name for name in ("clip.MP4", "song.Mp3", "lyrics.MD"))
            for path in expected:
                path.write_bytes(b"fixture")
            self.assertEqual(tuple(karaoke.input_files(folder)), expected)

    def test_ambiguous_or_missing_input_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            for name in ("clip.mp4", "song.mp3", "lyrics.md"):
                (folder / name).write_bytes(b"fixture")
            for suffix in ("mp4", "mp3", "md"):
                with self.subTest(suffix=suffix):
                    extra = folder / f"second.{suffix}"
                    extra.write_bytes(b"fixture")
                    with self.assertRaises(ValueError):
                        karaoke.input_files(folder)
                    extra.unlink()
            (folder / "lyrics.md").unlink()
            with self.assertRaises(ValueError):
                karaoke.input_files(folder)


class RenderCommandTests(unittest.TestCase):
    def test_provided_font_is_copied_without_overwriting_a_different_font(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / 'Mirage.otf'
            source.write_bytes(b'font fixture')
            with mock.patch.object(karaoke, 'DEFAULT_FONT_FILE', source):
                fonts = karaoke.package_font(root / 'output', karaoke.DEFAULT_FONT)
                self.assertEqual((fonts / 'Mirage.otf').read_bytes(), source.read_bytes())
                (fonts / 'Mirage.otf').write_bytes(b'existing different output font')
                with self.assertRaises(ValueError):
                    karaoke.package_font(root / 'output', karaoke.DEFAULT_FONT)
                self.assertEqual(source.read_bytes(), b'font fixture')

    def test_missing_mirage_is_not_silently_replaced(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with mock.patch.object(karaoke, 'DEFAULT_FONT_FILE', root / 'missing.otf'):
                with self.assertRaises(ValueError):
                    karaoke.package_font(root / 'output', karaoke.DEFAULT_FONT)

    def test_bundled_fonts_are_loaded_by_libass(self):
        command = karaoke.render_command(Path("clip.mp4"), Path("song.mp3"),
                                         Path("out.mp4"), duration=5)
        graph = command[command.index("-vf") + 1]
        self.assertIn("ass=lyrics.ass:fontsdir=fonts", graph)

    def test_loop_and_audio_mapping_use_separate_safe_arguments(self):
        video = Path("input folder/clip.mp4")
        audio = Path("input folder/song.mp3")
        output = Path("output folder/result.mp4")
        command = karaoke.render_command(video, audio, output, start=0, duration=5, width=None)
        inputs = [command[i + 1] for i, token in enumerate(command) if token == "-i"]
        self.assertEqual(inputs, [str(video), str(audio)])
        self.assertEqual(command[-1], str(output))
        self.assertIn("-n", command)
        self.assertNotIn("-y", command)
        self.assertLess(command.index("-stream_loop"), command.index("-i"))
        self.assertEqual(command[command.index("-stream_loop") + 1], "-1")
        self.assertFalse(any("loop=" in token for token in command))
        mappings = [command[i + 1] for i, token in enumerate(command) if token == "-map"]
        self.assertIn("1:a:0", mappings)
        self.assertEqual(float(command[command.index("-t") + 1]), 5.0)

    def test_preview_seek_restores_song_clock_before_ass(self):
        command = karaoke.render_command(Path("clip.mp4"), Path("song.mp3"), Path("out.mp4"),
                                         start=12.5, duration=5, width=None)
        graph = next(token for token in command if "ass=" in token)
        before_ass = graph.split("ass=", 1)[0]
        shift = re.search(r"setpts=[^,;]*\+([\d.]+)/TB", before_ass)
        self.assertIsNotNone(shift, "ASS must see original song time after seeking")
        self.assertEqual(float(shift.group(1)), 12.5)


class PaletteTests(unittest.TestCase):
    def test_gray_uses_gold_and_selection_is_stable(self):
        pixels = bytes([120, 120, 120]) * 100
        selected = karaoke.choose_palette(pixels)
        self.assertEqual(selected["name"], "gold")
        self.assertEqual(selected, karaoke.choose_palette(pixels))
        self.assertRegex(selected["highlight"], r"^&H00[0-9A-Fa-f]{6}&$")

    def test_palette_avoids_matching_dominant_background(self):
        for background, excluded in (((255, 220, 60), "gold"),
                                     ((50, 220, 255), "cyan")):
            with self.subTest(background=background):
                selected = karaoke.choose_palette(bytes(background) * 100)
                self.assertIn(selected["name"], {"gold", "cyan", "mint", "rose"})
                self.assertNotEqual(selected["name"], excluded)


class AssEventIntegrationTests(unittest.TestCase):
    def test_no_next_line_does_not_overlay_two_main_lyrics(self):
        lines = karaoke.build_line_timing([('verse', 'hello'), ('verse', 'world')],
            [{'w': 'hello', 'start': 1, 'end': 1.5}, {'w': 'world', 'start': 1.6, 'end': 2.2}])
        main = [e for e in self.events(lines, show_next=False) if e['style'] == 'Main']
        self.assertLessEqual(main[0]['end'], main[1]['start'])
        self.assertLessEqual(main[1]['start'], 1.6)

    def test_short_line_does_not_promote_an_unseen_preview(self):
        lines = karaoke.build_line_timing([('verse', 'hello'), ('verse', 'world'), ('verse', 'again')],
            [{'w': 'hello', 'start': 1, 'end': 2}, {'w': 'world', 'start': 2, 'end': 2.1},
             {'w': 'again', 'start': 2.1, 'end': 3}])
        events = self.events(lines)
        upcoming = [e for e in events if e['style'] == 'Next' and 'again' in e['text']]
        active = next(e for e in events if e['style'] == 'Main' and 'again' in e['text'])
        self.assertEqual(upcoming, [])
        self.assertNotIn('\\move(', active['text'])

    @staticmethod
    def timestamp(value):
        hours, minutes, seconds = value.split(":")
        return int(hours) * 3600 + int(minutes) * 60 + float(seconds)

    def events(self, lines, **options):
        options.setdefault('layout', 'lines')
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "lyrics.ass"
            karaoke.build_ass(lines, target, **options)
            events = []
            for line in target.read_text(encoding="utf-8").splitlines():
                if line.startswith("Dialogue:"):
                    fields = line.split(",", 9)
                    events.append({"start": self.timestamp(fields[1]), "end": self.timestamp(fields[2]),
                                   "style": fields[3], "text": fields[9]})
            return events

    def test_adjacent_main_lines_overlap_only_for_short_outgoing_fade(self):
        lines = karaoke.build_line_timing(
            [("verse", "hello"), ("verse", "world")],
            [{"w": "hello", "start": 1.0, "end": 1.5},
             {"w": "world", "start": 1.6, "end": 2.2}],
        )
        main = [event for event in self.events(lines) if event["style"] == "Main"]
        self.assertEqual(len(main), 2)
        self.assertGreaterEqual(main[1]["start"], 1.5)
        self.assertGreaterEqual(main[0]["end"], 1.5)
        self.assertLessEqual(main[0]["end"], 1.7)
        self.assertTrue(all(event["end"] > event["start"] for event in main))

    def test_preview_promotes_upward_without_duplicate_or_shifted_word_clock(self):
        lines = karaoke.build_line_timing(
            [("verse", "hello!"), ("verse", "world?")],
            [{"w": "hello", "start": 1.0, "end": 2.0},
             {"w": "world", "start": 3.0, "end": 4.0}],
        )
        events = self.events(lines)
        main = [event for event in events if event["style"] == "Main"]
        preview = [event for event in events if event["style"] == "Next"]
        self.assertEqual(len(main), 2)
        self.assertEqual(len(preview), 1)
        outgoing, incoming = main
        self.assertEqual(outgoing["end"], 2.18)
        fade = re.search(r"\\fad\((\d+),(\d+)\)", outgoing["text"])
        self.assertIsNotNone(fade)
        self.assertAlmostEqual(outgoing["end"] - int(fade.group(2)) / 1000, 2.0)
        self.assertEqual(preview[0]["end"], incoming["start"])
        self.assertEqual(incoming["start"], 2.0)
        move = re.search(r"\\move\(([^)]+)\)", incoming["text"])
        self.assertIsNotNone(move)
        coordinates = [float(value) for value in move.group(1).split(",")]
        self.assertEqual(coordinates, [960, round(1088 * .65), 960,
                                       round(1088 * .50), 0, 320])
        self.assertIn(r"\an5", incoming["text"])
        self.assertIn(r"\an5", preview[0]["text"])
        self.assertNotIn(r"\pos(", incoming["text"])
        self.assertRegex(incoming["text"], r"\\t\([^)]*\\fs\d+")
        cursor = 0
        word_start = None
        for part in re.finditer(r"\{\\kf?(\d+)\}([^{}]*)", incoming["text"]):
            if part.group(2).strip():
                word_start = incoming["start"] + cursor / 100
                break
            cursor += int(part.group(1))
        self.assertAlmostEqual(word_start, 3.0)
        self.assertEqual(re.sub(r"\{[^}]*\}", "", preview[0]["text"]), "world")

    def test_default_mirage_font_and_custom_highlight_are_written_to_header(self):
        self.assertEqual(karaoke.DEFAULT_FONT, "MADE Mirage")
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "lyrics.ass"
            karaoke.build_ass([KaraokeTagTests.line()], target, layout='lines', highlight="&H00FFC864&")
            content = target.read_text(encoding="utf-8")
        styles = [line for line in content.splitlines()
                  if line.startswith(("Style: Main,", "Style: Next,"))]
        self.assertEqual(len(styles), 2)
        self.assertTrue(all(line.split(",")[1] == "MADE Mirage" for line in styles))
        main = next(line for line in styles if line.startswith("Style: Main,"))
        self.assertEqual(main.split(",")[3], "&H00FFC864&")

    def test_centered_large_lyrics_have_no_background_panel(self):
        lines = karaoke.build_line_timing(
            [('verse', 'hello'), ('verse', 'world')],
            [{'w': 'hello', 'start': 1, 'end': 2},
             {'w': 'world', 'start': 3, 'end': 4}])
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / 'lyrics.ass'
            karaoke.build_ass(lines, target, layout='lines')
            content = target.read_text(encoding='utf-8')
        styles = {line.split(',')[0]: line.split(',') for line in content.splitlines()
                  if line.startswith('Style:')}
        self.assertEqual(set(styles), {'Style: Main', 'Style: Next'})
        self.assertGreaterEqual(int(styles['Style: Main'][2]), 90)
        self.assertGreaterEqual(int(styles['Style: Next'][2]), 60)
        self.assertLess(int(styles['Style: Next'][2]), int(styles['Style: Main'][2]))
        self.assertTrue(all(style[15] == '1' for style in styles.values()))
        self.assertNotIn('Panel', content)
        self.assertNotIn(r'\p1', content)
        events = self.events(lines)
        self.assertIn(r'\pos(960,544)', events[0]['text'])
        preview = next(event for event in events if event['style'] == 'Next')
        self.assertIn(r'\pos(960,707)', preview['text'])

    def test_disabling_next_line_also_disables_promotion(self):
        lines = karaoke.build_line_timing(
            [("verse", "hello"), ("verse", "world")],
            [{"w": "hello", "start": 1.0, "end": 2.0},
             {"w": "world", "start": 3.0, "end": 4.0}],
        )
        events = self.events(lines, show_next=False)
        self.assertEqual([event["style"] for event in events
                          if event["style"] in ("Main", "Next")], ["Main", "Main"])
        self.assertFalse(any(r"\move(" in event["text"] for event in events))

    def test_no_next_line_preview_across_long_instrumental(self):
        lines = karaoke.build_line_timing(
            [("verse", "hello"), ("chorus", "world")],
            [{"w": "hello", "start": 1.0, "end": 1.5},
             {"w": "world", "start": 10.0, "end": 11.0}],
        )
        events = self.events(lines)
        self.assertFalse(any(event["style"] == "Next" for event in events))
        self.assertFalse(any(r"\move(" in event["text"] for event in events))


class PhraseTests(unittest.TestCase):
    @staticmethod
    def line(text, gap_after=None):
        words = karaoke.display_words(text)
        intervals, cursor = [], 1000
        for i in range(len(words)):
            intervals.append((cursor, cursor + 400))
            cursor += 400 + (600 if i == gap_after else 0)
        return {'text': text, 'words': intervals, 'wstart': 1,
                'wend': intervals[-1][1] / 1000, 'label': 'verse'}

    def test_phrase_boundaries_use_punctuation_and_preserve_every_word_time(self):
        line = self.line('Shape the wood, let the grain run free')
        source = copy.deepcopy(line)
        phrases = karaoke.split_phrases([line])
        self.assertEqual([p['text'] for p in phrases],
                         ['Shape the wood', 'let the grain run free'])
        self.assertEqual([w for p in phrases for w in p['words']], line['words'])
        self.assertEqual([p['word_range'] for p in phrases], [[0, 3], [3, 8]])
        self.assertTrue(all(p['source_line'] == 1 for p in phrases))
        self.assertEqual(line, source)

    def test_long_lines_split_at_pauses_without_losing_contractions(self):
        line = self.line("Don't fear shadows follow every golden light", gap_after=2)
        phrases = karaoke.split_phrases([line])
        self.assertEqual(phrases[0]['text'], 'Dont fear shadows')
        self.assertEqual(' '.join(p['text'] for p in phrases),
                         ' '.join(karaoke.display_words(line['text'])))
        self.assertTrue(all(len(p['words']) <= 5 for p in phrases))

    def test_single_long_word_is_not_truncated_and_unaligned_line_is_not_invented(self):
        line = self.line('supercalifragilisticexpialidocious')
        phrases = karaoke.split_phrases([line, {'text': 'missing', 'wstart': None}])
        self.assertEqual([p['text'] for p in phrases], [line['text']])
        self.assertEqual(phrases[0]['words'], line['words'])

    def test_common_modifier_stays_with_following_noun(self):
        phrases = karaoke.split_phrases([self.line('When the final edge is smooth')])
        self.assertEqual([p['text'] for p in phrases], ['When the final edge', 'is smooth'])

    def test_cli_defaults_to_phrases_and_keeps_old_layout_selectable(self):
        with mock.patch.object(karaoke, 'render') as render:
            karaoke.main(['inputs/woodworking'])
            self.assertEqual(render.call_args.kwargs['layout'], 'phrases')
            karaoke.main(['inputs/woodworking', '--layout', 'lines'])
            self.assertEqual(render.call_args.kwargs['layout'], 'lines')

    def test_invalid_segmentation_limits_and_word_counts_fail(self):
        for options in ({'max_words': 0}, {'max_chars': 0}):
            with self.subTest(options=options), self.assertRaises(ValueError):
                karaoke.split_phrases([self.line('hello world')], **options)
        line = self.line('hello world')
        line['words'].pop()
        with self.assertRaises(ValueError):
            karaoke.split_phrases([line])

    def test_default_ass_is_one_large_centered_phrase_without_motion_or_panel(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / 'lyrics.ass'
            karaoke.build_ass([self.line('Shape the wood, let the grain run free')], target)
            content = target.read_text(encoding='utf-8')
        styles = [s.split(',') for s in content.splitlines() if s.startswith('Style:')]
        self.assertEqual([s[0] for s in styles], ['Style: Main'])
        self.assertGreaterEqual(int(styles[0][2]), 150)
        for forbidden in ('Panel', r'\move(', r'\t(', ',Next,'):
            self.assertNotIn(forbidden, content)
        events = [s for s in content.splitlines() if s.startswith('Dialogue:')]
        self.assertEqual(len(events), 2)
        self.assertTrue(all(r'\pos(960,544)' in e for e in events))

    def test_phrase_events_never_overlap_or_fade_during_sung_words(self):
        line = self.line('Shape the wood, let the grain run free', gap_after=2)
        for offset in (0, 0.23, -1.2):
            with self.subTest(offset=offset):
                phrases = karaoke.split_phrases([line])
                events = AssEventIntegrationTests().events([line], layout='phrases', offset=offset)
                self.assertEqual(len(events), len(phrases))
                for i, (event, phrase) in enumerate(zip(events, phrases)):
                    self.assertGreater(event['end'], event['start'])
                    if i:
                        self.assertLessEqual(events[i - 1]['end'], event['start'])
                    fade = re.search(r'\\fad\((\d+),(\d+)\)', event['text'])
                    self.assertIsNotNone(fade)
                    first = max(0, phrase['wstart'] + offset)
                    last = phrase['wend'] + offset
                    self.assertLessEqual(event['start'] + int(fade[1]) / 1000, first + .011)
                    self.assertGreaterEqual(event['end'] - int(fade[2]) / 1000, last - .011)
                    cursor = 0
                    word_times = []
                    for part in re.finditer(r'\{\\kf?(\d+)\}([^{}]*)', event['text']):
                        if part[2].strip():
                            word_times.append(round(event['start'] + cursor / 100, 2))
                        cursor += int(part[1])
                    self.assertEqual(word_times,
                        [karaoke.centiseconds(s / 1000 + offset) / 100 for s, _ in phrase['words']])

    def test_contiguous_phrases_keep_last_word_visible_until_handoff(self):
        line = self.line('Shape the wood, let the grain run free')
        phrases = karaoke.split_phrases([line])
        events = AssEventIntegrationTests().events([line], layout='phrases')
        boundary = phrases[0]['wend']
        self.assertEqual(events[0]['end'], boundary)
        self.assertEqual(events[1]['start'], boundary)
        self.assertIn(r'\fad(0,', events[1]['text'])

    def test_long_instrumental_does_not_keep_a_phrase_on_screen(self):
        lines = [{'text': 'hello', 'words': [(1000, 1400)], 'wstart': 1, 'wend': 1.4},
                 {'text': 'world', 'words': [(10000, 10400)], 'wstart': 10, 'wend': 10.4}]
        events = AssEventIntegrationTests().events(lines, layout='phrases')
        self.assertAlmostEqual(events[0]['end'], 2.1)
        self.assertAlmostEqual(events[1]['start'], 9.88)

    def test_completed_phrase_holds_before_soft_fade_without_changing_word_times(self):
        line = self.line('hello world')
        original = copy.deepcopy(line)
        event = AssEventIntegrationTests().events([line], layout='phrases')[0]
        fade = re.search(r'\\fad\((\d+),(\d+)\)', event['text'])
        self.assertEqual(int(fade[2]), 300)
        self.assertAlmostEqual(event['end'] - int(fade[2]) / 1000 - line['wend'], .4)
        self.assertEqual(line, original)

    def test_short_gap_shortens_hold_instead_of_overlapping_next_phrase(self):
        lines = [{'text': 'hello', 'words': [(1000, 1400)], 'wstart': 1, 'wend': 1.4},
                 {'text': 'world', 'words': [(1800, 2200)], 'wstart': 1.8, 'wend': 2.2}]
        events = AssEventIntegrationTests().events(lines, layout='phrases')
        self.assertAlmostEqual(events[0]['end'], 1.68)
        self.assertEqual(events[0]['end'], events[1]['start'])
        self.assertIn(r'\fad(120,140)', events[0]['text'])

    def test_invalid_word_intervals_are_not_silently_repaired(self):
        for intervals in ([(1000, 1500), (1400, 2000)],
                          [(1000, 900), (1500, 2000)]):
            line = self.line('hello world')
            line['words'] = intervals
            with self.subTest(intervals=intervals), self.assertRaises(ValueError):
                karaoke.split_phrases([line])


class InstrumentalTests(unittest.TestCase):
    def test_logo_waits_for_lyric_hold_but_intro_and_equalizer_gaps_stay_unchanged(self):
        lines = self.lines()
        original = karaoke.instrumental_windows(lines, song_duration=22)
        logo = karaoke.instrumental_windows(lines, song_duration=22, lyric_tail_seconds=.7)
        self.assertEqual(logo, [(0.4, 5.6), (7.8, 13.6), (15.8, 21.6)])
        self.assertEqual(original, [(0.4, 5.6), (7.4, 13.6), (15.4, 21.6)])

    @staticmethod
    def lines():
        return [{'text': 'hello', 'words': [(6000, 7000)], 'wstart': 6, 'wend': 7},
                {'text': 'world', 'words': [(14000, 15000)], 'wstart': 14, 'wend': 15}]

    def test_long_gaps_include_intro_and_outro_with_vocal_padding(self):
        self.assertEqual(karaoke.instrumental_windows(self.lines(), song_duration=22),
                         [(0.4, 5.6), (7.4, 13.6), (15.4, 21.6)])

    def test_short_gaps_and_unaligned_lines_do_not_claim_an_instrumental(self):
        lines = self.lines()
        lines[0]['wstart'] = 1
        lines[1]['wstart'] = 11
        self.assertEqual(karaoke.instrumental_windows(lines), [])
        lines = self.lines()
        lines.insert(1, {'text': 'unknown vocals', 'wstart': None})
        self.assertEqual(karaoke.instrumental_windows(lines), [(0.4, 5.6)])

    def test_offsets_are_applied_and_clipped_to_song_duration(self):
        self.assertEqual(karaoke.instrumental_windows(self.lines(), offset=-8, song_duration=9),
                         [(0.4, 5.6)])
        self.assertEqual(karaoke.instrumental_windows([], song_duration=9), [])

    def test_five_second_threshold_and_invalid_duration(self):
        lines = self.lines()
        lines[0]['wstart'] = 1
        lines[1]['wstart'] = 12
        self.assertEqual(karaoke.instrumental_windows(lines), [(7.4, 11.6)])
        lines[1]['wstart'] = 11.99
        self.assertEqual(karaoke.instrumental_windows(lines), [])
        for duration in (0, -1, float('nan')):
            with self.subTest(duration=duration), self.assertRaises(ValueError):
                karaoke.instrumental_windows(lines, song_duration=duration)

    def test_cli_can_disable_instrumental_icon(self):
        with mock.patch.object(karaoke, 'render') as render:
            karaoke.main(['inputs/woodworking', '--no-instrumental-icon'])
            self.assertFalse(render.call_args.kwargs['instrumental_icon'])

    def test_image_windows_never_overlap_lyrics_and_ass_has_no_old_symbol(self):
        lines = self.lines()
        for layout in ('phrases', 'lines'):
            with self.subTest(layout=layout):
                windows = karaoke.instrumental_windows(lines, song_duration=22,
                    lyric_tail_seconds=.7 if layout == 'phrases' else 0)
                events = AssEventIntegrationTests().events(lines, layout=layout)
                self.assertTrue(all(e['style'] != 'Instrumental' for e in events))
                for start, end in windows:
                    self.assertTrue(all(end <= e['start'] or start >= e['end'] for e in events))

    def test_supplied_logo_is_copied_exactly_and_missing_source_is_not_replaced(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / 'music logo.jpg'
            source.write_bytes(b'user supplied logo fixture')
            target = karaoke.package_music_logo(root / 'output', source)
            self.assertEqual(target.read_bytes(), source.read_bytes())
            target.write_bytes(b'preserve different existing output')
            with self.assertRaises(ValueError):
                karaoke.package_music_logo(root / 'output', source)
            with self.assertRaises(ValueError):
                karaoke.package_music_logo(root / 'other', root / 'missing.jpg')

    def test_render_command_uses_image_input_and_preserves_audio_mapping(self):
        command = karaoke.render_command(Path('clip.mp4'), Path('song.mp3'), Path('result.mp4'),
            start=10, duration=5, icon_file=Path('user assets/music logo.jpg'),
            instrumental_intervals=[(7.4, 13.6)], icon_height=180, icon_rgb=(255, 188, 230))
        inputs = [command[i+1] for i, value in enumerate(command) if value == '-i']
        self.assertEqual(inputs, ['clip.mp4', 'song.mp3', str(Path('user assets/music logo.jpg'))])
        self.assertIn('-filter_complex', command)
        mappings = [command[i+1] for i, value in enumerate(command) if value == '-map']
        self.assertEqual(mappings, ['[vout]', '1:a:0'])
        graph = command[command.index('-filter_complex')+1]
        self.assertIn('colorkey=', graph)
        self.assertIn('overlay=', graph)
        self.assertIn('alpha(X,Y)', graph)
        self.assertIn('(-2.600000)', graph)
        self.assertIn('[ink]lutrgb=r=255:g=188:b=230', graph)

    def test_invalid_logo_color_is_rejected_before_filtergraph(self):
        for rgb in ('cyan', (256, 0, 0), (0, 0), (True, 0, 0), (0.5, 0, 0)):
            with self.subTest(rgb=rgb), self.assertRaises(ValueError):
                karaoke.logo_overlay_graph([(0,10)], fill_rgb=rgb)

    def test_no_image_input_for_previews_outside_instrumental_windows(self):
        command = karaoke.render_command(Path('clip.mp4'), Path('song.mp3'), Path('result.mp4'),
            start=30, duration=5, icon_file=Path('music logo.jpg'),
            instrumental_intervals=[(7.4, 13.6)])
        self.assertNotIn('-filter_complex', command)
        self.assertEqual(command.count('-i'), 2)


class BackgroundLoopTests(unittest.TestCase):
    def test_default_cli_uses_seamless_with_hard_loop_escape_hatch(self):
        with mock.patch.object(karaoke, 'render') as render:
            karaoke.main(['inputs/woodworking'])
            self.assertEqual(render.call_args.kwargs['loop_mode'], 'seamless')
            karaoke.main(['inputs/woodworking','--loop-mode','hard'])
            self.assertEqual(render.call_args.kwargs['loop_mode'], 'hard')

    def test_loop_uses_frame_aligned_overlap_and_rejects_bad_metadata(self):
        config = karaoke.loop_config(481/24, '24/1')
        self.assertEqual(config['source_frames'],481)
        self.assertEqual(config['fade_frames'],24)
        self.assertEqual(config['cycle_frames'],457)
        short = karaoke.loop_config(.5,24)
        self.assertLessEqual(short['fade_frames'],short['source_frames']//4)
        for duration,rate in ((0,24),(float('nan'),24),(20,'0/0'),(20,0)):
            with self.subTest(duration=duration,rate=rate), self.assertRaises(ValueError):
                karaoke.loop_config(duration,rate)

    @unittest.skipUnless(shutil.which('ffmpeg'), 'FFmpeg needed')
    def test_loop_boundary_on_synthetic_motion_is_continuous(self):
        # A brightness ramp makes the original last-to-first hard cut measurable.
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory); source=root/'ramp.mkv'
            subprocess.run(['ffmpeg','-v','error','-f','lavfi','-i',
                'nullsrc=s=64x64:r=24:d=4,geq=lum=30+2*N:cb=128:cr=128',
                '-c:v','ffv1',str(source)],check=True,capture_output=True)
            config=karaoke.loop_config(4,24)
            result=karaoke.prepare_background_loop(source,root/'out',config)
            info=karaoke.probe(result)
            self.assertEqual(int(info['streams'][0]['nb_frames']),72)
            self.assertEqual(len(info['streams']),1, 'Background cycle must not carry audio')
            def frames(file):
                raw=subprocess.run(['ffmpeg','-v','error','-i',str(file),'-f','rawvideo',
                    '-pix_fmt','gray','pipe:1'],capture_output=True,check=True).stdout
                return [sum(raw[i:i+4096])/4096 for i in range(0,len(raw),4096)]
            original,looped=frames(source),frames(result)
            self.assertGreater(abs(original[-1]-original[0]),100)
            self.assertLess(abs(looped[-1]-looped[0]),8)
            self.assertGreater(max(looped)-min(looped),30, 'Must preserve motion, not freeze the video')
            with self.assertRaises(ValueError):
                karaoke.prepare_background_loop(source,root/'out',config)


class EqualizerTests(unittest.TestCase):
    def test_cli_defaults_to_selected_subtle_and_keeps_other_modes(self):
        with mock.patch.object(karaoke, 'render') as render:
            for mode in ('off', 'subtle', 'instrumental'):
                args = ['inputs/woodworking'] + (['--equalizer', mode] if mode != 'subtle' else [])
                karaoke.main(args)
                self.assertEqual(render.call_args.kwargs['equalizer'], mode)

    def test_off_preserves_command_and_on_uses_mp3_with_or_without_logo(self):
        args = (Path('clip.mp4'), Path('song.mp3'), Path('result.mp4'))
        baseline = karaoke.render_command(*args, duration=5)
        self.assertEqual(baseline, karaoke.render_command(*args, duration=5, equalizer='off'))
        for logo in (None, Path('logo.jpg')):
            command = karaoke.render_command(*args, duration=5, start=10, icon_file=logo,
                instrumental_intervals=[(7, 16)], equalizer='subtle',
                equalizer_intervals=[(7, 16)])
            graph = command[command.index('-filter_complex')+1]
            self.assertIn('[1:a:0]asplit=2[audio_out][viz_source]', graph)
            self.assertIn('asetpts=PTS-STARTPTS', graph)
            self.assertIn('showfreqs=', graph)
            self.assertEqual([command[i+1] for i,v in enumerate(command) if v == '-map'],
                             ['[equalized]', '[audio_out]'])
            self.assertEqual(command.count('-i'), 3 if logo else 2)

    def test_invalid_mode_is_rejected(self):
        with self.assertRaises(ValueError):
            karaoke.render_command(Path('v'), Path('a'), Path('o'), duration=5, equalizer='random')

    def test_subtitles_only_keeps_windows_without_logo_and_does_not_render(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            folder = root / 'song'
            folder.mkdir()
            for filename in ('v.mp4', 'a.mp3', 'lyrics.md'):
                (folder / filename).write_bytes(b'fixture')
            words = [{'w':'hello', 'start':6, 'end':7}, {'w':'world', 'start':14, 'end':15}]
            with mock.patch.object(karaoke, 'probe', return_value={
                    'streams':[{'codec_type':'video', 'width':640, 'height':360, 'avg_frame_rate':'24/1'}]}), \
                 mock.patch.object(karaoke, 'ffprobe_duration', return_value=22), \
                 mock.patch.object(karaoke, 'parse_lyrics', return_value=[('verse','hello'),('verse','world')]), \
                 mock.patch.object(karaoke, 'whisper_words', return_value=words), \
                 mock.patch.object(karaoke, 'render_command') as command:
                for layout in ('lines', 'phrases'):
                    target = root/layout
                    ass = karaoke.render(folder, target, subtitles_only=True, palette='cyan',
                        layout=layout, instrumental_icon=False, equalizer='instrumental', offset=.2)
                    self.assertTrue(ass.exists())
                    report = json.loads((target/'song.alignment.json').read_text())
                    eq = report['style']['equalizer']
                    self.assertEqual(report['instrumental_windows'], [])
                    self.assertEqual(eq['windows'], [[.4,5.8],[7.6,13.8],[15.6,21.6]])
                    self.assertFalse(eq['rendered'])
                    self.assertEqual(eq['status'], 'subtitles_only')
                    self.assertFalse((target/'assets').exists())
                    with self.assertRaises(ValueError):
                        karaoke.render(folder, target, subtitles_only=True, palette='cyan', equalizer='subtle')
                command.assert_not_called()

    @staticmethod
    def spectrum_frames(audio, mode='subtle', windows=(), start=0):
        config = karaoke.equalizer_config(mode, 640, 360)
        graph = '[0:v]null[base];' + karaoke.equalizer_overlay_graph(config, windows, start=start)
        graph += ';[audio_out]anullsink'
        result = subprocess.run(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i',
            'color=0x208040:s=640x360:r=24:d=1', '-f', 'lavfi', '-i', audio,
            '-filter_complex', graph, '-map', '[equalized]', '-t', '1',
            '-f', 'rawvideo', '-pix_fmt', 'rgb24', 'pipe:1'], check=True, capture_output=True)
        size = 640*360*3
        return [result.stdout[i:i+size] for i in range(0, len(result.stdout), size)]

    @staticmethod
    def changed_columns(frame):
        return [sum(abs(frame[(y*640+x)*3]-frame[0]) > 8 for y in range(328, 346))
                for x in range(640)]

    @unittest.skipUnless(shutil.which('ffmpeg'), 'FFmpeg needed')
    def test_spectrum_responds_to_frequency_and_silence_without_a_panel(self):
        peaks = []
        for frequency in (0, 220, 4000):
            audio = (f'sine=frequency={frequency}:sample_rate=44100:duration=1' if frequency
                     else 'anullsrc=r=44100:cl=mono:d=1')
            frames = self.spectrum_frames(audio)
            self.assertEqual(len(frames), 24)
            frame = frames[12]
            columns = self.changed_columns(frame)
            if frequency:
                self.assertGreater(max(columns), 8)
                peaks.append(columns.index(max(columns)))
                # All changes are confined to the short bottom overlay, not a black canvas.
                bg = frame[:3]
                self.assertEqual(frame[:328*640*3], bg*(328*640))
                self.assertEqual(frame[346*640*3:], bg*(14*640))
                self.assertLessEqual(sum(c > 0 for c in columns), 128)
            else:
                self.assertEqual(frame, frame[:3]*(640*360))
        self.assertGreater(peaks[1]-peaks[0], 100)

    @unittest.skipUnless(shutil.which('ffmpeg'), 'FFmpeg needed')
    def test_instrumental_visibility_uses_song_clock_and_fades(self):
        audio = 'sine=frequency=440:sample_rate=44100:duration=1'
        middle = self.spectrum_frames(audio, 'instrumental', [(7, 16)], start=10)
        hidden = self.spectrum_frames(audio, 'instrumental', [(7, 16)], start=20)
        fade = self.spectrum_frames(audio, 'instrumental', [(10, 16)], start=10)
        self.assertGreater(sum(self.changed_columns(middle[0])), 10)
        self.assertEqual(hidden[12], hidden[12][:3]*(640*360))
        self.assertEqual(fade[0], fade[0][:3]*(640*360))
        self.assertEqual(fade[18], middle[18])

    @unittest.skipUnless(shutil.which('ffmpeg'), 'FFmpeg needed')
    def test_tone_onset_stays_close_to_audio_time(self):
        audio = r"aevalsrc=if(between(t\,0.5\,0.75)\,0.125*sin(2*PI*440*t)\,0):s=44100:d=1"
        frames = self.spectrum_frames(audio)
        active = [i for i, frame in enumerate(frames) if sum(self.changed_columns(frame)) > 10]
        self.assertTrue(active)
        # FFT 2048/44100 and frame pacing allow at most two frames of onset difference.
        self.assertLessEqual(abs(active[0]/24-.5), 2/24)
        self.assertLessEqual(active[-1]/24, .75+4/24)


class LibassRenderTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which('ffmpeg'), 'FFmpeg needed')
    def test_finished_word_remains_opaque_then_fades_and_disappears(self):
        line = {'text': 'Hello', 'wstart': 1, 'wend': 1.4, 'words': [(1000, 1400)]}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            karaoke.build_ass([line], path/'lyrics.ass', width=640, height=360, highlight='&H00FFF573&')
            karaoke.package_font(path, karaoke.DEFAULT_FONT)
            brightness = []
            for timestamp in (1.45, 1.7, 1.95, 2.15):
                frame = subprocess.run(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i',
                    'color=black:s=640x360:r=100', '-vf',
                    f'setpts=PTS+{timestamp}/TB,ass=lyrics.ass:fontsdir=fonts',
                    '-frames:v', '1', '-f', 'rawvideo', '-pix_fmt', 'rgb24', 'pipe:1'],
                    cwd=path, check=True, capture_output=True).stdout
                brightness.append(sum(frame))
            self.assertGreater(brightness[0], 10000)
            self.assertEqual(brightness[0], brightness[1], 'Hold must not start fading early')
            self.assertGreater(brightness[2], brightness[0] * .3)
            self.assertLess(brightness[2], brightness[0] * .7)
            self.assertEqual(brightness[3], 0)

    @unittest.skipUnless(shutil.which('ffmpeg'), 'FFmpeg needed')
    def test_first_word_stays_white_during_lead_in_including_preview_seek(self):
        for onset, offset, preview_start in ((1.2, 0, 0), (1.2, .25, 0), (10.2, 0, 10)):
            with self.subTest(onset=onset, offset=offset, preview_start=preview_start), \
                    tempfile.TemporaryDirectory() as directory:
                path = Path(directory)
                line = {'text': 'Hello', 'wstart': onset, 'wend': onset + .4,
                        'words': [(onset * 1000, (onset + .4) * 1000)]}
                karaoke.build_ass([line], path / 'lyrics.ass', width=640, height=360,
                                  offset=offset, highlight='&H00FFF573&')
                karaoke.package_font(path, karaoke.DEFAULT_FONT)
                # Preserve the production song-clock/preview-clock transforms;
                # crop only after libass to keep the raw frame fixture small.
                filters = (f'setpts=PTS-STARTPTS+{preview_start}/TB,'
                           'ass=lyrics.ass:fontsdir=fonts,setpts=PTS-STARTPTS,'
                           'crop=200:80:220:140')
                raw = subprocess.run([
                    'ffmpeg', '-hide_banner', '-loglevel', 'error', '-f', 'lavfi',
                    '-i', 'color=c=black:s=640x360:r=24:d=2', '-vf', filters,
                    '-f', 'rawvideo', '-pix_fmt', 'rgb24', 'pipe:1'],
                    cwd=path, check=True, capture_output=True).stdout
                frame_size = 200 * 80 * 3
                self.assertEqual(len(raw), 48 * frame_size)
                cyan, white = [], []
                for start in range(0, len(raw), frame_size):
                    frame = raw[start:start + frame_size]
                    pixels = list(zip(frame[0::3], frame[1::3], frame[2::3]))
                    cyan.append(sum(g > 30 and b > 30 and b > r * 1.4
                                    for r, g, b in pixels))
                    white.append(sum(min(pixel) > 30 and max(pixel) - min(pixel) < 10
                                     for pixel in pixels))
                expected_onset = onset + offset - preview_start
                lead_in = [i for i in range(48) if expected_onset - .12 < i / 24 < expected_onset]
                self.assertGreater(max(white[i] for i in lead_in), 100,
                                   'The pending first word should be visible in white')
                self.assertFalse(any(count for i, count in enumerate(cyan) if i / 24 < expected_onset),
                                 'First-word highlight advanced before its assigned onset')
                first_highlight = next(i for i, count in enumerate(cyan) if count)
                self.assertLessEqual(first_highlight / 24 - expected_onset, 2 / 24,
                                     'First-word highlight did not start near its assigned onset')

    @unittest.skipUnless(shutil.which('ffmpeg'), 'FFmpeg needed')
    def test_logo_silhouette_has_antialiased_edges_without_blurring_core(self):
        graph = karaoke.logo_overlay_graph([(0,20)], start=5, icon_height=196)
        # Inspect the actual compositor mask before the video overlay/codec.
        graph = '[0:v]nullsink;' + graph.rsplit(';',1)[0] + ';[logo]alphaextract[mask]'
        alpha = subprocess.run(['ffmpeg','-v','error','-f','lavfi','-i','color=s=640x360',
            '-f','lavfi','-i','anullsrc','-loop','1','-i',str(karaoke.DEFAULT_LOGO_FILE),
            '-filter_complex',graph,'-map','[mask]','-frames:v','1','-f','rawvideo',
            '-pix_fmt','gray','pipe:1'],capture_output=True,check=True).stdout
        self.assertEqual(len(alpha),104*196)
        self.assertGreater(sum(32<=v<=223 for v in alpha),150,
                           'Silhouette boundary needs fractional pixel coverage, not hard keying at final size')
        self.assertGreater(sum(v==255 for v in alpha),4000)
        self.assertGreater(sum(v==0 for v in alpha),8000)

    @unittest.skipUnless(shutil.which('ffmpeg'), 'FFmpeg needed')
    def test_logo_matches_selected_accent_with_black_outline(self):
        for rgb in ((115,245,255), (255,188,230)):
            graph = '[0:v]null[base];' + karaoke.logo_overlay_graph(
                [(7.4,13.6)], start=10, icon_height=180, fill_rgb=rgb)
            frame = subprocess.run(['ffmpeg','-v','error','-f','lavfi','-i',
                'color=0x208040:s=640x360:r=24','-f','lavfi','-i','anullsrc',
                '-loop','1','-i',str(karaoke.DEFAULT_LOGO_FILE),'-filter_complex',graph,
                '-map','[vout]','-frames:v','1','-f','rawvideo','-pix_fmt','rgb24','pipe:1'],
                check=True,capture_output=True).stdout
            pixels = list(zip(frame[0::3],frame[1::3],frame[2::3]))
            colored = sum(max(abs(p[i]-rgb[i]) for i in range(3))<8 for p in pixels)
            black = sum(max(p)<15 for p in pixels)
            self.assertGreater(colored,1000)
            self.assertGreater(black,100)
            self.assertEqual(sum(min(p)>230 for p in pixels),0, 'No old white contour/checkerboard')

    @unittest.skipUnless(shutil.which('ffmpeg'), 'FFmpeg dibutuhkan untuk verifikasi libass')
    def test_user_logo_composites_without_checkerboard_and_disappears_before_lyrics(self):
        for timestamp, visible in ((7.4, False), (7.55, True), (10, True), (13.8, False)):
            with self.subTest(timestamp=timestamp):
                graph = '[0:v]null[base];' + karaoke.logo_overlay_graph([(7.4, 13.6)], start=timestamp, icon_height=120)
                frame = subprocess.run([
                    'ffmpeg', '-hide_banner', '-loglevel', 'error', '-f', 'lavfi',
                    '-i', 'color=c=0x208040:s=640x360:r=25', '-f', 'lavfi', '-i', 'anullsrc',
                    '-loop', '1', '-i', str(karaoke.DEFAULT_LOGO_FILE), '-filter_complex', graph,
                    '-map', '[vout]', '-frames:v', '1', '-f', 'rawvideo', '-pix_fmt', 'rgb24', 'pipe:1'],
                    check=True, capture_output=True).stdout
                self.assertEqual(len(frame), 640*360*3)
                background = tuple(frame[:3])
                changed = [(i//3%640, i//3//640) for i in range(0,len(frame),3)
                           if max(abs(frame[i+c]-background[c]) for c in range(3)) > 8]
                if visible:
                    self.assertGreater(len(changed), 300)
                    self.assertTrue(all(275 < x < 365 and 115 < y < 245 for x,y in changed))
                    if timestamp == 7.55:
                        # Halfway through the 300 ms fade: black outline must still
                        # reveal roughly half the green video beneath it.
                        darkest_green = min(frame[(y*640+x)*3+1] for x,y in changed)
                        self.assertGreater(darkest_green, 40)
                        self.assertLess(darkest_green, 85)
                    # The JPG corners/checkerboard must reveal the green video.
                    for x,y in ((291,123),(348,123),(291,237),(348,237)):
                        pixel=frame[(y*640+x)*3:(y*640+x)*3+3]
                        self.assertLess(max(abs(pixel[c]-background[c]) for c in range(3)), 8)
                else:
                    self.assertEqual(changed, [])

    @unittest.skipUnless(shutil.which('ffmpeg'), 'FFmpeg dibutuhkan untuk verifikasi libass')
    def test_real_renderer_waits_through_gap_before_highlighting_second_word(self):
        line = KaraokeTagTests.line(words=[(1200, 1500), (4000, 4500)])
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            karaoke.build_ass([line], path / 'lyrics.ass', width=640, height=360, show_next=False)
            karaoke.package_font(path, karaoke.DEFAULT_FONT)
            counts = []
            for timestamp in (2.0, 3.0, 4.25):
                frame = subprocess.run([
                    'ffmpeg', '-hide_banner', '-loglevel', 'error', '-f', 'lavfi',
                    '-i', 'color=c=black:s=640x360:r=25', '-vf',
                    f'setpts=PTS+{timestamp}/TB,ass=lyrics.ass:fontsdir=fonts', '-frames:v', '1',
                    '-f', 'rawvideo', '-pix_fmt', 'rgb24', 'pipe:1'],
                    cwd=path, check=True, capture_output=True).stdout
                self.assertEqual(len(frame), 640 * 360 * 3)
                # Second word occupies the right half. Gold means highlighted.
                gold = 0
                for y in range(140, 220):
                    for x in range(320, 500):
                        pos = (y * 640 + x) * 3
                        r, g, b = frame[pos:pos + 3]
                        gold += r > 170 and g > 120 and b < 150 and r > b * 1.3
                counts.append(gold)
            self.assertEqual(counts[:2], [0, 0], 'Kata kedua disorot terlalu awal saat jeda')
            self.assertGreater(counts[2], 5, 'Kata kedua tidak disorot pada waktunya')


class AcousticTimingTests(unittest.TestCase):
    def test_full_alignment_batches_keep_lines_whole_and_skip_preserved_intro(self):
        from musicmerger.acoustic import alignment_batches
        lines = [{'wstart': i * 4.0 + .2, 'wend': i * 4.0 + 3.5} for i in range(20)]
        batches = alignment_batches(lines, 85, preserved=4)
        self.assertEqual([i for batch in batches for i in batch['indices']], list(range(4, 20)))
        self.assertTrue(all(0 < b['end'] - b['start'] <= 30 for b in batches))
        for batch in batches:
            self.assertLessEqual(batch['start'], lines[batch['indices'][0]]['wstart'])
            self.assertGreaterEqual(batch['end'], lines[batch['indices'][-1]]['wend'])
        self.assertTrue(all(a['end'] <= b['start'] for a, b in zip(batches, batches[1:])))

    def fixture(self):
        line = dict(KaraokeTagTests.line(), issues=[], needs_review=False)
        payload = {'schema': 1, 'method': 'wav2vec2_ctc_forced_alignment',
                   'audio_sha256': 'audio', 'lyrics_sha256': 'lyrics', 'coverage': [0, 6],
                   'lines': [{'index': 0, 'words_text': ['Hello', 'world'],
                              'words': [[1300, 1800], [4100, 4600]]}]}
        args = dict(audio_sha256='audio', lyrics_sha256='lyrics', start=0, duration=6, song_duration=10)
        return [line], payload, args

    def test_override_changes_per_word_without_mutating_original(self):
        from musicmerger.acoustic import apply_timing_override
        lines, payload, args = self.fixture()
        original = copy.deepcopy(lines)
        result = apply_timing_override(lines, payload, **args)
        self.assertEqual(result[0]['words'], [[1300, 1800], [4100, 4600]])
        self.assertEqual(result[0]['wstart'], 1.3)
        self.assertTrue(result[0]['needs_review'])
        self.assertEqual(lines, original)

    def test_override_rejects_mismatches_and_incomplete_coverage(self):
        from musicmerger.acoustic import apply_timing_override
        for change in ('audio', 'lyrics', 'window', 'text', 'overlap', 'nan', 'duplicate', 'missing'):
            lines, payload, args = self.fixture()
            if change in ('audio', 'lyrics'): payload[change + '_sha256'] = 'wrong'
            elif change == 'window': args['duration'] = 7
            elif change == 'text': payload['lines'][0]['words_text'][0] = 'Wrong'
            elif change == 'overlap': payload['lines'][0]['words'][1] = [1700, 2000]
            elif change == 'nan': payload['lines'][0]['words'][0][0] = float('nan')
            elif change == 'duplicate': payload['lines'].append(copy.deepcopy(payload['lines'][0]))
            else: payload['lines'] = []
            with self.subTest(change=change), self.assertRaises(ValueError):
                apply_timing_override(lines, payload, **args)

    def test_cli_forwards_acoustic_file_without_changing_offset(self):
        with mock.patch.object(karaoke, 'render') as render:
            karaoke.main(['inputs/woodworking', '--timing-file', 'corrected.json', '--duration', '6'])
        self.assertEqual(render.call_args.kwargs['timing_file'], Path('corrected.json'))
        self.assertEqual(render.call_args.kwargs['offset'], 0)

    def test_ctc_leading_blank_and_word_gap_do_not_belong_to_first_word(self):
        import numpy as np
        from musicmerger.acoustic import ctc_word_spans
        # blank=0, A=1, B=2, separator=3; 20 ms frames.
        emissions = np.full((12, 4), -20.0)
        emissions[np.arange(12), [0, 0, 0, 1, 1, 0, 3, 0, 0, 2, 2, 0]] = 0
        result = ctc_word_spans(emissions, ['A', 'B'], {'A': 1, 'B': 2, '|': 3}, 0)
        self.assertEqual([(w['start_frame'], w['end_frame']) for w in result], [(3, 5), (9, 11)])

    def test_ctc_repeated_letters_require_separate_emissions(self):
        import numpy as np
        from musicmerger.acoustic import ctc_word_spans
        emissions = np.full((8, 4), -20.0)
        emissions[np.arange(8), [0, 1, 0, 1, 3, 2, 0, 0]] = 0
        result = ctc_word_spans(emissions, ['AA', 'B'], {'A': 1, 'B': 2, '|': 3}, 0)
        self.assertEqual(result[0]['start_frame'], 1)
        self.assertEqual(result[0]['end_frame'], 4)

    def test_ctc_rejects_unsupported_text_and_impossible_path(self):
        import numpy as np
        from musicmerger.acoustic import ctc_word_spans
        for words in (['Z'], ['AAAA']):
            with self.subTest(words=words), self.assertRaises(ValueError):
                ctc_word_spans(np.zeros((2, 3)), words, {'A': 1, '|': 2}, 0)


class EncoderTests(unittest.TestCase):
    def test_probe_timeout_is_unavailable_not_success(self):
        from musicmerger import encoder as enc
        with mock.patch.object(enc.subprocess, 'run', side_effect=subprocess.TimeoutExpired('ffmpeg', 20)):
            self.assertFalse(enc.probe_encoder('h264_amf', 640, 360, '24')[0])

    def test_explicit_gpu_does_not_silently_fallback_and_cpu_failure_stops(self):
        from musicmerger import encoder as enc
        for requested, selected, count in [('h264_amf', 'h264_amf', 1), ('auto', 'h264_amf', 2)]:
            with self.subTest(requested=requested), tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / 'partial.mp4'
                config = dict(requested=requested, selected=selected, attempts=[])
                with mock.patch.object(enc.subprocess, 'run', return_value=types.SimpleNamespace(returncode=1)) as run:
                    with self.assertRaises(RuntimeError):
                        enc.run_encode(lambda name: [name], path, Path(tmp)/'log.txt', config)
                self.assertEqual(run.call_count, count)

    def test_existing_partial_is_not_touched(self):
        from musicmerger import encoder as enc
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'partial.mp4'
            path.write_bytes(b'keep')
            with mock.patch.object(enc.subprocess, 'run') as run, self.assertRaises(ValueError):
                enc.run_encode(lambda name: [name], path, Path(tmp)/'log', dict(selected='libx264'))
            run.assert_not_called()
            self.assertEqual(path.read_bytes(), b'keep')

    def test_loop_encoder_does_not_change_crossfade_graph(self):
        from musicmerger.loop import loop_command, loop_config
        config = loop_config(20, '24')
        cpu = loop_command('source', 'target', config)
        gpu = loop_command('source', 'target', config, 'h264_amf')
        self.assertEqual(cpu[cpu.index('-filter_complex') + 1], gpu[gpu.index('-filter_complex') + 1])
        self.assertEqual(gpu[gpu.index('-qp_i') + 1], '16')

    def test_probe_dimensions_match_ffmpeg_even_scale(self):
        from musicmerger.encoder import frame_size
        self.assertEqual(frame_size(1920, 1088, 1280), (1280, 726))
        self.assertEqual(frame_size(1920, 1088, None), (1920, 1088))

    def test_auto_tries_real_hardware_then_uses_cpu(self):
        from musicmerger import encoder as enc
        with mock.patch.object(enc, 'probe_encoder', side_effect=[(False, 'driver'), (True, '')]) as probe:
            result = enc.select_encoder('auto', 1920, 1088, '24')
        self.assertEqual(result['selected'], 'h264_amf')
        self.assertEqual(probe.call_args_list[1].args, ('h264_amf', 1920, 1088, '24'))
        with mock.patch.object(enc, 'probe_encoder', return_value=(False, 'no device')):
            self.assertEqual(enc.select_encoder('auto', 640, 360, '24')['selected'], 'libx264')

    def test_cpu_is_explicit_and_forced_gpu_failure_is_clear(self):
        from musicmerger import encoder as enc
        with mock.patch.object(enc, 'probe_encoder', return_value=(False, 'missing driver')) as probe:
            self.assertEqual(enc.select_encoder('cpu', 640, 360, '24')['selected'], 'libx264')
            probe.assert_not_called()
            with self.assertRaises(RuntimeError):
                enc.select_encoder('h264_amf', 640, 360, '24')

    def test_amf_and_cpu_commands_preserve_filter_and_audio(self):
        cpu = karaoke.render_command(Path('v.mp4'), Path('a.mp3'), Path('out.mp4'), duration=2, encoder='libx264')
        gpu = karaoke.render_command(Path('v.mp4'), Path('a.mp3'), Path('out.mp4'), duration=2, encoder='h264_amf')
        self.assertIn('h264_amf', gpu)
        self.assertNotIn('-crf', gpu)
        self.assertEqual(cpu[cpu.index('-vf') + 1], gpu[gpu.index('-vf') + 1])
        self.assertEqual(cpu[cpu.index('-c:a'):], gpu[gpu.index('-c:a'):])

    def test_runtime_gpu_failure_preserves_partial_then_retries_cpu_once(self):
        from musicmerger import encoder as enc
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'out.partial.mp4'
            config = {'requested': 'auto', 'selected': 'h264_amf', 'attempts': []}
            def run(command, **kwargs):
                path.write_bytes(b'gpu failed' if command[0] == 'h264_amf' else b'cpu success')
                return types.SimpleNamespace(returncode=1 if command[0] == 'h264_amf' else 0)
            with mock.patch.object(enc.subprocess, 'run', side_effect=run) as mocked:
                enc.run_encode(lambda name: [name], path, Path(tmp)/'encode.log', config)
            self.assertEqual(mocked.call_count, 2)
            self.assertEqual(config['selected'], 'libx264')
            self.assertEqual(path.read_bytes(), b'cpu success')
            self.assertEqual(path.with_name('out.partial.failed-h264_amf.mp4').read_bytes(), b'gpu failed')

    def test_cli_defaults_to_auto_and_allows_cpu(self):
        with mock.patch.object(karaoke, 'render') as render:
            karaoke.main(['inputs/woodworking'])
            self.assertEqual(render.call_args.kwargs['encoder'], 'auto')
            karaoke.main(['inputs/woodworking', '--encoder', 'cpu'])
            self.assertEqual(render.call_args.kwargs['encoder'], 'cpu')


if __name__ == "__main__":
    unittest.main()
