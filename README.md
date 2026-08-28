# MusicMerger

CLI lokal untuk menggabungkan satu video MP4, musik MP3, dan lirik Markdown menjadi video karaoke. Lirik, logo dan equalizer dibakar langsung ke gambar video.

## Fitur

- Menu sederhana: pilih folder, lalu Preview, Render penuh + thumbnail, atau Thumbnail saja.
- Frasa lirik besar di tengah, sorotan per kata, hold/fade, font Mirage.
- Logo musik saat jeda dan equalizer subtle.
- Video latar diulang dengan crossfade pada sambungan.
- Encode GPU otomatis dengan fallback CPU.
- Timing otomatis, reuse berdasarkan identitas audio/lirik, dan retry terbatas.
- Final terbaru langsung terlihat di `HASIL/`; versi sebelumnya masuk riwayat.
- MP4 terpisah dari subtitle/log; bahan asli tidak ditimpa.
- Thumbnail otomatis: frame video asli, judul tengah, font sesuai genre/mood,
  warna kalem dan kontras terukur; tersedia juga mode thumbnail saja.

## Struktur repository

```text
MusicMerger/
  musicmerger/                 Semua kode Python aplikasi
    __main__.py                python -m musicmerger
    cli.py                     Menu terminal
    workflow.py                Alur pekerjaan
    publication.py             HASIL dan riwayat final
    thumbnail.py               Pemilihan font/frame, layout dan JPEG thumbnail
    thumbnail-fonts.json       Katalog karakter font (tanpa file font)
    sync.py                    Persiapan timing otomatis
    fallback.py                Pemilihan lirik dan catatan bagian yang dilewati
    renderer.py                Renderer dan CLI lanjutan
    acoustic.py, timing.py      Alignment lirik
    encoder.py, loop.py         Encoding dan video loop
    logo.py, equalizer.py       Overlay
    phrases.py, process.py      Frasa dan proses worker
    paths.py                   Lokasi aset dan model
  assets/
    fonts/                     Font lokal, tidak dibundel
    images/                    Logo lokal, tidak dibundel
  listfont/                    Koleksi font thumbnail dan katalog lokal (diabaikan Git)
  tests/                       Tes regresi
  docs/DEVELOPMENT.md           Panduan pengembangan
  MusicMerger.bat               Launcher Windows
  requirements.txt             Dependensi dasar
  requirements-alignment.txt   Dependensi alignment
  .gitignore
  .gitattributes
  README.md
```

Folder `inputs/`, `outputs/`, `archive/`, `.models/`, dan catatan kerja lain boleh tetap ada secara lokal, tetapi tidak ikut GitHub.

Simpan font karaoke dan font contoh di `assets/fonts/`; koleksi yang dipilih otomatis
untuk thumbnail tetap di `listfont/`. Simpan eksperimen serta bukti pemeriksaan di
`archive/`, bukan di root. Hasil siap unggah berada di `HASIL/` pada folder lagu;
log, timing, dan riwayat proses berada di `MusicMerger-output/` pada folder yang sama.

## Persiapan

Gunakan Python 3.11 dan FFmpeg/ffprobe pada PATH. Build FFmpeg harus memiliki filter libass dan showfreqs.

Dari root repository:

```powershell
python -m pip install -r requirements-alignment.txt
```

Siapkan font dan logo sesuai [panduan aset](assets/README.md):

```text
assets/fonts/MADE Mirage Bold PERSONAL USE.otf
assets/images/music logo.jpg
```

Aset pengguna tidak didistribusikan otomatis. Pada komputer yang sebelumnya menjalankan proyek ini, aset tersebut sudah dipindahkan ke lokasi baru tanpa mengubah isinya.

