# Validasi perubahan

Dijalankan pada cloud Linux, Python **3.10.12 dan 3.12.14**, Node 24.19.0/npm 11.9.0. Tidak ada akses Google Drive/Colab nyata, alokasi GPU, training, inferensi model berat, atau pembacaan file autentikasi pribadi. Data tes seluruhnya berada di direktori sementara. Tidak ada reset data laptop pengguna.

## Hasil

| Pemeriksaan | Hasil |
| --- | --- |
| `python -m unittest discover -s tests -v` | **113 tes offline lulus pada masing-masing interpreter** |
| `npm --prefix frontend run build` | Lulus, frontend produksi dibangun |
| `npm --prefix frontend run lint` | Exit 0; 18 warning kode React yang masih perlu perbaikan terpisah |
| `python3 tests/smoke_install.py` | Instalasi nyata dua kali pada Python 3.10.12 dan 3.12.14, interpreter virtualenv cocok, production HTTP/asset, pengunci pembaruan, shutdown SIGTERM, proses lain dipertahankan, uninstall dengan data/konfigurasi tetap ada |
| `python3 tests/browser_smoke.py` | Chromium desktop 1440×1000 dan mobile 390×844 lulus; API mock |
| `python3 tests/browser_dataset_smoke.py` | Pilihan dataset, status upload/verifikasi/gagal, retry tanpa upload ulang dan payload dataset_ids lulus dengan API mock |
| `python3 tests/browser_updates_smoke.py` | Sidebar versi, status no-release/jaringan/rate limit, target/catatan/instruksi, notifikasi setelah startup dan tidak berulang, desktop/mobile lulus dengan API mock |
| `python3 tests/browser_source_analysis_smoke.py` | Hitungan QA/indeks kosong, konfirmasi sebelum training, peran fakta yang tidak mempromosikan chat, dan reindex model tanpa training lulus (API mock) |
| `python -m compileall -q backend/app training tools tests chanthecno-hub` | Lulus |
| Pemeriksaan whitespace Git | Lulus |

Tes backend mencakup startup/database baru, isolasi/persistensi dataset, duplikasi, UTF-8/NUL/path/batas ukuran, URL tanpa fetch, host/origin lokal, model lama setelah pekerjaan baru gagal, training selesai dengan backup gagal, pemulihan yang tidak melatih ulang, retry upload, ukuran/checksum cadangan, heartbeat kedaluwarsa, pencegahan worker ganda/pergantian proyek, kernel yang masih sibuk, penolakan OAuth, akses Colab read-only, state checkpoint lengkap, retrieval, fallback pengetahuan umum memakai model yang sama (mock), reset/restore SQLite termasuk WAL, kegagalan snapshot, korupsi cadangan dan symlink/path traversal/ukuran arsip.

Uji browser memeriksa nama model dinamis, pengiriman chat, salin jawaban, percakapan baru, panel model yang dapat dilipat, pergantian proyek ketika jawaban lama masih tertunda, tidak ada overflow horizontal, serta judul dan kolom pesan yang tetap terlihat pada mobile. Respons chat dan status model di screenshot adalah **fixture pengujian**, bukan hasil GPU atau akses Drive nyata.

- [Desktop](screenshots/model-testing-desktop.png)
- [Mobile](screenshots/model-testing-mobile.png)
- [Pembaruan desktop (fixture)](screenshots/updates-desktop.png)
- [Pembaruan mobile (fixture)](screenshots/updates-mobile.png)
- [Peringatan analisis QA/indeks kosong (fixture)](screenshots/source-analysis-empty.png)
- [Pilihan dan ringkasan dataset (fixture)](screenshots/dataset-training-selection.png)

Untuk mengulang browser smoke, gunakan Python dengan Playwright (divalidasi 1.62.0) dan Chromium lokal di `/usr/bin/chromium`. Suite backend hanya membutuhkan `requirements-dev.txt`; smoke installer membutuhkan akses registry paket dan port lokal 8000 kosong. Keempat browser smoke memakai port 8765 dan dijalankan bergantian. Tidak ada dependensi Playwright dalam runtime aplikasi.

Starlette saat ini menampilkan peringatan deprecation kompatibilitas TestClient/httpx; seluruh tes tetap berjalan dan lulus. CI memakai matriks Python 3.10.12/3.12 untuk suite offline, build dan lint; eksekusi CI pada GitHub belum diverifikasi di cloud ini.

## Tambahan bukti kompatibilitas, Drive, dan retrieval

