"""Thumbnail contracts use generated test fonts; no user assets or models required."""
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from PIL import Image, ImageDraw, ImageFont
from fontTools.fontBuilder import FontBuilder
from fontTools.pens.ttGlyphPen import TTGlyphPen

from musicmerger import thumbnail as thumb
from musicmerger import publication, workflow, cli


def test_font(path, chars):
    builder = FontBuilder(1000, isTTF=True)
    glyphs = {'.notdef': TTGlyphPen(None).glyph()}
    cmap = {}
    for char in chars:
        name = f'u{ord(char):04x}'
        pen = TTGlyphPen(None)
        if char != ' ':
            pen.moveTo((40, 0)); pen.lineTo((510, 0))
            pen.lineTo((510, 700)); pen.lineTo((40, 700)); pen.closePath()
        glyphs[name] = pen.glyph()
        cmap[ord(char)] = name
    builder.setupGlyphOrder(list(glyphs))
    builder.setupCharacterMap(cmap)
    builder.setupGlyf(glyphs)
    builder.setupHorizontalMetrics({g: (550, 0) for g in glyphs})
    builder.setupHorizontalHeader(ascent=800, descent=-200)
    builder.setupNameTable({'familyName': 'TestFont', 'styleName': 'Regular'})
    builder.setupOS2(sTypoAscender=800, sTypoDescender=-200, usWinAscent=800, usWinDescent=200)
    builder.setupPost(); builder.setupMaxp(); builder.save(path)


class ThumbnailTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.font = self.root / 'test.ttf'
        test_font(self.font, ''.join(chr(i) for i in range(32, 127)))

    def metadata(self):
        md = self.root / 'lyrics.md'
        md.write_text('Lyrics\n[Verse]\nShape the wood\n\nStyle Prompt\nWarm roots rock, no EDM.', encoding='utf8')
        data = dict(schema_version=1, thumbnail_title='Shape the Wood', youtube_title='Shape the Wood | Roots Rock',
                    tags=['roots rock'], source_md=md.name,
                    source_md_sha256=hashlib.sha256(md.read_bytes()).hexdigest())
        (self.root / 'youtube-metadata.json').write_text(json.dumps(data), encoding='utf8')
        return md, data

    def test_metadata_requires_matching_md_and_never_invents_a_title(self):
        md, data = self.metadata()
        self.assertEqual(thumb.read_metadata(self.root, md)['thumbnail_title'], 'Shape the Wood')
        md.write_text('Changed lyrics')
        with self.assertRaisesRegex(ValueError, 'MD'):
            thumb.read_metadata(self.root, md)
        (self.root / 'youtube-metadata.json').unlink()
        with self.assertRaisesRegex(ValueError, 'youtube-metadata'):
            thumb.read_metadata(self.root, md)

    def test_metadata_rejects_empty_title_and_invalid_tags(self):
        md, data = self.metadata()
        for changed in ({'thumbnail_title': ''}, {'tags': 'not a list'}, {'thumbnail_title': 'X' * 201}):
            with self.subTest(changed=changed):
                (self.root / 'youtube-metadata.json').write_text(json.dumps(data | changed))
                with self.assertRaises(ValueError):
                    thumb.read_metadata(self.root, md)

    def test_style_uses_prompt_not_lyrics_and_ignores_negated_genres(self):
        md, data = self.metadata()
        md.write_text('Lyrics\n[Verse]\nDance EDM forever\n\nStyle Prompt\nWarm acoustic folk, no EDM, no heavy metal.', encoding='utf8')
        context = thumb.style_context(md, [])
        self.assertIn('folk', context)
        self.assertNotIn('edm', context)
        self.assertNotIn('metal', context)
        self.assertFalse(thumb.is_energetic(context))
        self.assertTrue(thumb.is_energetic('energetic electronic dance, 140 BPM'))

    def test_font_selection_checks_real_glyphs_and_prefers_matching_genre(self):
        limited = self.root / 'limited.ttf'; test_font(limited, 'Shape theWood')
        entries = [dict(file=limited.name, family='Limited', genre_hints=['rock'], mood_hints=[]),
                   dict(file=self.font.name, family='Complete', genre_hints=['jazz'], mood_hints=[])]
        (self.root / 'font-catalog.json').write_text(json.dumps({'schema_version': 1, 'fonts': entries}))
        selected = thumb.select_font(self.root, 'Shape the Wood 2', 'rock')
        self.assertEqual(selected['family'], 'Complete')
        self.assertEqual(thumb.select_font(self.root, 'Shape the Wood', 'rock')['family'], 'Limited')
        with self.assertRaisesRegex(ValueError, 'karakter'):
            thumb.select_font(self.root, '\u4f60\u597d', 'rock')

    def test_catalog_rejects_escape_paths(self):
        (self.root / 'font-catalog.json').write_text(json.dumps({'schema_version': 1, 'fonts': [
            dict(file='../outside.ttf', family='Unsafe', genre_hints=[], mood_hints=[])]}))
        with self.assertRaises(ValueError):
            thumb.select_font(self.root, 'Hi', '')

    def test_layout_centers_actual_glyph_bounds_and_fits_long_titles(self):
        for title in ['Shape the Wood', 'Clockmaker', 'A Long Journey Through the Beautiful Mountains at Midnight']:
            with self.subTest(title=title):
                rows = thumb.layout_title(title, self.font)
                self.assertEqual(' '.join(row['text'] for row in rows), title)
                for row in rows:
                    left, top, right, bottom = row['box']
                    self.assertAlmostEqual((left + right) / 2, 640, delta=1)
                    self.assertTrue(64 <= left < right <= 1216)
                    self.assertTrue(200 <= top < bottom <= 590)
                for a, b in zip(rows, rows[1:]):
                    self.assertLess(a['box'][3], b['box'][1])
        rows = thumb.layout_title('Shape the Wood', self.font)
        self.assertEqual([r['text'] for r in rows], ['Shape the', 'Wood'])
        self.assertGreater(rows[-1]['size'], rows[0]['size'])

    def test_palette_is_calm_and_contrast_survives_bright_background(self):
        warm = Image.new('RGB', (1280, 720), (160, 100, 50))
        calm = thumb.choose_colors(warm, 'relaxed acoustic folk')
        energetic = thumb.choose_colors(warm, 'energetic rock')
        self.assertEqual(calm['mood'], 'calm')
        self.assertNotEqual(calm['accent'], energetic['accent'])
        rows = thumb.layout_title('Shape the Wood', self.font)
        white = Image.new('RGB', (1280, 720), 'white')
        shaded, report = thumb.contrast_backdrop(white, rows, calm)
        self.assertGreaterEqual(min(report['ratios']), 4.5)
        self.assertLess(shaded.getpixel((640, 360))[0], 255)

    def test_palette_uses_dark_or_light_text_without_unnecessary_dimming(self):
        rows = thumb.layout_title('Shape the Wood', self.font)
        for rgb, polarity in [((238, 241, 235), 'dark'), ((20, 27, 24), 'light')]:
            with self.subTest(polarity=polarity):
                frame = Image.new('RGB', (1280, 720), rgb)
                colors = thumb.choose_colors(frame, 'calm folk', rows=rows)
                self.assertEqual(colors['polarity'], polarity)
                canvas, report = thumb.contrast_backdrop(frame, rows, colors)
                self.assertEqual(canvas.tobytes(), frame.tobytes())
                self.assertGreaterEqual(min(report['ratios']), 4.5)

    def test_dominant_colors_preserve_secondary_hue_and_are_deterministic(self):
        colors = []
        for rgb in ((42, 145, 122), (180, 72, 94)):
            frame = Image.new('RGB', (1280, 720), (155, 155, 155))
            frame.paste(rgb, (0, 0, 320, 720))
            result = thumb.choose_colors(frame, 'calm')
            self.assertEqual(result, thumb.choose_colors(frame, 'calm'))
            self.assertTrue(any(c['kind'] == 'chromatic' for c in result['dominant_colors']))
            colors.append(result['accent'])
        self.assertNotEqual(*colors)

    def test_local_contrast_correction_preserves_pixels_outside_text_region(self):
        frame = Image.new('RGB', (1280, 720), (250, 250, 250))
        rows = [dict(box=(440, 300, 840, 420))]
        colors = dict(main=(245, 245, 245), accent=(235, 240, 230), polarity='light')
        canvas, report = thumb.contrast_backdrop(frame, rows, colors)
        self.assertGreaterEqual(min(report['ratios']), 4.5)
        self.assertGreater(report['strength'], 0)
        self.assertEqual(canvas.getpixel((0, 0)), frame.getpixel((0, 0)))
        self.assertLess(canvas.getpixel((640, 360))[0], 250)

    def test_neutral_frames_have_a_safe_palette_with_contrast_in_both_directions(self):
        rows = [dict(box=(240, 250, 1040, 510))]
        for value in (0, 110, 180, 255):
            with self.subTest(value=value):
                frame = Image.new('RGB', (1280, 720), (value, value, value))
                colors = thumb.choose_colors(frame, 'energetic', rows=rows)
                self.assertEqual(colors['name'], 'neutral')
                _, report = thumb.contrast_backdrop(frame, rows, colors)
                self.assertGreaterEqual(min(report['ratios']), 4.5)

    def test_glyph_contrast_support_does_not_darken_spaces_between_letters(self):
        frame = Image.new('RGB', (1280, 720), 'white')
        ink = Image.new('L', frame.size)
        pen = ImageDraw.Draw(ink)
        pen.rectangle((400, 300, 440, 420), fill=255)
        pen.rectangle((650, 300, 690, 420), fill=255)
        rows = [dict(box=(400, 300, 691, 421))]
        colors = dict(main=(245, 245, 245), accent=(210, 230, 200), polarity='light')
        canvas, report = thumb.contrast_backdrop(frame, rows, colors, text_mask=ink)
        self.assertEqual(canvas.getpixel((550, 360)), (255, 255, 255))
        self.assertEqual(canvas.getpixel((20, 360)), (255, 255, 255))
        self.assertLess(canvas.getpixel((420, 360))[0], 255)
        self.assertGreaterEqual(min(report['ratios']), 4.5)
        self.assertEqual(report['coverage'], 'glyphs')
        with self.assertRaisesRegex(ValueError, 'mask'):
            thumb.contrast_backdrop(frame, rows, colors, text_mask=Image.new('L', frame.size))

    def test_generate_writes_valid_jpeg_and_report_without_touching_sources(self):
        md, data = self.metadata()
        video = self.root / 'video.mp4'; video.write_bytes(b'source')
        audio = self.root / 'song.mp3'; audio.write_bytes(b'source audio')
        (self.root / 'font-catalog.json').write_text(json.dumps({'schema_version': 1, 'fonts': [
            dict(file=self.font.name, family='Test', genre_hints=['rock'], mood_hints=[])]}))
        output = self.root / 'support'; output.mkdir()
        sources = {p: p.read_bytes() for p in (md, video, audio, self.font)}
        with mock.patch.object(thumb, 'extract_frame', return_value=(Image.new('RGB', (1280,720), '#715631'), 8.0)):
            result = thumb.generate(self.root, video, audio, md, output, font_dir=self.root)
        with Image.open(result['thumbnail.jpg']) as image:
            self.assertEqual(image.size, (1280, 720)); self.assertEqual(image.format, 'JPEG')
            image.load()
        report = json.loads((output / 'thumbnail-report.json').read_text())
        self.assertEqual(report['font']['family'], 'Test')
        self.assertEqual(report['contrast']['coverage'], 'glyphs')
        self.assertGreaterEqual(min(report['contrast']['ratios']), 4.5)
        self.assertLess(result['thumbnail.jpg'].stat().st_size, 2 * 1024 * 1024)
        for p, contents in sources.items(): self.assertEqual(p.read_bytes(), contents)
        with self.assertRaises(ValueError):
            thumb.generate(self.root, video, audio, md, output, font_dir=self.root)
        bright_output = self.root / 'bright-support'; bright_output.mkdir()
        with mock.patch.object(thumb, 'extract_frame', return_value=(Image.new('RGB', (1280,720), 'white'), 8.0)):
            thumb.generate(self.root, video, audio, md, bright_output, font_dir=self.root)
        bright_report = json.loads((bright_output/'thumbnail-report.json').read_text())
        self.assertEqual(bright_report['colors']['polarity'], 'dark')
        self.assertEqual(bright_report['contrast']['backdrop'], 'unchanged')
        with Image.open(bright_output/'thumbnail.jpg') as image:
            self.assertEqual(image.getpixel((0, 0)), (255, 255, 255))


class ThumbnailPublicationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def staged(self, run, content=b'new'):
        files = {'thumbnail.jpg': run/'support/thumbnail.jpg', 'youtube-upload.txt': run/'support/youtube-upload.txt'}
        for path in files.values(): path.write_bytes(content)
        return files

    def test_thumbnail_only_never_changes_video_and_archives_previous_extras(self):
        run = workflow.reserve_run(self.root, 'thumbnail')
        visible = self.root/'HASIL'; visible.mkdir()
        video = visible/'song-final.mp4'; video.write_bytes(b'original video')
        publication.publish_thumbnail(self.staged(run,b'old'), run)
        second = workflow.reserve_run(self.root, 'thumbnail')
        publication.publish_thumbnail(self.staged(second), second)
        self.assertEqual(video.read_bytes(), b'original video')
        self.assertEqual((visible/'thumbnail.jpg').read_bytes(), b'new')
        self.assertEqual((second/'support/previous-thumbnail/thumbnail.jpg').read_bytes(), b'old')

    def test_unknown_or_modified_thumbnails_are_not_overwritten(self):
        visible = self.root/'HASIL'; visible.mkdir()
        target = visible/'thumbnail.jpg'; target.write_bytes(b'user file')
        run = workflow.reserve_run(self.root, 'thumbnail')
        with self.assertRaises(ValueError): publication.publish_thumbnail(self.staged(run), run)
        self.assertEqual(target.read_bytes(), b'user file')
        target.unlink()
        publication.publish_thumbnail({name:run/'support'/name for name in ('thumbnail.jpg','youtube-upload.txt')},run)
        target.write_bytes(b'edited by user')
        second = workflow.reserve_run(self.root, 'thumbnail')
        with self.assertRaises(ValueError): publication.publish_thumbnail(self.staged(second),second)
        self.assertEqual(target.read_bytes(), b'edited by user')

    def test_full_publication_failure_rolls_back_thumbnail_and_metadata(self):
        first = workflow.reserve_run(self.root,'thumbnail')
        publication.publish_thumbnail(self.staged(first,b'old'),first)
        manifest = first.parent/'latest-thumbnail.json'; before = manifest.read_bytes()
        run = workflow.reserve_run(self.root,'full')
        staged = run/'support/song.mp4'; staged.write_bytes(b'new video')
        with mock.patch.object(publication,'_publish_full',side_effect=OSError('video failed')):
            with self.assertRaises(OSError):
                publication.publish(staged,run,'full',attachments=self.staged(run))
        self.assertEqual((self.root/'HASIL/thumbnail.jpg').read_bytes(), b'old')
        self.assertEqual((self.root/'HASIL/youtube-upload.txt').read_bytes(), b'old')
        self.assertEqual(manifest.read_bytes(),before)
        self.assertFalse((run.parent/'.publish.lock').exists())

    def test_existing_identical_upload_text_is_adopted_but_different_text_is_not(self):
        visible = self.root/'HASIL'; visible.mkdir()
        text = visible/'youtube-upload.txt'; text.write_bytes(b'new')
        run = workflow.reserve_run(self.root,'thumbnail')
        publication.publish_thumbnail(self.staged(run),run)
        self.assertEqual(text.read_bytes(), b'new')
        text.write_bytes(b'user correction')
        second=workflow.reserve_run(self.root,'thumbnail')
        with self.assertRaises(ValueError): publication.publish_thumbnail(self.staged(second),second)
        self.assertEqual(text.read_bytes(),b'user correction')

    def test_standalone_workflow_does_not_require_karaoke_assets_or_asr(self):
        for name in ('v.mp4','a.mp3','lyrics.md'): (self.root/name).write_bytes(b'input')
        video=self.root/'HASIL/a-final.mp4'; video.parent.mkdir(); video.write_bytes(b'final')
        args=cli.options([str(self.root),'--mode','thumbnail','--font-dir',str(self.root)])
        def generate(folder, v, audio, md, output, **kwargs):
            self.assertEqual(kwargs['font_dir'],self.root)
            return self.staged(output.parent)
        with mock.patch.object(thumb,'generate',side_effect=generate), \
                mock.patch.object(workflow.karaoke,'DEFAULT_FONT_FILE',self.root/'missing.otf'), \
                mock.patch.object(workflow,'run_command',side_effect=AssertionError('No render or ASR')):
            result=workflow.run(args)
        self.assertEqual(result,self.root/'HASIL/thumbnail.jpg')
        self.assertEqual(video.read_bytes(),b'final')

    def test_full_default_creates_thumbnail_before_render_and_publishes_bundle(self):
        video=self.root/'v.mp4'; video.write_bytes(b'video')
        audio=self.root/'a.mp3'; audio.write_bytes(b'audio')
        md=self.root/'lyrics.md'; md.write_text('Hello world')
        timing=self.root/'timing.json'
        timing.write_text(json.dumps(dict(schema=1,method='wav2vec2_ctc_forced_alignment',
            audio_sha256=thumb.digest(audio),lyrics_sha256=thumb.digest(md),coverage=[0,10],
            lines=[dict(index=0,words_text=['Hello','world'],words=[[1000,2000],[2200,3000]],scores=[.9,.9])])))
        args=cli.options([str(self.root),'--mode','full','--timing-file',str(timing)])
        events=[]
        def generate(folder,v,a,m,output,**kwargs):
            events.append('thumbnail'); return self.staged(output.parent)
        def render(command,log,**kwargs):
            events.append('render')
            out=Path(command[command.index('--out')+1]); (out/(self.root.name+'.mp4')).write_bytes(b'rendered')
        with mock.patch.object(thumb,'generate',side_effect=generate), \
                mock.patch.object(workflow.karaoke,'DEFAULT_FONT_FILE',audio), \
                mock.patch.object(workflow.karaoke,'DEFAULT_LOGO_FILE',video), \
                mock.patch.object(workflow.karaoke,'ffprobe_duration',return_value=10), \
                mock.patch.object(workflow,'run_command',side_effect=render):
            result=workflow.run(args)
        self.assertEqual(events,['thumbnail','render'])
        self.assertEqual(result.read_bytes(),b'rendered')
        self.assertEqual({p.name for p in result.parent.iterdir()}, {'a-final.mp4','thumbnail.jpg','youtube-upload.txt'})

    def test_sidecar_manifest_write_failure_restores_previous_files(self):
        first=workflow.reserve_run(self.root,'thumbnail')
        publication.publish_thumbnail(self.staged(first,b'old'),first)
        manifest=first.parent/'latest-thumbnail.json'; before=manifest.read_bytes()
        second=workflow.reserve_run(self.root,'thumbnail')
        with mock.patch.object(publication,'_write_json',side_effect=OSError('manifest failed')):
            with self.assertRaises(OSError): publication.publish_thumbnail(self.staged(second),second)
        self.assertEqual((self.root/'HASIL/thumbnail.jpg').read_bytes(),b'old')
        self.assertEqual((self.root/'HASIL/youtube-upload.txt').read_bytes(),b'old')
        self.assertEqual(manifest.read_bytes(),before)

    def test_thumbnail_publication_refuses_links_and_active_lock(self):
        run=workflow.reserve_run(self.root,'thumbnail'); files=self.staged(run)
        with mock.patch.object(Path,'is_symlink',autospec=True,side_effect=lambda p:p==self.root/'HASIL'):
            with self.assertRaises(ValueError): publication.publish_thumbnail(files,run)
        lock=run.parent/'.publish.lock'; lock.write_text('other job')
        with self.assertRaises(RuntimeError): publication.publish_thumbnail(files,run)
        self.assertEqual(lock.read_text(),'other job')


if __name__ == '__main__':
    unittest.main()