Model CTC bawaan memakai [facebook/wav2vec2-base-960h](https://huggingface.co/facebook/wav2vec2-base-960h) untuk bahasa Inggris. Jika folder model lokal belum tersedia, unduh satu kali:

```powershell
python -c "from huggingface_hub import snapshot_download; snapshot_download('facebook/wav2vec2-base-960h', local_dir='.models/wav2vec2-base-960h', allow_patterns=['*.json', 'model.safetensors'])"
```

Model tidak masuk GitHub. Model Whisper dapat diunduh saat transkripsi pertama. Dependensi adalah rentang versi, bukan lockfile; versi runtime yang terakhir diuji: faster-whisper 1.2.1, torch 2.13.0, transformers 5.12.1, numpy 2.4.6.

## Cara menjalankan

**Windows:** klik dua kali `MusicMerger.bat`, paste path folder lagu, lalu pilih
**1 = Preview**, **2 = Render penuh + thumbnail**, atau **3 = Thumbnail saja**.
Folder boleh berada di luar repository.

Atau dari terminal di root proyek:

```powershell
python -B -m musicmerger
```

Folder input berisi tepat satu file setiap jenis:

```text
LaguSaya/
  video.mp4
  musik.mp3
  lirik.md
```

Lirik boleh teks biasa, satu frasa per baris, atau menggunakan `Lyrics`, `[Verse]` dan `[Chorus]`. Tuliskan sesuai rekaman termasuk pengulangan; jangan masukkan instruksi atau prompt sebagai lirik.

Tanpa menu:

```powershell
python -B -m musicmerger 'D:\Folder Lagu' --mode preview --start 3 --duration 15
python -B -m musicmerger 'D:\Folder Lagu' --mode full
python -B -m musicmerger 'D:\Folder Lagu' --mode thumbnail
```

Opsi tambahan: `--encoder cpu`, `--language en`, `--width 640` (preview), `--timing-file <file.json>`, `--vocals off`, `--lyric-policy strict`. Lihat `python -B -m musicmerger --help`.

CLI renderer lama kini diakses dengan `python -B -m musicmerger.renderer --help`. Jangan menjalankan file di dalam paket secara langsung; gunakan `-m` agar import dan worker tetap benar.

### Thumbnail otomatis

Mode `full` pada CLI utama otomatis membuat thumbnail sebelum pekerjaan timing
yang mahal, lalu mempublikasikannya bersama video setelah render berhasil.
Mode `thumbnail` hanya membuat JPEG dan metadata upload; tidak menjalankan ASR,
CTC, encode video atau mengubah MP4 yang sudah selesai. Mode `preview` dan CLI
lanjutan `musicmerger.renderer` tidak membuat thumbnail.

Siapkan `listfont/` di root checkout berisi font pilihan pengguna. Katalog bawaan
mengenali Bogimber, Brant, Classica Bold Oblique, Dream Orphans Bold, Morris Roman
Black, Rockdale, Yeseva One, dan ZT Otez. Tidak ada file font yang dibundel atau
diunduh. Untuk katalog khusus, taruh `font-catalog.json` di folder font tersebut
dengan `schema_version: 1` dan daftar `fonts` (field `file`, `family`,
`genre_hints`, `mood_hints`). Setiap nama file harus berada langsung di folder itu.

Folder lagu membutuhkan **youtube-metadata.json** yang sudah direview:

```json
{
  "schema_version": 1,
  "thumbnail_title": "Shape the Wood",
  "youtube_title": "Shape the Wood | Roots Rock (Lyric Video)",
  "tags": ["roots rock", "woodworking song"],
  "source_md": "lirik.md",
  "source_md_sha256": "isi dengan SHA256 file lirik.md yang direview"
}
```

Dapatkan hash melalui `Get-FileHash -Algorithm SHA256 -LiteralPath 'lirik.md'`
(simpan sebagai huruf kecil). Metadata yang hilang, tidak valid, atau berasal
dari MD berbeda akan menghentikan proses dengan pesan koreksi; aplikasi tidak
mengarang judul/tag atau mengubah lirik. Metadata yang telah dibuat sebelumnya
dengan format ini dapat langsung dipakai.

```powershell
# Thumbnail saja, memakai koleksi font di lokasi lain (misalnya dari worktree)
python -B -m musicmerger 'D:\Folder Lagu' --mode thumbnail --font-dir 'D:\Koleksi Font'
# Tetap render video bila belum menyiapkan metadata/font thumbnail
python -B -m musicmerger 'D:\Folder Lagu' --mode full --no-thumbnail
```

Pemilih font memakai genre/mood dari bagian **Style Prompt** MD dan tag yang
disetujui, bukan menebak genre dari lirik atau mendengarkan audio dengan LLM.
Klausa negatif seperti `no EDM` tidak dipakai sebagai rekomendasi genre. Semua
karakter judul harus tersedia di font; tidak ada fallback sebagian huruf secara
diam-diam. Ukuran dan pemisahan 1–3 baris dihitung dari batas glyph aktual.

Hasil JPEG 1280×720 di bawah 2 MiB, tanpa bingkai/footer/panel. Warna aksen
mengikuti warna frame, kalem untuk lagu tenang dan lebih cerah untuk mood energik.
Foto digelapkan secara lembut di area teks hingga kontras sampel minimum 4.5:1.
Ini tidak harus identik dengan palet karaoke dan tidak mengubah style video.
Pemilihan font/genre dan pemisahan judul bersifat heuristik; tetap review hasilnya.
Tidak membutuhkan Chrome, Node, layanan berbayar, atau model AI tambahan.

## Output

### Lirik tidak jelas atau improvisasi

Mode bawaan `--lyric-policy auto` mencoba model kedua jika kecocokan lirik lemah.
Baris yang masih tidak didukung dilewati dalam hasil tampilan: bagian tersebut
memakai logo tanpa teks/equalizer, lalu karaoke dilanjutkan saat lirik yang cocok
kembali. **MD, audio, dan video sumber tidak diubah.** Tidak ada kata pengganti
atau timing kata yang dikarang untuk baris yang dilewati.

Kecocokan minimal per baris adalah 80% token dengan anchor tepat dan posisi yang
masuk akal. Model kedua dapat menyelamatkan baris yang gagal pada model pertama.
Jika kurang dari 75% total token lirik dapat dipertahankan, atau urutan waktunya
tidak dapat ditentukan dengan aman, proses tetap berhenti untuk pemeriksaan input.
Ini aturan konservatif, bukan persentase akurasi: ASR dapat melewatkan lirik yang
sebenarnya dinyanyikan. Periksa preview dan laporan `support/*.alignment.json`.

Gunakan `--lyric-policy strict` untuk perilaku lama yang berhenti bila masih ada
baris tanpa anchor. Strict menolak pemakaian timing yang mencatat omission.
Timing baru menyimpan nomor baris asli dan alasan bagian dilewati; timing lama
yang lengkap tetap dapat digunakan. Cache ASR dari run sebelumnya hanya disalin
bila identitas audio/model/bahasa cocok, tidak ditimpa.

CLI `musicmerger` mengatur fallback saat sinkronisasi. CLI lanjutan
`musicmerger.renderer` memakai keputusan fallback dari sidecar schema 2; ia
tidak membuat keputusan omission baru hanya dari cache ASR.

### Susunan hasil

```text
LaguSaya/
  HASIL/
    musik-final.mp4               Final terbaru, nama mengikuti MP3
    thumbnail.jpg                Thumbnail terakhir (full/thumbnail)
    youtube-upload.txt           Judul dan tags, siap salin ke YouTube
  MusicMerger-output/
    latest-final.json            Identitas hasil yang dikelola aplikasi
    latest-thumbnail.json        Identitas JPEG dan metadata upload
    cache/                       Timing yang dapat dipakai ulang
    <tanggal>-preview-001/
      preview/LaguSaya.mp4        Preview tidak mengganti final
      timing/
      support/                   ASS, log, cache dan aset perantara
      status.json
    <tanggal>-full-001/
      final/                     Versi lama disimpan di run asalnya
      timing/
      support/
      status.json                Menunjuk lokasi MP4 run tersebut
    <tanggal>-thumbnail-001/
      support/                   Frame, laporan pilihan font/kontras, JPEG sumber
        previous-thumbnail/      Backup sidecar sebelumnya bila diganti
      status.json
```

Untuk render penuh, cukup buka **HASIL** di folder lagu. Setelah render baru berhasil,
final sebelumnya dipindahkan ke `final/` pada run asalnya, lalu hasil baru menggantikannya
di HASIL. Tidak ada salinan video tambahan atau penghapusan riwayat otomatis.
Preview tetap berada di run masing-masing dan tidak mengubah final terbaru.

Lokasi hasil ditampilkan setelah **SELESAI**. Upload MP4 sebagai video, pilih
`thumbnail.jpg` pada kolom thumbnail, dan salin judul/tag dari `youtube-upload.txt`.
Metadata JSON, laporan dan font tidak perlu diunggah.

Penggantian thumbnail memakai lock publikasi yang sama dengan video. File hasil
yang telah diedit pengguna atau file tak dikenal tidak ditimpa; metadata TXT lama
tanpa manifest hanya dapat diadopsi bila byte-identik dengan hasil baru. Backup
sidecar lama disimpan di `support/previous-thumbnail` pada run penggantinya.
Error publikasi normal memulihkan hasil sebelumnya. `--no-thumbnail` tidak
mengganti thumbnail lama; periksa kecocokannya sebelum upload video baru.
Ctrl+C membatalkan proses; file parsial tetap di support dan tidak mengganti final.
File yang bertabrakan namanya tetapi tidak tercatat sebagai milik aplikasi, atau final
yang telah diedit pengguna, tidak ditimpa. Simpan file pribadi di luar HASIL.

Publikasi memakai `.publish.lock` dalam `MusicMerger-output` untuk mencegah dua proses
mengganti hasil bersamaan. Jika proses dimatikan paksa saat publikasi, proses berikutnya
berhenti: periksa HASIL, status run, dan manifest sebelum memulihkan lock. Jangan
menghapus `latest-final.json` atau folder run final aktif saat final tersebut masih dipakai.

## Batasan

- Timing nyanyian tidak dijamin tepat 100%; skor model bukan persentase akurasi. Preview tetap disarankan.
- Model yang disiapkan secara default hanya bahasa Inggris. Bahasa lain membutuhkan `--align-model` yang sesuai dan belum diuji pada proyek ini.
- Pemisahan vokal Demucs opsional, tidak wajib dan tidak dipasang otomatis. Bila tidak tersedia, analisis memakai MP3 asli dengan pemberitahuan. Audio final selalu MP3 asli.
- GPU mempercepat encoding; filter dan alignment tetap berjalan di CPU.
- Distribusi font/logo dan lisensi source harus ditentukan pemiliknya sebelum membagikan aset atau memberikan izin penggunaan ulang.

## Pengembangan dan GitHub

```powershell
python -B -m unittest discover -s tests -q
```

Tes lengkap memerlukan aset/model lokal dan FFmpeg. [Panduan pengembangan](docs/DEVELOPMENT.md) menjelaskan modul, smoke test tanpa media, dan batas data publik.

`.gitignore` sudah menyingkirkan video, audio, model, cache, arsip, aset lokal dan catatan kerja dari calon file Git. Periksa daftar file sebelum commit/push. Untuk upload manual lewat browser, pilih hanya source, tests, launcher, requirements, README, konfigurasi Git, panduan aset dan dokumentasi pengembangan—jangan unggah seluruh folder lokal.
