import { useEffect, useRef, useState } from "react";
import { useWorkspace } from "../context/WorkspaceContext";
import { Badge, Empty, Notice, PageTitle } from "../components/UI";
import SourceAnalysis from "../components/SourceAnalysis";
import { analyzeDatasets, setSourceMode, previewDataset, restoreDataset } from "../services/datasets";

const DRIVE_LABELS = {
  pending: "Drive: menunggu sinkronisasi",
  syncing: "Drive: mengunggah…",
  verifying: "Drive: memverifikasi ID, folder, ukuran, checksum…",
  saved: "Drive terverifikasi",
  failed: "Drive: gagal — coba sinkronkan kembali",
};
function sizeLabel(size) {
  return size >= 1048576 ? `${(size / 1048576).toFixed(1)} MB` : `${(size / 1024).toFixed(1)} KB`;
}

export default function Datasets({ onNavigate }) {
  const {
    selectedProject, datasets, datasetsLoading, datasetsError,
    refreshDatasets, uploadDatasetFile, addDatasetUrl, removeDataset, retryDatasetSync,
  } = useWorkspace();
  const projectId = selectedProject?.id;
  const [url, setUrl] = useState("");
  const [error, setError] = useState("");
  const [message, setMessage] = useState("");
  const [busy, setBusy] = useState("");
  const [preview, setPreview] = useState(null);
  const activeProject = useRef(projectId);
  const locked = useRef(false);
  const mounted = useRef(false);
  activeProject.current = projectId;

  useEffect(() => {
    mounted.current = true;
    return () => { mounted.current = false; };
  }, []);
  useEffect(() => {
    setPreview(null);
    setUrl("");
    setError("");
    setMessage("");
  }, [projectId]);
  useEffect(() => {
    if (!busy) return;
    const warn = event => { event.preventDefault(); event.returnValue = ""; };
    window.addEventListener("beforeunload", warn);
    return () => window.removeEventListener("beforeunload", warn);
  }, [busy]);

  async function perform(label, task) {
    if (locked.current || !projectId) return;
    const id = projectId;
    locked.current = true;
    setBusy(label);
    setError("");
    setMessage("");
    const current = () => mounted.current && activeProject.current === id;
    try {
      await task(id, current);
    } catch (err) {
      if (current()) setError(err.message);
    } finally {
      locked.current = false;
      if (mounted.current) setBusy("");
    }
  }

  function readFile(event) {
    const file = event.target.files?.[0];
    event.target.value = "";
    if (!file) return;
    if (!/\.txt$/i.test(file.name) || !file.size) {
      setError("Pilih berkas TXT yang tidak kosong.");
      return;
    }
    perform("Menyimpan TXT ke backend…", async (id, current) => {
      const result = await uploadDatasetFile(id, file);
      if (current()) setMessage(result.duplicate
        ? "TXT ini sudah terdaftar. Salinan yang tersimpan digunakan kembali."
        : "TXT tersimpan di disk backend. Sinkronisasi Drive berjalan terpisah; kamu boleh memuat ulang setelah pesan ini muncul.");
    });
  }

  function addUrl(event) {
    event.preventDefault();
    perform("Menyimpan alamat…", async (id, current) => {
      const result = await addDatasetUrl(id, url.trim());
      if (current()) {
        setUrl("");
        setMessage(result.duplicate ? "URL ini sudah tersimpan." : "Alamat tersimpan. Isi situs belum diambil.");
      }
    });
  }

  function view(source) {
    perform("Membuka pratinjau…", async (id, current) => {
      if (!source.local_saved) {
        await restoreDataset(id, source.id);
        await refreshDatasets(id);
      }
      const result = await previewDataset(id, source.id);
      if (current()) setPreview(result);
    });
  }

  function remove(source) {
    if (!window.confirm(`Hapus "${source.name}" dari daftar proyek? Salinan di disk dan Drive tetap disimpan.`)) return;
    perform("Menghapus dari daftar…", async (id, current) => {
      const result = await removeDataset(id, source.id);
      if (current()) {
        setMessage(result.message);
        setPreview(previous => previous?.id === source.id ? null : previous);
      }
    });
  }

  function retry(source) {
    perform("Meminta sinkronisasi…", async (id, current) => {
      await retryDatasetSync(id, source.id);
      if (current()) setMessage("Sinkronisasi diminta. Salinan lokal tetap tersedia.");
    });
  }

  return (
    <>
      <PageTitle
        title="Datasets"
        description="Sumber teks dan URL tersimpan per proyek, tetap tersedia setelah aplikasi dibuka ulang."
        action={<Badge tone="info">Penyimpanan persisten</Badge>}
      />
      <Notice>
        TXT disimpan ke disk backend terlebih dahulu, lalu disinkronkan ke Drive.
        Tunggu pesan tersimpan sebelum menutup tab. Status hijau menandakan salinan Drive sudah terverifikasi.
      </Notice>

      {!selectedProject ? (
        <section className="cth-card">
          <Empty icon="datasets" title="Pilih proyek terlebih dahulu">Buat atau pilih proyek melalui Google Drive.</Empty>
          <button className="cth-button primary" onClick={() => {
            if (onNavigate) onNavigate("drive");
            else window.location.hash = "drive";
          }}>Buka Google Drive</button>
        </section>
      ) : (
        <>
          <div className="cth-grid two cth-section-gap">
            <section className="cth-card">
              <h2>Masukkan TXT</h2>
              <p className="cth-muted">Proyek: {selectedProject.name}</p>
              <label className="cth-upload">
                <strong>{busy || "Pilih berkas TXT"}</strong>
                <span>UTF-8 · maksimal 64 MiB</span>
                <input aria-label="Pilih berkas TXT" type="file" accept=".txt,text/plain" disabled={Boolean(busy)} onChange={readFile} />
              </label>
              <p className="cth-muted">Berkas dikirim langsung ke backend tanpa membaca seluruh isinya ke RAM browser. Kapasitas disk tetap membatasi penyimpanan.</p>
            </section>
            <section className="cth-card">
              <h2>Tambahkan URL</h2>
              <p className="cth-muted">Alamat disimpan per proyek. Pengambilan isi situs belum diintegrasikan.</p>
              <form className="cth-form" onSubmit={addUrl}>
                <label>Domain atau alamat halaman
                  <input value={url} onChange={event => setUrl(event.target.value)} maxLength={2048}
                    placeholder="example.com atau https://example.id/artikel" required disabled={Boolean(busy)} />
                </label>
                <button className="cth-button" type="submit" disabled={Boolean(busy)}>Simpan alamat</button>
              </form>
            </section>
          </div>

          {(error || datasetsError) && <Notice tone="error">{error || datasetsError}</Notice>}
          {message && <Notice>{message}</Notice>}

          <div className="cth-grid two cth-section-gap">
            <section className="cth-card">
              <div className="cth-card-heading">
                <h2>Sumber proyek</h2>
                <Badge>{datasets.length} sumber tersimpan</Badge>
              </div>
              <button className="cth-text-button" disabled={Boolean(busy) || datasetsLoading}
                onClick={() => refreshDatasets(projectId)}>{datasetsLoading ? "Memuat daftar…" : "Muat ulang daftar"}</button>
              {!datasets.length ? (
                <Empty icon="datasets" title={datasetsLoading ? "Memuat dataset…" : "Belum ada sumber tersimpan"} />
              ) : (
                <div className="cth-source-list">
                  {datasets.map(source => (
                    <article key={source.id}>
                      <div style={{ minWidth: 0 }}>
                        <Badge>{source.type.toUpperCase()}</Badge>
                        <strong style={{ overflowWrap: "anywhere" }}>{source.name}</strong>
                        <small>{source.type === "txt" ? sizeLabel(source.size) : "Alamat saja · isi belum diambil"} · {source.local_saved ? "Tersimpan di perangkat" : "Salinan lokal belum tersedia"}</small>
                        <Badge tone={source.drive_status === "saved" ? "success" : source.drive_status === "failed" ? "warning" : "info"}>
                          {DRIVE_LABELS[source.drive_status] || "Drive belum terverifikasi"}
                        </Badge>
                        {source.error && <small>{source.error}</small>}
                        {source.type === "txt" && <>
                          <label>Peran berkas<select aria-label={`Peran ${source.name}`} value={source.source_mode || "filtered"} disabled={Boolean(busy)} onChange={event => {
                            const mode = event.target.value;
                            perform("Mengubah peran sumber…", async id => { await setSourceMode(id, source.id, mode); await refreshDatasets(id); });
                          }}>
                            <option value="filtered">Latihan + sumber tersaring</option><option value="training_only">Khusus percakapan/latihan</option><option value="facts_only">Fakta proyek saja (tanpa training)</option>
                          </select></label>
                          {source.analysis && <SourceAnalysis report={{ ...source.analysis, files: [source.analysis] }} />}
                          <small>Peran tidak mengubah isi TXT. Fakta proyek tetap melewati filter; jawaban ASSISTANT tidak otomatis dimasukkan. Sumber model lama diperbarui secara eksplisit di Model Testing.</small>
                        </>}

                      </div>
                      <div className="cth-actions" style={{ flexWrap: "wrap" }}>
                        {source.type === "txt" && <>
                          <button className="cth-text-button" disabled={Boolean(busy)} onClick={() => view(source)}>Lihat</button>
                          <button className="cth-text-button" disabled={Boolean(busy) || !source.local_saved} onClick={() => perform("Menganalisis sumber…", async id => { await analyzeDatasets(id, [source.id]); await refreshDatasets(id); })}>Analisis sumber</button>
                        </>}
                        {source.drive_status === "failed" && <button className="cth-text-button" disabled={Boolean(busy)} onClick={() => retry(source)}>Coba sinkronkan</button>}
                        <button className="cth-text-button danger" disabled={Boolean(busy)} onClick={() => remove(source)}>Hapus dari daftar</button>
                      </div>
                    </article>
                  ))}
                </div>
              )}
            </section>
            <section className="cth-card">
              <h2>Pratinjau teks</h2>
              {preview ? (
                <>
                  <p className="cth-muted">{preview.name} · {preview.truncated ? "Menampilkan 12.000 karakter pertama" : "Seluruh teks ditampilkan"}</p>
                  <pre className="cth-text-preview">{preview.content}</pre>
                </>
              ) : <Empty icon="datasets" title="Pilih TXT untuk dibaca">Pratinjau diambil saat kamu menekan Lihat.</Empty>}
            </section>
          </div>
          <Notice>
            TXT yang pernah diunggah lewat CTH dicoba dimuat dari bukti upload lama, sehingga tidak perlu dipilih lagi.
            Training memakai TXT yang berstatus tersimpan di Drive. URL belum dipakai untuk training.
            Data tersimpan di direktori aplikasi per pengguna, terpisah dari repositori. Gunakan cadangan lokal saat memindahkannya.
          </Notice>
        </>
      )}
    </>
  );
}
