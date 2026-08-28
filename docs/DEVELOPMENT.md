# Pengembangan MusicMerger

## Struktur kode

| Modul | Tanggung jawab |
| --- | --- |
| `musicmerger/__main__.py`, `cli.py` | Entry point, menu, argumen, pembatalan |
| `workflow.py` | Validasi input, reuse timing, run unik |
| `publication.py` | Publikasi HASIL, manifest kepemilikan, pengarsipan versi sebelumnya |
| `thumbnail.py`, `thumbnail-fonts.json` | Metadata, font lokal, pemilihan frame, layout glyph, palet/kontras JPEG |
| `thumbnail_palette.py` | Kelompok warna foto, kandidat harmoni, polaritas teks dan koreksi kontras lokal |
| `sync.py` | Transkripsi, pemilihan model bahasa, orkestrasi alignment |
| `fallback.py` | Seleksi lirik, partisi indeks asli, validasi omission dan batas interval |
| `process.py` | Subprocess, log, progres, penutupan proses turunannya |
| `renderer.py` | CLI lanjutan, parser lirik, cache, ASS, render FFmpeg |
| `acoustic.py`, `timing.py` | Alignment CTC dan pencocokan teks ASR |
| `phrases.py` | Pemotongan frasa tampilan |
| `encoder.py`, `loop.py` | GPU/CPU encode dan seamless loop |
| `logo.py`, `equalizer.py` | Compositing logo dan bar spektrum |
| `paths.py` | Root checkout untuk aset/model; tidak bergantung cwd pemanggil |

Jalankan dari root checkout. Aplikasi ini belum didistribusikan sebagai wheel
atau executable mandiri; tidak memerlukan `pip install -e .`.

```powershell
python -B -m musicmerger --help
python -B -m musicmerger.renderer --help
python -B -m musicmerger.acoustic --help
python -B -m unittest discover -s tests -q
```

Tes lengkap membutuhkan FFmpeg/ffprobe, dependensi alignment, model Inggris
lokal, serta font/logo yang dijelaskan di [panduan aset](../assets/README.md).
Smoke test entry point tidak membutuhkan model atau media:

```powershell
python -B -m unittest discover -s tests -p test_project_layout.py -q
```

## Kontrak yang harus dipertahankan

- Bahan asli dan cache lama tidak ditimpa. Setiap run memakai folder unik.
- Hash audio/lirik, isi kata, cakupan dan urutan timing harus cocok sebelum reuse.
- Render yang gagal tetap di `support/`; hanya MP4 selesai masuk `preview/` atau `HASIL/`.
- `HASIL/<nama MP3>-final.mp4` adalah final terbaru. Publikasi berikutnya memindah
  versi lama ke `final/` run asal dan memperbarui `status.json` run lama.
- Manifest `MusicMerger-output/latest-final.json` menyimpan run, filename dan SHA256.
  Publikasi memeriksa kepemilikan, menolak link/junction dan collision, serta memakai
  lock eksklusif. Error normal mengembalikan file/status sebelumnya; pemutusan paksa
  dapat menyisakan lock dan memerlukan pemeriksaan manual sebelum publikasi ulang.
- `publish(staged, run, 'full', song_name=audio.stem)` juga menerima sumber dari
  `run/final/` untuk migrasi hasil lama tanpa encoding. Pemanggil migrasi harus
  memverifikasi run selesai serta hash input/output, lalu memperbarui status output.
- Audio output tetap berasal dari MP3 asli, meskipun analisis memakai vokal terpisah.
- GPU dipakai untuk encoding; filter dan alignment CPU tetap didukung.
- Skor alignment adalah diagnostik, bukan persentase akurasi timing nyanyian.
- Style default: Mirage, frasa pendek di tengah, hold/fade, logo senada,
  equalizer subtle, seamless loop. Refaktor struktur tidak mengubah style.

## Kontrak fallback otomatis

- `--lyric-policy auto` adalah default. Small yang lemah memicu satu retry medium;
  kandidat kuat dipilih dengan jalur waktu antarbaris yang tidak bertabrakan,
  tanpa clipping timestamp. Lirik yang gagal setelah dua model dicatat sebagai omission.
