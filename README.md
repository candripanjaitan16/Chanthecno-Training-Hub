# Chanthecno Training Hub

Versi aplikasi: **1.1.2**. Ini versi kode/build; bukan pernyataan bahwa GitHub Release 1.1.2 sudah diterbitkan.

Dashboard lokal untuk dataset proyek, fine-tuning LoRA di Google Colab T4, cadangan Google Drive, pemulihan model, dan pengujian chat. Aplikasi memakai React/Vite, FastAPI, dan SQLite. Model dasar tetap `Qwen/Qwen2.5-0.5B-Instruct`; nama model proyek adalah nama adapter hasil fine-tuning, bukan model yang dilatih dari nol.

## Instalasi dari GitHub (Linux, tanpa sudo)

Prasyarat: Git, Python **3.10+** dengan modul `venv`/pip, dan Node.js **22.12+ atau 24 LTS** beserta npm. Dependency yang dipatok, suite backend, dan installer produksi diuji memakai Python **3.10.12** serta Python 3.12.14 (cloud Linux), dengan Node 24.19.0/npm 11.9.0. Hasil ini bukan bukti instalasi pada laptop Linux Mint pengguna. Pada Linux Mint, pastikan paket Python menyediakan venv sebelum mulai. Installer tidak menjalankan sudo atau memasang paket sistem. Gunakan versi Node yang memenuhi batas Vite; Node bawaan Mint yang lebih tua mungkin perlu diperbarui oleh pengguna.

```bash
git clone https://github.com/candripanjaitan16/Chanthecno-Training-Hub.git
cd Chanthecno-Training-Hub
python3 tools/install.py
```

Installer menyalin sumber **yang terlacak Git**, membuat virtualenv backend pada rilis terpisah, memasang `backend/requirements.txt`, menjalankan `npm ci` dengan lockfile, membangun frontend produksi, lalu memasang perintah untuk pengguna saat ini. Perubahan sumber yang belum terlacak tidak ikut; pengembang perlu menambahkannya ke Git terlebih dahulu. Kegagalan pemasangan/build tidak mengganti rilis aktif. Sumber autentikasi tidak ikut disalin; jika nama berkas rahasia terlacak ditemukan, instalasi ditolak tanpa membaca isinya.

Distribusi aplikasi melalui GitHub. npm hanya dipakai untuk membangun frontend saat instalasi; **tidak ada paket npm/npx aplikasi ini yang diterbitkan atau perlu dipasang**. Runtime lokal tidak menjalankan Vite, Node, PyTorch, atau model besar. Beban model tetap di Colab; build frontend memerlukan memori sementara, sedangkan dashboard memakai satu proses server lokal. Kinerja pada laptop RAM 4 GB belum diukur langsung.

Jika `chanthecno-hub` belum ditemukan, tambahkan baris berikut ke `~/.profile`, kemudian buka terminal baru. Untuk terminal saat ini, jalankan baris yang sama:

```bash
export PATH="$HOME/.local/bin:$PATH"
```

Installer memberikan petunjuk ini bila direktori bin belum ada dalam PATH dan tidak mengubah file shell pengguna secara otomatis.

Python 3.10.12 dapat dipakai dengan dependency saat ini; tidak perlu mengganti Python sistem. Google API Core menampilkan FutureWarning bahwa pembaruan SDK berikutnya tidak lagi mendukung Python 3.10. Ini keterbatasan dukungan upstream, meskipun versi yang dipatok dan tes saat ini berjalan. Bila ingin interpreter lebih baru terpisah, misalnya Python 3.12 yang dikelola `uv`:

```bash
python3 -m pip install --user uv
"$HOME/.local/bin/uv" python install 3.12
cth_python="$("$HOME/.local/bin/uv" python find 3.12)"
"$cth_python" tools/install.py
```

Perintah ini menambahkan interpreter pengguna, tidak mengganti `/usr/bin/python3`. Installer memakai interpreter yang menjalankannya untuk virtualenv backend. Tidak ada dependency backend yang diturunkan versinya untuk pengujian 3.10; dependency training/GPU di Colab diuji terpisah dan tidak termasuk klaim kompatibilitas dashboard lokal.


