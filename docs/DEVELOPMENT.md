# Pengembangan MusicMerger

## Struktur kode

| Modul | Tanggung jawab |
| --- | --- |
| `musicmerger/__main__.py`, `cli.py` | Entry point, menu, argumen, pembatalan |
| `workflow.py` | Validasi input, reuse timing, run unik |
| `publication.py` | Publikasi HASIL, manifest kepemilikan, pengarsipan versi sebelumnya |
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