- Policy v1: support per baris >=0.8, token dipertahankan >=0.75. Skor CTC rendah
  saja tidak menghapus lirik. Masalah model/dependensi/media bukan improvisasi.
- Sidecar schema 1 tetap memakai validasi lama. Schema 2 wajib full coverage,
  hash audio/MD asli, `source_words`, `source_line_count`, policy, `lines` yang
  ditiming, `omitted_lines` dengan bukti kedua model, serta `omission_windows`.
  Gabungan indeks timed/omitted harus tepat mencakup seluruh MD tanpa duplikat.
- Worker CTC mengompakkan eksekusi saja, memisahkan batch pada omission, lalu
  mengembalikan indeks asli. Renderer schema 2 membaca indeks itu langsung;
  jangan realign cache kata terkompak ke seluruh MD karena chorus dapat tertukar.
- Window omission dihitung dari kata CTC tetangga/tepi audio, lalu renderer
  mengurangi event ASS sebenarnya (termasuk hold/fade/lead-in). Sisa interval
  menampilkan logo dan menyembunyikan equalizer pada waktu lagu yang sama,
  termasuk preview yang dimulai di tengah lagu. Fade logo dipendekkan untuk gap pendek.
- Strict menolak sidecar omission. Tidak boleh menurunkan schema, mengubah MD,
  atau menambahkan timestamp palsu agar validasi lolos.
- Preview tetap disarankan: omission berarti tidak didukung pemeriksaan otomatis,
  bukan bukti bahwa baris tersebut tidak dinyanyikan.

## Kontrak thumbnail

- CLI `full` menghasilkan sidecar sebelum ASR; `publish(..., attachments=...)`
  mempublikasikan video dan sidecar di bawah satu lock. Kegagalan publikasi video
  mengembalikan JPEG/TXT/manifest sebelumnya melalui context manager `_extras`.
- `thumbnail` memakai `publish_thumbnail` tanpa menyentuh MP4/timing; tidak
  membutuhkan Mirage, logo, Whisper atau CTC. `preview` tetap tanpa thumbnail.
- Metadata wajib cocok nama/hash MD. Semua input termasuk JSON/font difingerprint
  sebelum/sesudah generasi. Tidak ada title generator atau perubahan MD otomatis.
- Default font dir relatif terhadap `paths.ROOT`, dapat dioverride `--font-dir`;
  kode tidak memuat path runtime pengguna atau dependensi Codex/Chrome.
- Katalog lokal mengalahkan katalog bawaan. Gunakan cmap fontTools untuk menolak
  kandidat yang tidak mendukung judul; Pillow menghitung bbox glyph dan origin
  optis. Tidak ada substitusi karakter/pemotongan judul diam-diam.
- `layout_title` mengukur kandidat 1–3 baris (maks. 30 kata) dalam area aman,
  memberi bobot lebih besar pada baris akhir dan menghindari pemisahan buruk.
- Palet v2 memakai 12 cluster RGB dari sampel 160x90 (Pillow median cut), lalu
  mengelompokkan hue kromatik sambil mempertahankan sumber warna sekunder.
  Kandidat tonal/analogous/complementary dan neutral tersedia dalam polaritas
  light/dark. Luminans sasaran dibuat sebanding agar hue tertentu tidak menang
  hanya karena secara intrinsik lebih terang. Mood energik menaikkan saturasi.
- Skor menggabungkan besarnya koreksi lokal dan preferensi harmoni berdasarkan
  hue/exposure; tidak memakai nomor folder, judul, atau warna acak. Foto hampir
  monokrom bisa tetap memilih keluarga yang sama; ini bukan model estetika/vision.
- Tidak ada penggelapan global. Generator menggambar mask glyph judul/caption
  dengan font/posisi aktual. Koreksi blend hitam/putih hanya pada mask tersebut,
  dilatasi 3 px dengan Gaussian blur 2 px di tepinya; ruang kosong antarkata dan
  antarbaris tidak diisi blok. Alpha dibatasi 189/255 untuk teks terang dan
  140/255 untuk teks gelap; bayangan dekoratif juga dipersempit (blur 3 px).