## Menjalankan dan keluar

```bash
chanthecno-hub
```

Launcher menjalankan backend dan menyajikan frontend produksi melalui **http://127.0.0.1:8000**, lalu membuka browser. Swagger tersedia pada `/docs`. Jika browser tidak tersedia, launcher menampilkan alamat untuk dibuka manual. `chanthecno-hub --no-browser` menonaktifkan pembukaan browser. Menutup tab browser tidak menghentikan server; tekan **Ctrl+C di terminal** untuk keluar. SIGTERM juga ditangani. Launcher hanya menghentikan grup proses yang dibuatnya sendiri, menolak port yang sudah terpakai, dan tidak menghentikan runtime Colab ketika keluar.

Server mengikat loopback, memeriksa host/origin dan akses lokal. Ini aplikasi satu pengguna lokal; jangan mengeksposnya melalui reverse proxy publik. Menghentikan server lokal tidak membuktikan pekerjaan Colab berhenti. Setelah koneksi terputus, periksa status sebelum memulai pekerjaan baru.

## Lokasi program, data, dan konfigurasi

| Isi | Lokasi default |
| --- | --- |
| Perintah | `~/.local/bin/chanthecno-hub` |
| Rilis dan virtualenv | `~/.local/share/chanthecno-hub/program/releases/` |
| Rilis aktif | `~/.local/share/chanthecno-hub/program/current` |
| SQLite, dataset, status, workspace, salinan model | `~/.local/share/chanthecno-hub/data/` |
| Konfigurasi OAuth dan token akun pengguna | `~/.config/chanthecno-hub/` |
| Pengunci proses dan cadangan reset lokal | `~/.local/state/chanthecno-hub/` |

Mengikuti `XDG_DATA_HOME`, `XDG_CONFIG_HOME`, dan `XDG_STATE_HOME`. Untuk pengujian/konfigurasi khusus, `CTH_DATA_DIR`, `CTH_CONFIG_DIR`, `CTH_STATE_DIR` harus berupa path absolut dan konsisten pada setiap pemanggilan. `CTH_BIN_DIR` dapat mengganti direktori perintah untuk pengujian; default tetap `~/.local/bin`. Jangan menempatkan data di dalam source, direktori konfigurasi, atau direktori cadangan.

Database dan token versi lama di folder backend **tidak dibaca, dipindahkan, atau dihapus otomatis**. Instalasi baru dimulai dengan lokasi data per pengguna. Simpan checkout lama bila masih berisi data yang dibutuhkan; sambungkan ulang akun Google sendiri. Tidak ada token pengembang yang didistribusikan. Menghapus checkout unduhan setelah instalasi tidak menghapus data atau merusak rilis yang terpasang.

## Google OAuth: akun milik setiap pengguna

Buat konfigurasi OAuth **Desktop app** milik sendiri dengan akses Drive API dan izin Colab yang tersedia bagi akun tersebut. Letakkan file konfigurasi pengguna sendiri bernama `credentials.json` di `~/.config/chanthecno-hub/`. Jangan menaruhnya dalam checkout, frontend, `VITE_*`, dataset, atau lampiran publik. Direktori konfigurasi harus privat (0700); file konfigurasi/token sebaiknya 0600. File yang dibuat aplikasi ditulis secara atomik dengan izin privat.

```bash
chanthecno-hub connect-drive
chanthecno-hub connect-colab
```

Jalankan dari sesi desktop pengguna: OAuth membuka browser dan memakai callback loopback sementara. Drive menggunakan scope `https://www.googleapis.com/auth/drive.file`; Colab menggunakan `https://www.googleapis.com/auth/colaboratory`. Keduanya terpisah. Token disimpan hanya untuk pengguna tersebut setelah pemeriksaan akses terkait. Pemeriksaan Colab melakukan **GET daftar runtime**, bukan alokasi GPU/training. Respons ditolak/tidak valid tidak dinyatakan sebagai akses siap. Aplikasi API tidak memulai login interaktif otomatis ketika token belum tersedia.