- Interpreter Python 3.10.12 sungguhan dipasang terpisah di workspace, tanpa mengganti Python sistem. `pip install -r requirements-dev.txt` dan `pip check` berhasil dengan pin backend yang sama. Pengujian bukan sekadar parse/simulasi nomor versi. Google API Core tetap menampilkan FutureWarning penghentian dukungan upstream 3.10; ini dicatat, tidak disembunyikan atau dipaksa dengan downgrade.
- Fake Drive menjalankan protokol daftar folder, generateIds, upload, kehilangan acknowledgment, dan GET metadata. Tes menolak perbedaan ID/folder/ukuran/checksum, file trash, tag hash tanpa checksum API, pilihan proyek lain, perubahan lokal berukuran sama, dan satu pilihan pending di antara yang sudah saved. Retry memakai ID yang sama; tidak menjalankan create kedua ketika upload pertama sebenarnya sudah selesai. Sumber lokal dipertahankan saat upload gagal. Dukungan checksum SHA-256 API dan fallback MD5 API diuji terpisah.
- Endpoint start diuji langsung: sumber belum terverifikasi ditolak sebelum thread persiapan dibuat. Versi lokal diperiksa kembali di fungsi persiapan sebelum RuntimeBridge dibuat. Download paket dengan checksum berubah juga ditolak. Manifest per job dibuat sekali, diverifikasi digest, tetap sama setelah dataset baru ditambah, dan perubahan manifest menyebabkan resume ditolak sebelum model dimuat. Seluruh koneksi T4/model pada tes tersebut dimock.
- Fixture retrieval berisi fakta dokumenter, USER/ASSISTANT, lanjutan chat multiline, soal, fakta umum, identitas palsu dalam chat, dan kalimat tidak relevan. Tes membuktikan kutipan/fakta asal yang tepat, penolakan tanpa bukti, offset yang cocok dengan passage, dan pencegahan fakta Alpha menjawab pertanyaan Beta. Indeks legacy tanpa penanda aman tidak dikutip.
- Rebuild indeks diuji dari TXT fixture dengan nama/ID Drive tetap benar. Kode kontrol restore dijalankan hanya dengan path `/content` dipetakan ke direktori sementara dan **semua subprocess venv/pip/GPU diintersep mock**; tes membuktikan indeks lama bisa diganti tanpa menimpa bobot yang cocok. Tidak ada perintah GPU/Colab nyata yang dijalankan.

## Versi dan pembaruan CTH 1.1.0

- Satu versi aplikasi digunakan API, launcher, installer, manifest frontend, release-notes dan dokumentasi. `--version` launcher/installer diuji langsung. Nomor 1.1.0 adalah versi kode; tidak ada GitHub Release yang diterbitkan dalam tugas ini.
- Pemeriksaan memakai fixture release stabil lebih baru/sama/lebih lama, 404/no release, timeout, jaringan gagal, 403/429, Retry-After/reset API, draft/prerelease/tag tak didukung, respons berukuran berlebihan, redirect, cache, dan pemeriksaan paralel. Gagal memeriksa tidak diubah menjadi status terbaru. Header/body outgoing diuji: hanya URL publik repositori tetap dan header API/User-Agent; tanpa token Google atau data proyek. Pemanggilan Google credentials tidak terjadi.
- Metadata installer saja tidak menandai update dilihat. Tes membuktikan notifikasi baru muncul melalui HTTP versi baru, ACK tersimpan di direktori data, instalasi pertama/reinstall versi sama tidak dinyatakan update, downgrade tidak dinyatakan upgrade, dan versi UI yang tidak cocok ditolak. Browser memeriksa panel Yang baru dan bahwa reload setelah ACK tidak mengulang notifikasi.
- Dependency/build/startup gagal diuji dengan mock build; rilis lama tetap dapat dijalankan. Kegagalan pemasangan perintah sesudah penggantian pointer mengembalikan pointer lama. Status pekerjaan/runtime/restore tidak pasti dan sinkronisasi aktif menolak instalasi/uninstall. Model/data sentinel tetap utuh. Tidak ada git pull/reset atau overwrite checkout.
- Smoke installer nyata Python 3.10.12 melewati probe backend/metadata versi/frontend/aset memakai direktori data/config sementara sebelum aktivasi, kemudian startup/shutdown/reinstall/uninstall aktual. Suite mock/regresi dan build juga memverifikasi Python 3.12.14; smoke installer 3.12.14 untuk fitur versi dijalankan terpisah sebagaimana hasil tabel. Probe tidak membaca token/data pengguna atau menghubungi Google/GitHub Release.
- Catatan release yang menyerupai HTML/script ditampilkan sebagai teks pada browser. Screenshot adalah fixture, bukan bukti bahwa release 1.2.0 tersedia di GitHub.

## Analisis format dan pemisahan sumber CTH 1.1.1

