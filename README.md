# MusicMerger

CLI lokal untuk menggabungkan satu video MP4, musik MP3, dan lirik Markdown menjadi video karaoke. Lirik, logo dan equalizer dibakar langsung ke gambar video.

## Fitur

- Menu sederhana: pilih folder, lalu Preview atau Render penuh.
- Frasa lirik besar di tengah, sorotan per kata, hold/fade, font Mirage.
- Logo musik saat jeda dan equalizer subtle.
- Video latar diulang dengan crossfade pada sambungan.
- Encode GPU otomatis dengan fallback CPU.
- Timing otomatis, reuse berdasarkan identitas audio/lirik, dan retry terbatas.
- Final terbaru langsung terlihat di `HASIL/`; versi sebelumnya masuk riwayat.
- MP4 terpisah dari subtitle/log; bahan asli tidak ditimpa.

## Struktur repository

```text
MusicMerger/
  musicmerger/                 Semua kode Python aplikasi
    __main__.py                python -m musicmerger
    cli.py                     Menu terminal
    workflow.py                Alur pekerjaan
    publication.py             HASIL dan riwayat final
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

**Windows:** klik dua kali `MusicMerger.bat`, paste path folder lagu, lalu pilih **1 = Preview** atau **2 = Render penuh**. Folder boleh berada di luar repository.

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
```

Opsi tambahan: `--encoder cpu`, `--language en`, `--width 640` (preview), `--timing-file <file.json>`, `--vocals off`, `--lyric-policy strict`. Lihat `python -B -m musicmerger --help`.

CLI renderer lama kini diakses dengan `python -B -m musicmerger.renderer --help`. Jangan menjalankan file di dalam paket secara langsung; gunakan `-m` agar import dan worker tetap benar.

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
  MusicMerger-output/
    latest-final.json            Identitas hasil yang dikelola aplikasi
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
```

Untuk render penuh, cukup buka **HASIL** di folder lagu. Setelah render baru berhasil,
final sebelumnya dipindahkan ke `final/` pada run asalnya, lalu hasil baru menggantikannya
di HASIL. Tidak ada salinan video tambahan atau penghapusan riwayat otomatis.
Preview tetap berada di run masing-masing dan tidak mengubah final terbaru.

Lokasi MP4 dan folder hasil ditampilkan setelah **SELESAI**. Upload MP4 saja.
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