Akses Colab API, kuota, sesi, dan GPU tetap bergantung pada akun serta layanan Google. Otentikasi berhasil bukan bukti GPU atau training berjalan. Untuk jaringan terbatas, konfigurasi akses ke OAuth Google, Drive API, Colab API, dan hostname runtime yang benar melalui pengaturan jaringan yang didukung; jangan menonaktifkan TLS atau mempublikasikan token. Jangan mengirim nilai token ke chat atau issue.

## Pembaruan dan uninstall

Versi terpasang tampil di bawah sidebar. Tombol **Periksa pembaruan** meminta endpoint publik GitHub Release stabil terbaru untuk repositori ini. Hanya respons valid dengan versi stabil yang dapat menghasilkan status “sama dengan release terbaru”. Jika belum ada release, jaringan/timeout gagal, API menolak akses, atau tag tidak dapat dibandingkan, UI menampilkan kondisi itu; tidak mengklaim aplikasi terbaru. Versi pengembangan yang lebih tinggi daripada release stabil diberi status terpisah.

Pemeriksaan tidak memakai sesi Google, token akun, cookie, dataset, atau data proyek. Permintaan hanya memuat URL repositori tetap, header API publik dan versi CTH sebagai User-Agent; redirect tidak diikuti, TLS tetap diverifikasi. Timeout socket dan anggaran pembacaan 8 detik, respons maksimal 256 KiB, catatan release dibatasi 24.000 karakter dan ditampilkan sebagai teks. Hasil disimpan 15 menit; kegagalan dapat diperiksa kembali setelah setidaknya 60 detik. Respons 403/429 yang menyatakan batas API menghormati Retry-After/reset GitHub (dibatasi 24 jam). Beberapa tab tidak menjalankan pemeriksaan paralel.

Jika tersedia pembaruan, panel menampilkan versi tujuan, catatan release dan perintah untuk **checkout baru**, bukan menjalankan `git pull` pada repository pengguna. Selesaikan pekerjaan, pastikan statusnya aman, kemudian tutup server. Ganti `vX.Y.Z` dengan tag stabil yang benar-benar dilaporkan GitHub:

```bash
git clone --branch vX.Y.Z --single-branch https://github.com/candripanjaitan16/Chanthecno-Training-Hub.git cth-update-vX.Y.Z
cd cth-update-vX.Y.Z
python3 tools/install.py
chanthecno-hub
```

Gunakan direktori tujuan yang belum ada; jangan menimpa checkout atau perubahan lokal lama. Tidak ada perintah Git otomatis yang memodifikasi repository pengguna. Gunakan interpreter Python terpisah bila diperlukan sesuai bagian instalasi.

Installer menolak pembaruan/uninstall saat launcher/API masih hidup, training/pemulihan/pencadangan aktif atau tidak pasti, sinkronisasi masih berjalan, atau catatan status rusak. Ini pemeriksaan lokal yang konservatif, bukan pemeriksaan Google baru. Periksa pekerjaan memakai versi lama dan selesaikan statusnya dahulu; jangan menghapus file status untuk melewati pengaman.

Rilis baru dipasang terpisah; virtualenv tidak dipindah setelah dibuat. Sesudah dependency/build berhasil, installer menyalakan backend baru di direktori data/config sementara, memeriksa versi HTTP dan frontend/aset, lalu menghentikan proses probe miliknya. Baru setelah itu tautan rilis aktif diganti secara atomik. Kegagalan dependency/build/startup tidak mengganti rilis aktif; kegagalan pemasangan tautan perintah mengembalikan tautan sebelumnya. Rilis lama, database, dataset, konfigurasi, token dan model tetap dipertahankan. Probe tidak memakai data pengguna atau layanan Google.

