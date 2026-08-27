# Pengembangan MusicMerger

## Struktur kode

| Modul | Tanggung jawab |
| --- | --- |
| `musicmerger/__main__.py`, `cli.py` | Entry point, menu, argumen, pembatalan |
| `workflow.py` | Validasi input, reuse timing, run unik, publikasi MP4 |
| `sync.py` | Transkripsi, pemilihan model bahasa, orkestrasi alignment |
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
- Render yang gagal tetap di `support/`; hanya MP4 selesai masuk `preview/` atau `final/`.
- Audio output tetap berasal dari MP3 asli, meskipun analisis memakai vokal terpisah.
- GPU dipakai untuk encoding; filter dan alignment CPU tetap didukung.
- Skor alignment adalah diagnostik, bukan persentase akurasi timing nyanyian.
- Style default: Mirage, frasa pendek di tengah, hold/fade, logo senada,
  equalizer subtle, seamless loop. Refaktor struktur tidak mengubah style.

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
