import { useState } from "react";
import { useWorkspace } from "../context/WorkspaceContext";
import { Badge, ConnectionCard, Empty, Notice, PageTitle } from "../components/UI";
import Icon from "../components/Icon";
export default function GoogleDrive({ onNavigate }) {
  const { drive, refreshDrive, projects, projectsLoading, projectsError, refreshProjects, selectedProject, selectProject, addProject, isCreating } = useWorkspace();
  const [form, setForm] = useState({ name: "", description: "", model_name: "" });
  const [feedback, setFeedback] = useState(null);
  const [search, setSearch] = useState("");
  async function submit(event) {
    event.preventDefault(); setFeedback(null);
    if (!form.name.trim()) return setFeedback({ tone: "error", message: "Nama proyek wajib diisi." });
    try {
      const { listUpdated } = await addProject({ name: form.name.trim(), description: form.description.trim(), model_name: form.model_name.trim() });
      setForm({ name: "", description: "", model_name: "" });
      setFeedback({ tone: listUpdated ? "success" : "warning", message: listUpdated ? "Proyek tersimpan di SQLite dan folder Drive berhasil dibuat." : "Proyek berhasil dibuat, tetapi daftar belum diperbarui. Klik Muat ulang; jangan buat ulang proyek." });
    } catch (error) { setFeedback({ tone: "error", message: error.message }); }
  }
  const filtered = projects.filter(project => project.name.toLowerCase().includes(search.toLowerCase()));
  return <>
    <PageTitle title="Google Drive" description="Buat proyek baru atau lanjutkan workspace yang sudah ada." action={<button className="cth-button" onClick={refreshDrive} disabled={drive.status === "loading"}>Periksa koneksi</button>} />
    <ConnectionCard title="Google Drive" description="Status akun lokal pada backend" {...drive} />
    <div className="cth-grid drive-layout cth-section-gap">
      <section className="cth-card"><div className="cth-card-heading"><h2>Buat proyek</h2><Badge tone="success">Backend aktif</Badge></div>
        <form className="cth-form" onSubmit={submit}>
          <label>Nama proyek<input required maxLength={120} value={form.name} onChange={e => setForm({ ...form, name: e.target.value })} placeholder="Contoh: ChanThecno Bot" disabled={isCreating} /></label>
          <label>Nama model<input maxLength={120} value={form.model_name} onChange={e => setForm({ ...form, model_name: e.target.value })} placeholder="chanthecno-cb1" disabled={isCreating} /></label>
          <label>Deskripsi<textarea rows={4} maxLength={2000} value={form.description} onChange={e => setForm({ ...form, description: e.target.value })} placeholder="Tujuan dan isi proyek" disabled={isCreating} /></label>
          <p className="cth-muted">Folder datasets, notebooks, training_results, dan models dibuat oleh backend.</p>
          <button className="cth-button primary" disabled={isCreating || drive.status !== "connected"}>{isCreating ? "Membuat folder dan menyimpan..." : "Create Project"}</button>
          {feedback && <Notice tone={feedback.tone}>{feedback.message}</Notice>}
        </form>
      </section>
      <section className="cth-card"><div className="cth-card-heading"><h2>Proyek tersimpan</h2><button className="cth-text-button" onClick={refreshProjects} disabled={projectsLoading || isCreating}>Muat ulang</button></div>
        <input className="cth-input" type="search" aria-label="Cari proyek" placeholder="Cari nama proyek..." value={search} onChange={e => setSearch(e.target.value)} />
        {projectsLoading ? <Empty title="Memuat proyek..." /> : projectsError ? <Notice tone="error">{projectsError}</Notice> : filtered.length === 0 ? <Empty title={projects.length ? "Tidak ditemukan" : "Belum ada proyek"}>Proyek yang dibuat akan tampil di sini.</Empty> : <div className="cth-drive-projects">{filtered.map(project => <article className={`cth-project-tile ${selectedProject?.id === project.id ? "selected" : ""}`} key={project.id}>
          <div className="cth-card-heading"><Icon name="drive" /><Badge tone={selectedProject?.id === project.id ? "info" : "neutral"}>{selectedProject?.id === project.id ? "Proyek aktif" : `#${project.id}`}</Badge></div>
          <h3>{project.name}</h3><p>{project.description || "Belum ada deskripsi."}</p><small>Model: {project.model_name || "Belum ditentukan"}</small>
          <div className="cth-actions"><button className="cth-button" onClick={() => { selectProject(project.id); onNavigate("datasets"); }}>Lanjutkan proyek →</button>
            {typeof project.drive_folder_id === "string" && /^[A-Za-z0-9_-]+$/.test(project.drive_folder_id) && <a className="cth-text-button" href={`https://drive.google.com/drive/folders/${encodeURIComponent(project.drive_folder_id)}`} target="_blank" rel="noopener noreferrer">Folder Drive ↗</a>}
          </div>
        </article>)}</div>}
      </section>
    </div>
    <Notice>Metadata proyek disimpan saat Create Project berhasil. Dataset tersimpan di backend; status salinan lokal dan cadangan Drive ditampilkan terpisah pada halaman Datasets. Pilihan proyek diingat di browser; isi dataset tidak disimpan di browser storage.</Notice>
  </>;
}