**Unduhan/build/probe selesai bukan pemberitahuan update berhasil.** Sesudah pengguna menjalankan rilis baru dan API versi tersebut benar-benar melayani UI, aplikasi menampilkan **“Berhasil diperbarui ke versi …”** dan panel **“Yang baru”**. Setelah panel dirender, UI mencatat versi yang telah dilihat pada `data/updates/seen.json`; restart berikutnya tidak mengulang pemberitahuan. Jika penyimpanan gagal, UI menjelaskan bahwa pemberitahuan bisa muncul kembali dan menyediakan retry. Instalasi pertama serta pemasangan ulang versi yang sama tidak dianggap update. Cache pemeriksaan tersimpan di `data/updates/check.json`. Catatan versi terpasang berasal dari `release-notes.json` yang ikut installer, bukan dari release GitHub yang belum dijalankan.

Nomor versi utama berada di `backend/app/version.py`; API, `chanthecno-hub --version` dan `python3 tools/install.py --version` memakai nilai itu. Metadata frontend, catatan versi dan dokumentasi diselaraskan serta diuji. Penerbitan release tetap tindakan terpisah; pengujian fitur ini tidak menerbitkan release sungguhan.

```bash
chanthecno-hub uninstall
# Alternatif dari checkout:
python3 tools/install.py --uninstall
```

Uninstall menghapus rilis program dan tautan perintah milik installer, **mempertahankan seluruh data, konfigurasi/token, dan cadangan pengguna secara default**. Tidak ada operasi Google Drive/Colab. Tidak ada opsi penghapusan rahasia/data otomatis. Checkout GitHub yang diunduh juga dipertahankan.

## Dataset, status training, dan cadangan

- TXT UTF-8 disimpan bertahap ke disk backend dengan metadata SQLite, terisolasi per proyek; refresh/restart tidak menghilangkannya. Batas 64 MiB per TXT. Nama/path, NUL, teks kosong, dan duplikasi per proyek diperiksa.
- Alur wajib: **upload browser → menyimpan lokal → mengunggah Drive → memverifikasi → siap dipilih untuk training**. UI membedakan menunggu, mengunggah, memverifikasi, terverifikasi, dan gagal. Retry pada halaman Datasets memakai berkas lokal, tanpa unggah ulang dari browser. URL hanya dicatat dan tidak diikutkan training.
- Verifikasi Drive memakai ID file yang tepat, keanggotaan pada subfolder `datasets` dari folder proyek, status tidak di-trash, ukuran, serta checksum konten API. `sha256Checksum` dipakai bila tersedia; bila tidak, `md5Checksum` dibandingkan dengan MD5 dari byte lokal. Tag `appProperties.cth_sha256` hanya untuk pencarian kandidat deduplikasi, tidak cukup sebagai bukti konten. Jika API tidak memberi checksum yang dapat dibandingkan, dataset belum siap.
- Dataset yang sama pada proyek yang sama memakai baris metadata yang sama. Upload menyimpan ID Drive yang dicadangkan **sebelum** mengirim byte dan mengunci berdasarkan proyek/checksum; retry setelah respons terputus memeriksa ID tersebut, bukan langsung membuat file baru. Kandidat file Drive yang sudah cocok juga digunakan kembali. Salinan lokal selalu dipertahankan saat gagal; tidak ada penghapusan Drive otomatis.
- Pada Training Workspace, centang dataset secara eksplisit dan periksa ringkasan nama, ukuran, ID Drive, serta SHA-256 sebelum memberi konfirmasi. Hanya dataset terpilih yang diperiksa, bukan seluruh TXT proyek. Daftar dan bukti tetap tersedia setelah restart.
- Backend menolak pilihan kosong/duplikat, URL, sumber proyek lain, versi lokal yang berubah, bukti palsu/tidak cocok, dan Drive yang belum terverifikasi. Pemeriksaan dilakukan **sebelum thread persiapan worker dibuat**, lalu diulang sebelum membuka koneksi runtime. Byte yang diunduh dari Drive untuk paket worker juga diperiksa ukuran dan SHA-256-nya. Menonaktifkan tombol UI bukan pengaman utama.
- Setiap permintaan training baru membekukan manifest proyek, dataset ID, file/folder Drive, nama, ukuran, checksum dan metode verifikasi pada `data/workspace/training-web/manifests/<job_id>.json` beserta digest pada catatan job. File dibuat sekali; perubahan digest ditolak. Manifest disalin ke request worker dan `run.json` pada hasil training agar cadangan dapat melacak versi sumber. Menambah dataset membuat pilihan/run baru, tidak mengubah manifest lama.
- Runtime dialokasikan, T4 terverifikasi, heartbeat worker, langkah optimizer, evaluasi/penyimpanan adapter, dan cadangan terverifikasi adalah bukti yang berbeda. `step == max_steps` belum berarti selesai.
- Heartbeat worker harus baru agar progres dianggap aktif. Progres lama atau putus koneksi memberi status perlu diperiksa, tidak memicu worker/training kedua. Worker versi lama tanpa heartbeat perlu diperiksa; jangan memulai ulang training aktif hanya untuk memperbarui worker.
- Pergantian proyek tidak mengambil alih runtime yang masih dipakai pekerjaan aktif/tidak pasti. Polling menjaga respons lama agar tidak menimpa pilihan baru. Kernel kontrol yang eksekusinya belum selesai tidak dihapus saat koneksi ditutup.
- Checkpoint ditulis berkala. Penanda lengkap ditulis setelah penyimpanan dan pemeriksaan state optimizer/scheduler/RNG serta adapter. Resume CLI memerlukan kecocokan konfigurasi/data/kode dan checksum; checkpoint lama tanpa checksum state tidak diklaim sebagai kelanjutan persis.
- Hentikan training meminta stop kooperatif setelah langkah optimizer dan penyimpanan checkpoint. **Stop & Simpan** berbeda: runtime hanya boleh dihentikan setelah cadangan dalam cakupan yang disetujui terverifikasi. Arsip workspace bukan otomatis model selesai atau GGUF.
- Cadangan adapter mencakup tokenizer dan metadata; hasil diperiksa dengan ukuran/checksum. Arsip lokal dipertahankan saat upload gagal. Retry cadangan tidak menjalankan training ulang. Ekstraksi membatasi entri, ukuran, path, symlink dan checksum.

