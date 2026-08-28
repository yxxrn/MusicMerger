# Aset lokal

Letakkan aset yang Anda berhak gunakan pada lokasi berikut:

```text
assets/
  fonts/MADE Mirage Bold PERSONAL USE.otf
  fonts/GentleHearts-Regular.otf
  images/music logo.jpg
```

Font dan logo tidak dibundel ke GitHub secara default. File asli tetap ada pada
komputer pengembang dan diabaikan oleh `.gitignore`; tidak dihapus.

Renderer mengharapkan font MADE Mirage dan logo JPG hitam di atas latar terang.
Warna/outline logo dibentuk saat compositing. Penggunaan logo lain dengan warna
atau latar berbeda perlu pengujian tersendiri.

`GentleHearts-Regular.otf` adalah font opsional untuk contoh thumbnail lama,
bukan kebutuhan renderer karaoke. Koleksi font thumbnail otomatis berada di
`listfont/` pada root aplikasi, bersama `font-catalog.json` bila memakai katalog
sendiri. Opsi `--font-dir` dapat menunjuk koleksi di lokasi lain. Font asli dan
katalog lokal tetap di komputer pengguna; kode membawa katalog contoh tanpa font.

Nama font yang digunakan mencantumkan PERSONAL USE. Izin redistribusi atau
penggunaan komersial font dan gambar tidak ditetapkan oleh repository ini.