- Kontras simetris (luminans terang+0.05)/(luminans gelap+0.05) diuji terhadap
  setiap pixel unik pada sampel 100x32. Seleksi palet dan verifikasi akhir memakai
  nearest sampling yang sama, sejajar dengan mask, hanya inti glyph coverage
  >=192/255. Ini mencegah detail gelap hilang saat estimasi sehingga palet terpilih
  tidak bisa memenuhi batas koreksi ketika diperiksa. Pencarian alpha menargetkan
  4.55, lalu hasil compositing aktual diverifikasi >=4.5. Gagal berarti berhenti.
  Angka ini tidak berlaku untuk ruang kosong bbox, piksel antialias tipis, atau
  tepian halo. Laporan mencatat coverage= glyphs dan support_area_fraction.
  Helper tanpa text_mask tetap mendukung koreksi bbox; generator selalu memasok
  mask glyph. Mask kosong/tidak sesuai ukuran ditolak.
  Bayangan teks hanya dipakai untuk polaritas terang. Laporan bukan klaim kontras
  setiap pixel setelah kompresi JPEG atau jaminan kualitas editorial.
- Tiga kandidat frame 25/40/65% dipilih berdasarkan paparan/detail, mengutamakan
  posisi 40% bila kualitas setara. Tidak ada model vision/audio dan tidak ada
  pemahaman semantik objek dari skor tersebut.
- `latest-thumbnail.json` memiliki schema 1, run dan `files` berupa hash JPEG/TXT.
  Backup penggantian ada di support run baru. Tolak link/junction, collision,
  dan hasil yang diedit. Crash/pemutusan paksa bisa menyisakan lock; jangan hapus
  lock tanpa memeriksa status/files. Jangan klaim transaksi tahan power-loss.
- `tests/test_thumbnail.py` membuat font sintetis untuk unit/integration test,
  tanpa mengharuskan file font pengguna. Uji visual tetap perlu font nyata.

## Pemulihan batch dan konflik batas ASR

Operator `python -B -m musicmerger.batch` menyediakan `run`, `status`, dan
`resume`; lihat [BATCH.md](BATCH.md) untuk checkpoint, proses terpisah, dan
verifikasi sebelum melewati hasil yang sudah lengkap. Tidak ada scheduler
boot otomatis. Hasil gagal perlu ditinjau sebelum `resume --retry-failed`.

Jika kandidat ASR yang didukung bertabrakan pada batas dua baris berurutan,
`sync` mencoba CTC lokal pada jendela maksimal 30 detik, overlap maksimal
1 detik, dan perubahan batas maksimal 2 detik. Kata dan indeks MD tidak diubah,
timestamp tidak dipotong atau digeser untuk memaksakan urutan. Kandidat harus
memenuhi dukungan ASR yang sama serta pemeriksaan score dan rentang CTC.
Konflik yang melewati omission, urutan terbalik, atau bukti yang lemah tetap
berhenti. `reference-repaired.json` dan `boundary-repair.log` menyimpan bukti;
alignment penuh dan validator timing akhir tetap wajib. Score CTC bukan
persentase akurasi; hasil koreksi tetap ditandai perlu review audio.

## Cakupan publik dan data lokal

`.gitignore` memisahkan source/test/dokumentasi publik dari `inputs`, `outputs`,
`archive`, `.models`, catatan kerja lokal dan aset pengguna. Ignore tidak
menghapus file lokal dan tidak berlaku untuk file yang sudah terlanjur dilacak
Git. Periksa status dan staged diff sebelum commit.

Jangan unggah seluruh folder menggunakan drag-and-drop GitHub: upload web tidak
menyaring berdasarkan `.gitignore`. Gunakan Git/GitHub Desktop dan periksa daftar
file, atau pilih hanya file publik secara manual.

Tidak ada file LICENSE yang ditambahkan secara otomatis. Pemilik proyek perlu
menentukan lisensi source jika ingin memberikan izin penggunaan ulang; aset
font/logo punya izin tersendiri.