## Format dataset, sampel latihan, dan sumber pencarian (1.1.2)

TXT berformat `USER:/ASSISTANT:` dideteksi sebagai percakapan QA, tetapi jawaban assistant **tidak otomatis dipercaya** sebagai fakta. Teks biasa, percakapan, dan campuran dilaporkan terpisah. Contoh berikut sengaja berbeda peran walaupun isi jawabannya sama:

```text
USER: Siapa pembuat Proyek Contoh?
ASSISTANT: Proyek Contoh dikembangkan oleh Tim Contoh.
```

Secara default pasangan itu dipakai untuk latihan tetapi menghasilkan **nol kutipan**. Setelah memeriksa semua pasangan dalam berkas, pilih **QA terkurasi disetujui** untuk mengizinkan jawabannya menjadi sumber. Alternatifnya, tulis fakta pada berkas terpisah, misalnya `fakta-proyek.txt`:

```text
Proyek Contoh dikembangkan oleh Tim Contoh.
```

Pada **Datasets → Peran berkas**, pilih secara eksplisit:

| Peran | Dipakai latihan | Dipakai indeks |
| --- | --- | --- |
| Latihan + sumber tersaring (`filtered`, default) | TXT asli | Hanya kalimat dokumenter yang lolos filter |
| Khusus percakapan/latihan (`training_only`) | TXT asli | Tidak |
| QA terkurasi disetujui (`curated_qa`) | TXT asli | Hanya jawaban pasangan USER→ASSISTANT yang lolos filter |
| Fakta proyek saja (`facts_only`) | Tidak | Kalimat dokumenter yang lolos filter |