- Fixture identitas yang sama pada teks biasa dan pasangan USER/ASSISTANT menghasilkan hitungan yang jujur: fakta dokumenter dapat dikutip; pasangan QA terdeteksi sebagai latihan tetapi bukan sumber. Percakapan campuran, self-claim identitas palsu dan instruksi assistant tetap disaring, termasuk saat file diberi peran facts_only.
- Analisis melaporkan pasangan QA, blok/unit latihan sebelum tokenizer, kutipan unik, baris/alasan yang dilewati dan peran berkas. Unit awal bukan klaim jumlah sequence training final. Uji persiapan memakai tokenizer palsu dan SQLite membuktikan counts train/eval sesuai sampel yang disimpan, serta facts_only tidak dikirim ke tokenizer latihan; tidak ada Trainer/optimizer/model/GPU yang dijalankan.
- Backend mewajibkan pengakuan eksplisit untuk latihan dengan indeks kosong dan menolak pemilihan sumber-fakta saja sebagai training. Cache analisis yang dipalsukan atau rusak tidak menutupi pemeriksaan baru di endpoint start. Byte/checksum asli tetap sama setelah analisis dan perubahan peran; proyek tetap terisolasi.
- Pemulihan QA-only membuat indeks valid tetapi kosong dan menyimpan laporan nol kutipan. Status/hasil pemulihan menampilkan peringatan, bukan klaim sumber siap. Model Testing membedakan hitungan sumber dari jumlah sampel aktual run yang tercatat atau belum tersedia.
- Overlay fakta untuk model tersimpan diverifikasi melalui Drive mock dan memicu begin_restore, bukan prepare_job/training. Catatan current/history training tetap tidak berubah. Pemulihan hanya mengganti indeks dan penandanya setelah memeriksa checksum bobot lama; berkas asli/model/manifest training dipertahankan. Tindakan ini hanya terjadi sesudah pilihan fakta dan refresh_sources eksplisit.
- Browser menguji peringatan dan tombol start yang tertahan untuk QA-only, hilangnya peringatan setelah fakta terpisah dipilih, QA yang tetap nol kutipan meskipun perannya diubah, serta tombol Perbarui sumber tanpa training. Seluruh API diintersep mock dan tidak ada POST training/start pada skenario tersebut. Screenshot merupakan fixture, bukan bukti inferensi model nyata.

## QA terkurasi eksplisit CTH 1.1.2

- Menambah `curated_qa` sebagai persetujuan per berkas, sementara default/filtered, training_only dan facts_only tetap menolak raw assistant. Peran tersimpan bersama checksum pada manifest versi dataset; perubahan peran tidak mengubah byte/checksum atau salinan Drive. Cache analisis memakai versi parser 2 agar laporan lama dihitung ulang.
- Fixture identitas sama dalam teks biasa dan QA disetujui memberikan jawaban persis yang sama beserta nama sumber dan offset passage. USER/ASSISTANT dan pertanyaan tidak muncul dalam jawaban. Pertanyaan hanya menjadi metadata pencarian untuk jawaban satu kalimat; jawaban beberapa kalimat harus relevan per kalimat. Uji pasangan lain yang tidak terkait memastikan tidak ikut dikutip. Persetujuan bukan validasi kebenaran semantik atau bukti kualitas model generatif.
- Pasangan tanpa persetujuan, jawaban orphan/SYSTEM, pasangan terlalu panjang, instruksi di pertanyaan/jawaban termasuk setelah heading Fakta, percakapan campuran, pertanyaan tanpa bukti dan QA multiline diuji offline. Teks asli tetap digunakan untuk persiapan latihan; tidak ada transformasi TXT diam-diam.
- Backend start tetap memerlukan verifikasi Drive: status gagal ditolak walau QA sudah disetujui; QA terverifikasi dengan laporan positif dapat membentuk manifest lewat thread mock. Peringatan/acknowledgment indeks kosong dan isolasi proyek tetap diuji. Pembaruan sumber model memerlukan `refresh_sources` eksplisit, menerima QA disetujui, tidak memanggil prepare_job dan tidak mengubah riwayat training. Rebuild indeks QA diuji dengan download Drive mock.
- 113 tes lulus pada Python 3.10.12 dan 3.12.14. Build produksi dan Chromium menguji pilihan QA eksplisit serta pembaruan sumber model dengan API mock. Tidak ada training, GPU, akses Drive/Colab nyata, atau release yang diterbitkan. Smoke installer nyata yang tercatat di tabel dijalankan pada patch 1.1.1; perubahan 1.1.2 tidak mengubah installer atau dependency.

## Batas bukti

Python 3.10.12 lokal cloud teruji; instalasi pada laptop Linux Mint/RAM 4 GB pengguna tetap belum diverifikasi. OAuth desktop/browser sistem, izin Google, Colab API, sesi/T4, kualitas adapter, training/resume penuh, cadangan Drive nyata, dan inferensi GPU tetap memerlukan pengujian pengguna dengan akun sendiri. Tes mock tidak membuktikan integrasi itu. Launcher produksi dan HTTP lokal sudah diuji dengan dependensi nyata.

Resume web dan konversi/unduhan GGUF penuh belum ditambahkan. Resume CLI memerlukan checkpoint baru dengan checksum state lengkap serta kecocokan kode/data/config; checkpoint lama tidak otomatis dianggap dapat melanjutkan persis. Percakapan terisolasi di UI, tetapi model masih memproses setiap pertanyaan secara mandiri.
