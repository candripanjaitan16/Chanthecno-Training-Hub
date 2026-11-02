import { useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import "./UpdateStatus.css";

const API = (import.meta.env.VITE_API_BASE_URL || (import.meta.env.DEV ? "http://127.0.0.1:8000" : "")).replace(/\/$/, "");

async function applicationRequest(path, options = {}) {
  const controller = new AbortController();
  const abort = () => controller.abort();
  if (options.signal?.aborted) controller.abort();
  options.signal?.addEventListener("abort", abort, { once: true });
  const timer = setTimeout(abort, 25000);
  try {
    const response = await fetch(`${API}/api/app${path}`, {
      ...options, signal: controller.signal, cache: "no-store",
      headers: { Accept: "application/json", ...options.headers },
    });
    if (!response.ok) throw new Error("Status aplikasi belum dapat diperiksa atau disimpan.");
    return await response.json();
  } finally {
    clearTimeout(timer);
    options.signal?.removeEventListener("abort", abort);
  }
}

export default function UpdateStatus() {
  const [application, setApplication] = useState(null);
  const [update, setUpdate] = useState({ status: "not_checked", message: "Pembaruan belum diperiksa." });
  const [notice, setNotice] = useState(null);
  const [open, setOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [seenError, setSeenError] = useState(false);
  const dialog = useRef(null);
  const checking = useRef(false);
  const seenAttempt = useRef(false);

  useEffect(() => {
    const controller = new AbortController();
    applicationRequest("/status", { signal: controller.signal }).then(value => {
      if (controller.signal.aborted) return;
      setApplication(value);
      setUpdate(value.update);
      if (value.notice) { setNotice(value.notice); setOpen(true); }
    }).catch(() => { if (!controller.signal.aborted) setError("Versi terpasang belum dapat diperiksa. Muat ulang aplikasi."); });
    return () => controller.abort();
  }, []);

  useEffect(() => {
    if (open && !dialog.current?.open) dialog.current?.showModal();
    if (!open && dialog.current?.open) dialog.current.close();
  }, [open]);

  async function acknowledge() {
    if (!notice) return;
    try {
      await applicationRequest("/seen", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ version: notice.version }) });
      setSeenError(false);
    } catch { setSeenError(true); }
  }

  useEffect(() => {
    if (!open || !notice || seenAttempt.current) return;
    seenAttempt.current = true;
    // The notice has been committed to the UI; installation/download alone never acknowledges it.
    applicationRequest("/seen", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ version: notice.version }) })
      .catch(() => setSeenError(true));
  }, [open, notice]);

  async function check() {
    setOpen(true);
    if (checking.current) return;
    checking.current = true;
    setBusy(true);
    setError("");
    try { setUpdate(await applicationRequest("/check-updates", { method: "POST" })); }
    catch { setUpdate({ status: "unknown", message: "Pemeriksaan gagal. Status pembaruan belum diketahui." }); }
    finally { checking.current = false; setBusy(false); }
  }

  function close() { setOpen(false); setNotice(null); }
  const release = update?.release;
  return <div className="cth-update-footer">
    <span>CTH {application?.version || "· versi belum diketahui"}</span>
    <button type="button" onClick={check} disabled={busy}>{busy ? "Memeriksa…" : "Periksa pembaruan"}</button>
    {error && <small role="status">{error}</small>}
    {createPortal(<dialog className="cth-update-dialog" ref={dialog} aria-labelledby="cth-update-title" onCancel={close}>
      <header><h2 id="cth-update-title">{notice?.message || "Versi dan pembaruan CTH"}</h2><button type="button" onClick={close} aria-label="Tutup panel pembaruan">×</button></header>
      <p>Versi berjalan: <strong>{application?.version || "belum dapat diperiksa"}</strong>{application?.installation === "checkout" ? " · checkout pengembangan" : ""}</p>
      {notice && <section><h3>Yang baru</h3><ul>{notice.changes.map((item, index) => <li key={index}>{item}</li>)}</ul></section>}
      {seenError && <p role="alert">Pemberitahuan belum dapat ditandai dilihat; dapat muncul kembali. <button type="button" onClick={acknowledge}>Tandai sudah dilihat</button></p>}
      <p role="status">{busy ? "Memeriksa GitHub Release stabil…" : update?.message}</p>
      {update?.checked_at && <p className="cth-update-muted">Diperiksa: {new Date(update.checked_at * 1000).toLocaleString("id-ID")}{update.cached ? " · hasil tersimpan" : ""}. Pemeriksaan berikutnya tersedia setelah {new Date(update.next_check_at * 1000).toLocaleString("id-ID")}.</p>}
      {update?.status === "available" && release && <section>
        <h3>Pembaruan ke {release.version}</h3>
        <h4>Ringkasan perubahan</h4><pre>{release.notes || "Release ini tidak menyertakan catatan perubahan."}</pre>
        {release.notes_truncated && <p>Ringkasan dipotong; baca catatan lengkap di GitHub.</p>}
        <a href={release.url} target="_blank" rel="noopener noreferrer">Buka release di GitHub</a>
        <h4>Cara memperbarui</h4>
        <p>Selesaikan pekerjaan dan pastikan statusnya diketahui, lalu tutup CTH. Gunakan checkout baru di direktori kosong; perintah ini tidak menjalankan git pull pada repository Anda.</p>
        <pre aria-label="Petunjuk pembaruan">{release.commands.join("\n")}</pre>
        <p>Pakai interpreter Python yang sesuai saat menjalankan installer. Data, token, konfigurasi, dan model tetap disimpan di direktori pengguna. Jika instalasi gagal, jalankan kembali versi sebelumnya. Unduhan selesai belum berarti versi baru sudah berjalan.</p>
      </section>}
      {!notice && application?.changes && <details><summary>Yang baru di CTH {application.version}</summary><ul>{application.changes.map((item, index) => <li key={index}>{item}</li>)}</ul></details>}
      <p className="cth-update-muted">Pemeriksaan hanya meminta metadata release publik. Token Google dan data proyek tidak dikirim.</p>
    </dialog>, document.body)}
  </div>;
}