**Persetujuan berlaku per berkas dan versi yang dipilih**, bukan otomatis untuk semua dataset. `filtered`, `training_only`, dan `facts_only` tetap tidak mengangkat jawaban ASSISTANT menjadi sumber. Dalam `curated_qa`, hanya jawaban dari pasangan USER→ASSISTANT yang lengkap diindeks; giliran SYSTEM, jawaban tanpa pertanyaan, penanda role, pertanyaan dan pasangan dengan instruksi tertanam dilewati. Jawaban dipecah menjadi kutipan kalimat, lalu dipilih menurut relevansi; pertanyaan disimpan hanya sebagai metadata pencarian untuk jawaban satu kalimat, tidak ditampilkan sebagai jawaban atau dikirim sebagai instruksi. Untuk jawaban beberapa kalimat, setiap kalimat harus relevan sendiri. Batas 8.192 karakter per pertanyaan/jawaban menjaga analisis terbatas; bagian terlalu panjang dilaporkan dilewati. Filter bukan pemeriksa kebenaran semantik atau jaminan menangkap semua prompt injection; periksa setiap pasangan sebelum menyetujui berkas. Isi latihan tidak disunting.

**Analisis sumber** pada halaman Datasets dan pilihan Training Workspace memperlihatkan format, pasangan QA terdeteksi, unit latihan awal, jumlah kutipan unik yang dapat dicari, serta nomor baris/alasan bagian yang dilewati (rincian maksimal 25 per berkas). “Dilewati” berarti tidak masuk indeks, bukan dihapus dari berkas atau diam-diam dibuang dari latihan. Perubahan peran hanya menambah metadata aplikasi; byte TXT asli, ukuran, checksum dan salinan Drive tidak ditulis ulang. Pilihan peran ikut manifest run yang dibekukan.

Jumlah sebelum tokenizer dilabeli **unit awal**, bukan jumlah sampel training final: TXT dibaca dalam blok 16.000 karakter, sedangkan pasangan QA terdeteksi dilaporkan sebagai hitungan lain. TXT QA tidak otomatis diubah menjadi format supervised QA JSONL. Jumlah sequence train/evaluasi aktual baru dihitung setelah persiapan tokenizer dan ditampilkan dari `dataset_stats.counts`; catatan run lama yang tidak memiliki angka itu ditampilkan sebagai belum tersedia, tidak ditebak dari step.

Jika kutipan nol, UI menampilkan **Indeks sumber kosong** sebelum training. Backend juga menolak start sampai pengguna secara eksplisit mengakui `acknowledge_empty_sources` bila memang ingin latihan tanpa sumber. Pilihan hanya berkas fakta tanpa unit latihan ditolak untuk training baru. Alternatif yang dianjurkan: pilih file percakapan `training_only` dan file fakta `facts_only` bersama-sama. Pemeriksaan salinan Drive tetap wajib untuk semua pilihan.

### Memperbarui sumber model lama tanpa training ulang

1. Di Datasets, pilih peran **QA terkurasi disetujui** setelah memeriksa isinya, atau tambahkan berkas **Fakta proyek saja**. Periksa analisisnya dan tunggu Drive terverifikasi.
2. Pilih model tersimpan di **Model Testing → Detail model**.
3. Centang berkas fakta atau QA terkurasi yang benar, lalu klik **Perbarui sumber tanpa training**.

Tindakan itu eksplisit: backend memeriksa proyek, peran, ukuran/checksum lokal/Drive, lalu membangun indeks inferensi baru dari fakta terpilih. Ini tidak menjalankan optimizer, tidak mengubah bobot adapter, isi TXT, riwayat job training atau manifest dataset run lama. Metadata indeks menyimpan nama, ID Drive dan checksum sumber yang dipakai. Pemulihan tetap memakai runtime proyek yang sesuai dan menolak pekerjaan aktif/tidak pasti.

Saat pemulihan, jumlah kutipan dihitung kembali. Model yang berhasil dipulihkan dengan nol kutipan diberi peringatan; keberhasilan memulihkan bobot bukan klaim sumber siap. Jika metadata hitungan belum tersedia, UI menyatakan belum dapat diverifikasi. Mode kutipan memberi penolakan tanpa bukti, sedangkan mode generatif tetap bisa memakai pengetahuan umum model yang sama dengan label yang sesuai. Tidak perlu training ulang hanya untuk memperbaiki indeks sumber.

API lokal terkait: POST `/api/datasets/projects/{id}/analysis`, PATCH `/api/datasets/projects/{id}/{source_id}/source-mode`, dan POST `/api/model-testing/restore` dengan `refresh_sources: true` serta `source_dataset_ids`. Mengirim pilihan berkas fakta tanpa konfirmasi refresh, berkas latihan sebagai overlay, atau sumber proyek lain ditolak. Data asli tidak dikonversi otomatis.

## Memakai model tersimpan tanpa training ulang

1. Pilih proyek dan buka **Model Testing**.
2. Pilih hasil training yang selesai dari **Model tersimpan**. Pekerjaan baru yang gagal tidak menutupi hasil lama; training selesai dengan backup gagal tetap dapat diperiksa.
3. Hubungkan runtime proyek yang sesuai. Bila artefak model belum ada, klik **Pulihkan & jalankan** secara eksplisit. Pemulihan menyiapkan adapter/tokenizer/indeks dan lingkungan inferensi, bukan training baru. Artefak yang sudah cocok di runtime tidak diunggah ulang.
4. Periksa status, lalu kirim pertanyaan. Ganti runtime berarti artefak perlu diperiksa/pulihkan lagi.

Nama utama chat mengikuti `model_name`; identitas model dasar tetap ada dalam metadata. Mode generatif memakai adapter dan model dasar yang sama di Colab. Retrieval memilih kalimat dokumenter relevan yang melewati filter; giliran USER/ASSISTANT/SYSTEM beserta lanjutannya, soal/latihan dan instruksi tidak dikutip. Pencocokan ini konservatif berbasis kata, sehingga dapat menolak parafrasa yang sebenarnya relevan. Asal bukti memuat nama sumber/ID Drive, passage dan offset kutipan. Indeks lama tanpa versi filter tidak dipakai untuk kutipan mentah; UI meminta Pulihkan & jalankan bila penanda indeks aman belum ada. Pemulihan membangun indeks dari TXT tanpa training ulang; pada run yang sudah ada, hanya indeks/penandanya diganti setelah checksum berkas model lain cocok. Jika tidak ada bukti dokumenter, mode kutipan menyatakan sumber tidak cukup; jalur generatif tetap dapat memakai pengetahuan umum model tersebut. Keterangan dasar jawaban menyatakan sumber yang tersedia dan kemungkinan campuran pengetahuan umum, bukan jaminan atribusi faktual setiap kalimat. Mode **Kutipan sumber** hanya kutipan kalimat dokumenter dari pencarian kata, bukan inferensi generatif. Bukti uji filter dan fallback mock tidak membuktikan kualitas adapter nyata meningkat.

Dataset diperlakukan sebagai data, bukan instruksi sistem. Penanda peran tidak boleh diteruskan sebagai lanjutan chat. Pertanyaan tidak otomatis menjadi data training. Percakapan UI dipisah berdasarkan proyek dan run; setiap pertanyaan saat ini diproses mandiri di model (belum ada memori dialog model lintas pertanyaan). Tombol Percakapan baru membersihkan tampilan. Tidak ada API AI eksternal.

TXT mentah merupakan adaptasi teks, bukan pasangan tanya jawab terawasi. Adapter dapat menghasilkan jawaban pendek/tidak akurat; jawaban terpotong ditolak dengan error, bukan diganti jawaban palsu. Perbaikan retrieval/chat template tidak membuktikan kualitas adapter. Bila diperlukan, evaluasi dataset dan contoh QA terstruktur terlebih dahulu; aplikasi tidak melatih ulang diam-diam. Kualitas model nyata belum diverifikasi pada tugas cloud ini.

## Reset lokal dan pemulihan cadangan

Reset bersifat **eksplisit**, tidak berjalan pada instalasi, startup, pembaruan, atau uninstall. Tutup server terlebih dahulu. Pekerjaan aktif, runtime masih dialokasikan, status tidak pasti, pengunci API, symlink, atau file autentikasi dalam direktori data membuat reset ditolak. Selesaikan/periksa status pekerjaan melalui aplikasi; jangan menghapus status secara manual untuk melewati pemeriksaan.

```bash
chanthecno-hub reset-local --confirm
```

Cakupan reset: database aplikasi, dataset lokal, status pekerjaan/runtime, cache dan workspace/model lokal dalam direktori data aplikasi. **Tidak mengubah konfigurasi OAuth/token, virtualenv, source, Google Drive, atau runtime Google.** Sebelum reset, aplikasi membuat cadangan terverifikasi di `~/.local/state/chanthecno-hub/backups/<id>/`. Snapshot SQLite memakai API backup agar transaksi WAL yang sudah commit tercakup. Kegagalan snapshot/checksum menghentikan reset; kegagalan inisialisasi schema mengembalikan data sebelumnya.

```bash
chanthecno-hub restore-local --backup "$HOME/.local/state/chanthecno-hub/backups/<id>" --confirm
```

Ganti `<id>` dengan lokasi yang dilaporkan reset. Restore memverifikasi manifest/checksum dan integritas SQLite, menolak symlink, lalu membuat cadangan keadaan saat ini sebelum mengganti data. Ini pemulihan data lokal, berbeda dari **Pulihkan model** di Colab. Cadangan mengandung dataset/model lokal dan perlu diperlakukan sebagai data privat.

Sesudah reset/restore, penanda generasi berubah. Saat UI dibuka ulang, hanya key browser berawalan `cth-` pada origin aplikasi yang dibersihkan; storage situs lain tidak disentuh. Jika masih membuka tab lama, muat ulang. Pilihan browser lama pada origin development berbeda dapat dibersihkan dari developer tools dengan menghapus hanya key `cth-`, bukan seluruh storage browser.

## Pengembangan dan pemeriksaan offline

Gunakan checkout yang ada. Instal dependensi pengujian tanpa mengubah lockfile:

```bash
python3 -m venv backend/.venv
backend/.venv/bin/python -m pip install -r requirements-dev.txt
npm --prefix frontend ci
npm --prefix frontend run build
backend/.venv/bin/python -m unittest discover -s tests -v
npm --prefix frontend run lint
```

Untuk menguji launcher dari checkout setelah build: `python3 chanthecno-hub --no-browser`. Untuk development HMR, jalankan backend secara terpisah dari `backend/` dengan `./.venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8000`, kemudian `npm --prefix frontend run dev -- --host localhost`. CORS mengizinkan origin development 5173 dan produksi 8000. Jangan menjalankan launcher kedua pada port yang sama. Jangan memasang dependensi training pada mesin lokal hanya untuk dashboard.

Tes memakai direktori/database sementara dan mock Google, Colab, serta inferensi berat; tidak meminta GPU, training, atau membaca token pribadi. Lihat `docs/validation.md` untuk hasil yang benar-benar dijalankan. Build dan tes mock tidak membuktikan integrasi Google atau kualitas model nyata. UI ekspor GGUF dan deployment tetap belum menyediakan konversi/deployment penuh; skrip training/ekspor CLI memerlukan pengujian terpisah. Resume training masih melalui CLI worker, belum alur pemulihan otomatis dari web.

## Struktur

- `tools/install.py`: instalasi/pembaruan/uninstall GitHub untuk pengguna saat ini.
- `chanthecno-hub`: launcher produksi dan perintah pemeliharaan/OAuth eksplisit.
- `backend/app/paths.py`, `storage.py`, `maintenance.py`: lokasi data, penulisan atomik, reset/restore offline.
- `backend/app/`: API dan layanan dataset, Drive, Colab, training, cadangan, model library/pemulihan/chat.
- `training/`: worker T4, checkpoint, persiapan data, retrieval/inferensi dan ekspor.
- `frontend/src/`: dashboard ringan navy/biru/putih; pilihan model dapat dilipat pada layar kecil. Animasi hanya mengikuti heartbeat training yang terverifikasi, berhenti saat tab tersembunyi/reduced motion.
- `tests/`: validasi offline tanpa kredensial pengguna.

Proyek masih dalam pengembangan aktif. Lisensi proyek perlu ditetapkan melalui berkas LICENSE; model dasar dan dependensi mengikuti ketentuan masing-masing.
