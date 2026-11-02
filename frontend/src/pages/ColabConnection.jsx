import { useEffect, useRef, useState } from "react";
import { useWorkspace } from "../context/WorkspaceContext";
import { Badge, ConnectionCard, Notice, PageTitle } from "../components/UI";

const API = (
  import.meta.env.VITE_API_BASE_URL || (import.meta.env.DEV ? "http://127.0.0.1:8000" : "")
).replace(/\/$/, "");

const TERMINAL = ["idle", "completed", "failed", "interrupted"];

const VALID = [
  "disconnected",
  "provisioning",
  "allocated",
  "uncertain",
  "mismatch",
  "error",
];

const LABELS = {
  checking: "Memeriksa…",
  disconnected: "Belum terhubung",
  provisioning: "Menyiapkan T4…",
  allocated: "Runtime T4 tersedia",
  uncertain: "Permintaan perlu diperiksa",
  mismatch: "Spesifikasi berbeda",
  error: "Belum terverifikasi",
};

async function api(path, options = {}, signal, timeoutMs = 90000) {
  const controller = new AbortController();
  const abort = () => controller.abort();

  if (signal?.aborted) {
    controller.abort();
  } else {
    signal?.addEventListener("abort", abort, { once: true });
  }

  const timeout = window.setTimeout(abort, timeoutMs);

  try {
    const response = await fetch(`${API}/api/colab/${path}`, {
      ...options,
      signal: controller.signal,
      cache: "no-store",
      headers: {
        Accept: "application/json",
        ...options.headers,
      },
    });

    let data;

    try {
      data = await response.json();
    } catch {
      throw new Error(
        "Respons backend tidak sesuai. Pastikan backend sudah direstart setelah pembaruan.",
      );
    }

    if (!response.ok) {
      throw new Error(
        typeof data.detail === "string"
          ? data.detail
          : `Permintaan gagal (HTTP ${response.status}).`,
      );
    }

    return data;
  } catch (error) {
    if (error.name === "AbortError") {
      throw new Error(
        "Permintaan terputus atau melewati batas waktu. Periksa status sebelum mengulang tindakan.",
      );
    }

    if (error instanceof TypeError) {
      throw new Error(
        "Backend belum dapat dihubungi. Pastikan chanthecno-hub berjalan.",
      );
    }

    throw error;
  } finally {
    window.clearTimeout(timeout);
    signal?.removeEventListener("abort", abort);
  }
}

function jsonPost(body) {
  return {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
    },
    body: JSON.stringify(body),
  };
}

export default function ColabConnection() {
  const {
    drive,
    selectedProject,
    sources = [],
    datasets = [],
    datasetsLoading,
    datasetsError,
  } = useWorkspace();

  const projectId = selectedProject?.id;

  const [runtime, setRuntime] = useState({
    status: "checking",
  });

  const [job, setJob] = useState({
    status: "idle",
  });

  const [busy, setBusy] = useState(false);
  const [checking, setChecking] = useState(false);
  const [savingSources, setSavingSources] = useState("");
  const [error, setError] = useState("");
  const [checkedAt, setCheckedAt] = useState(null);
  const [confirmation, setConfirmation] = useState(false);
  const [confirmName, setConfirmName] = useState("");
  const [acknowledge, setAcknowledge] = useState(false);
  const [automatic, setAutomatic] = useState(true);

  const mounted = useRef(false);
  const polling = useRef(false);
  const actionBusy = useRef(false);
  const failureCount = useRef(0);
  const currentProject = useRef(projectId);
  const activeRequest = useRef(null);

  currentProject.current = projectId;

  const jobActive = Boolean(job.job_id) && !TERMINAL.includes(job.status);

  const ownJob = job.job_id && String(job.project_id) === String(projectId);

  const allocated = runtime.status === "allocated";

  const sameProject =
    projectId != null && String(runtime.project_id) === String(projectId);

  const differentProject =
    runtime.project_id != null && projectId != null && !sameProject;

  const sourcesReady =
    !datasetsLoading &&
    !datasetsError &&
    datasets.every(
      (source) => source.drive_status === "saved" && source.receipt_id,
    );

  async function refresh(id = currentProject.current) {
    if (!mounted.current || polling.current || actionBusy.current) {
      return;
    }

    polling.current = true;

    const controller = new AbortController();
    activeRequest.current = controller;

    setChecking(true);

    const valid = () =>
      mounted.current &&
      currentProject.current === id &&
      !controller.signal.aborted;

    try {
      const latest = await api("stop-save", {}, controller.signal, 20000);

      if (!valid()) return;

      setJob(latest);

      const active = latest.job_id && !TERMINAL.includes(latest.status);

      if (!active) {
        let status = await api("runtime", {}, controller.signal);

        if (!valid()) return;

        if (!VALID.includes(status.status)) {
          throw new Error("Format status runtime tidak sesuai.");
        }

        setRuntime(status);

        if (
          id != null &&
          status.status === "allocated" &&
          String(status.project_id) !== String(id)
        ) {
          status = await api(
            "select-project",
            jsonPost({
              project_id: id,
            }),
            controller.signal,
          );

          if (!valid()) return;

          if (!VALID.includes(status.status)) {
            throw new Error("Format hubungan proyek tidak sesuai.");
          }

          setRuntime(status);
        }

        setCheckedAt(new Date());
      }

      failureCount.current = 0;

      if (valid()) {
        setAutomatic(true);
        setError("");
      }
    } catch (err) {
      if (valid()) {
        failureCount.current += 1;
        setError(err.message);

        if (failureCount.current >= 3) {
          setAutomatic(false);
        }
      }
    } finally {
      if (activeRequest.current === controller) {
        activeRequest.current = null;
      }

      polling.current = false;

      if (valid()) {
        setChecking(false);
      }
    }
  }

  useEffect(() => {
    mounted.current = true;

    setRuntime({ status: "checking" });
    setJob({ status: "idle" });
    setError("");
    setCheckedAt(null);
    setConfirmation(false);
    setConfirmName("");
    setAcknowledge(false);
    setAutomatic(true);

    failureCount.current = 0;

    const timer = window.setTimeout(() => refresh(projectId), 0);

    return () => {
      mounted.current = false;
      window.clearTimeout(timer);
      activeRequest.current?.abort();
    };
  }, [projectId]);

  useEffect(() => {
    if (!automatic) return;

    const timer = window.setInterval(
      () => {
        if (document.visibilityState === "visible") {
          refresh();
        }
      },
      jobActive ? 4000 : 15000,
    );

    return () => window.clearInterval(timer);
  }, [automatic, jobActive]);

  useEffect(() => {
    if (!busy && !jobActive) return;

    const warn = (event) => {
      event.preventDefault();
      event.returnValue = "";
    };

    window.addEventListener("beforeunload", warn);

    return () => {
      window.removeEventListener("beforeunload", warn);
    };
  }, [busy, jobActive]);

  async function connect() {
    if (!projectId || actionBusy.current || jobActive || polling.current) {
      return;
    }

    const id = projectId;
    const controller = new AbortController();

    activeRequest.current = controller;
    actionBusy.current = true;

    setBusy(true);
    setError("");

    try {
      const data = await api(
        "connect",
        jsonPost({
          project_id: id,
        }),
        controller.signal,
      );

      if (!VALID.includes(data.status)) {
        throw new Error("Format respons koneksi tidak sesuai.");
      }

      if (
        mounted.current &&
        currentProject.current === id &&
        !controller.signal.aborted
      ) {
        setRuntime(data);
        setCheckedAt(new Date());
        setAutomatic(true);
      }
    } catch (err) {
      if (
        mounted.current &&
        currentProject.current === id &&
        !controller.signal.aborted
      ) {
        setError(err.message);
      }
    } finally {
      actionBusy.current = false;

      if (activeRequest.current === controller) {
        activeRequest.current = null;
      }

      if (mounted.current && currentProject.current === id) {
        setBusy(false);
      }
    }
  }

  async function stopAndSave() {
    if (
      !selectedProject ||
      actionBusy.current ||
      jobActive ||
      polling.current ||
      !sameProject ||
      !sourcesReady ||
      confirmName !== selectedProject.name ||
      !acknowledge
    ) {
      return;
    }

    const project = { ...selectedProject };
    const localSources = [...sources];

    const receipts = [...new Set(datasets.map((source) => source.receipt_id))];

    const controller = new AbortController();

    activeRequest.current = controller;
    actionBusy.current = true;

    setBusy(true);
    setError("");

    const valid = () =>
      mounted.current &&
      currentProject.current === project.id &&
      !controller.signal.aborted;

    const assertCurrent = () => {
      if (!valid()) {
        throw new Error(
          "Pilihan proyek berubah. Tindakan berikutnya dibatalkan.",
        );
      }
    };

    try {
      const texts = localSources.filter((source) => source.type === "txt");

      for (let index = 0; index < texts.length; index += 1) {
        assertCurrent();

        const source = texts[index];

        if (typeof source.content !== "string") {
          throw new Error("Isi TXT lokal belum tersedia; Stop dibatalkan.");
        }

        setSavingSources(
          `Menyimpan TXT ${index + 1}/${texts.length}: ${source.name}`,
        );

        const saved = await api(
          "save-local-source",
          {
            method: "POST",
            headers: {
              "Content-Type": "text/plain;charset=UTF-8",
              "X-CTH-Project-ID": String(project.id),
              "X-CTH-Source-Type": "txt",
              "X-CTH-Source-Name": encodeURIComponent(source.name),
            },
            body: new Blob([source.content], {
              type: "text/plain;charset=UTF-8",
            }),
          },
          controller.signal,
          1800000,
        );

        if (!saved.verified || !saved.receipt_id) {
          throw new Error(
            "Upload TXT belum terverifikasi. Runtime belum dihentikan.",
          );
        }

        receipts.push(saved.receipt_id);
      }

      const urls = localSources
        .filter((source) => source.type === "url")
        .map((source) => source.name);

      if (urls.length) {
        assertCurrent();

        setSavingSources("Menyimpan alamat URL…");

        const saved = await api(
          "save-local-source",
          {
            method: "POST",
            headers: {
              "Content-Type": "application/json",
              "X-CTH-Project-ID": String(project.id),
              "X-CTH-Source-Type": "urls",
              "X-CTH-Source-Name": "cth-url-sources.json",
            },
            body: JSON.stringify(urls),
          },
          controller.signal,
          1800000,
        );

        if (!saved.verified || !saved.receipt_id) {
          throw new Error(
            "Daftar URL belum tersimpan. Runtime belum dihentikan.",
          );
        }

        receipts.push(saved.receipt_id);
      }

      assertCurrent();

      setSavingSources("Meminta cadangan workspace proyek…");

      const queued = await api(
        "stop-save",
        jsonPost({
          project_id: project.id,
          confirm_name: confirmName,
          acknowledge_scope: acknowledge,
          source_receipts: receipts,
        }),
        controller.signal,
      );

      if (valid()) {
        setJob(queued);
        setConfirmation(false);
        setAutomatic(true);
      }
    } catch (err) {
      if (valid()) {
        setError(`${err.message} Periksa status sebelum mengulangi Stop.`);
      }
    } finally {
      actionBusy.current = false;

      if (activeRequest.current === controller) {
        activeRequest.current = null;
      }

      if (mounted.current && currentProject.current === project.id) {
        setBusy(false);
        setSavingSources("");
      }
    }
  }

  const cardStatus = allocated
    ? "connected"
    : ["checking", "provisioning"].includes(runtime.status)
      ? "loading"
      : runtime.status === "disconnected"
        ? "disconnected"
        : "error";

  const blocked = busy || checking || jobActive || !selectedProject;

  return (
    <>
      <PageTitle
        title="Colab Connection"
        description="Runtime T4 mengikuti proyek yang dipilih ketika tidak ada pekerjaan aktif."
        action={
          <Badge
            tone={
              jobActive
                ? "info"
                : allocated && sameProject
                  ? "success"
                  : "neutral"
            }
          >
            {jobActive
              ? "Stop & Simpan berjalan"
              : checking
                ? "Memeriksa proyek…"
                : LABELS[runtime.status]}
          </Badge>
        }
      />

      <div className="cth-grid two">
        <ConnectionCard
          title="Google Drive"
          description="Penyimpanan proyek melalui backend"
          {...drive}
        />

        <ConnectionCard
          title="Google Colab"
          description="T4 Standard · tidak beralih ke CPU"
          status={cardStatus}
          message={
            jobActive
              ? "Penyimpanan sedang berjalan. Pergantian proyek ditunda."
              : runtime.message
          }
        />
      </div>

      {error && <Notice tone="error">{error}</Notice>}

      {!automatic && (
        <Notice tone="warning">
          Pemeriksaan otomatis dijeda setelah tiga kegagalan. Klik Periksa
          ulang.
        </Notice>
      )}

      {differentProject && (
        <Notice tone="warning">
          Runtime masih terkait proyek #{runtime.project_id}. CTH mencoba
          mengaitkannya ke proyek pilihan setelah pekerjaan aktif selesai.
          Berkas lama tetap disimpan.
        </Notice>
      )}

      {jobActive && !ownJob && (
        <Notice tone="warning">
          Stop &amp; Simpan proyek {job.project_name || `#${job.project_id}`}{" "}
          masih berjalan. Tunggu selesai sebelum memakai runtime untuk proyek
          ini.
        </Notice>
      )}

      {savingSources && <Notice>{savingSources}</Notice>}

      {ownJob && (
        <section className="cth-card cth-section-gap" role="status">
          <div className="cth-card-heading">
            <h2>
              Stop &amp; Simpan · {job.project_name || selectedProject?.name}
            </h2>

            <Badge
              tone={
                job.status === "completed"
                  ? "success"
                  : ["failed", "interrupted"].includes(job.status)
                    ? "warning"
                    : "info"
              }
            >
              {job.status}
            </Badge>
          </div>

          <p>{job.message}</p>

          {job.backup?.verified && (
            <p className="cth-muted">
              Cadangan terverifikasi: {job.backup.name} · {job.file_count ?? 0}{" "}
              berkas · {job.checkpoint_count ?? 0} checkpoint.{" "}
              <a
                className="cth-text-button"
                href={`https://drive.google.com/file/d/${encodeURIComponent(job.backup.file_id)}/view`}
                target="_blank"
                rel="noopener noreferrer"
              >
                Buka cadangan ↗
              </a>
            </p>
          )}
        </section>
      )}

      <div className="cth-grid two cth-section-gap">
        <section className="cth-card">
          <h2>Runtime session</h2>

          <dl className="cth-details">
            <dt>Proyek dipilih</dt>
            <dd>{selectedProject?.name || "Pilih proyek di header"}</dd>

            <dt>ID proyek dipilih</dt>
            <dd>{projectId ?? "—"}</dd>

            <dt>Proyek terkait runtime</dt>
            <dd>
              {sameProject
                ? selectedProject?.name
                : runtime.project_id != null
                  ? `Proyek #${runtime.project_id}`
                  : "Belum terkait"}
            </dd>

            <dt>Runtime</dt>
            <dd>{runtime.runtime_name || "—"}</dd>

            <dt>GPU dialokasikan</dt>
            <dd>
              {allocated ? "NVIDIA T4 · Standard" : "Belum terverifikasi"}
            </dd>

            <dt>Uji GPU pada status API</dt>
            <dd>
              {runtime.gpu_verified
                ? "Terverifikasi"
                : "Belum tercatat pada respons ini"}
            </dd>

            <dt>Sumber tersimpan</dt>
            <dd>
              {datasets.length} ·{" "}
              {
                datasets.filter((source) => source.drive_status === "saved")
                  .length
              }{" "}
              tersinkron ke Drive
            </dd>

            <dt>Sumber lama di memori tab</dt>
            <dd>{sources.length}</dd>

            <dt>Pemeriksaan terakhir</dt>
            <dd>{checkedAt?.toLocaleTimeString("id-ID") || "—"}</dd>
          </dl>

          <div className="cth-actions">
            <button
              className="cth-button primary"
              disabled={
                blocked || ["provisioning", "mismatch"].includes(runtime.status)
              }
              onClick={connect}
            >
              {busy
                ? "Memproses…"
                : runtime.status === "provisioning"
                  ? "Menyiapkan T4…"
                  : allocated && sameProject
                    ? "Proyek ini sudah terhubung"
                    : allocated
                      ? "Gunakan untuk proyek dipilih"
                      : "Hubungkan ke Colab"}
            </button>

            <button
              className="cth-button"
              disabled={busy || checking}
              onClick={() => {
                failureCount.current = 0;
                setAutomatic(true);
                refresh();
              }}
            >
              Periksa ulang
            </button>
          </div>

          <button
            className="cth-button cth-section-gap"
            style={{
              borderColor: "#fecaca",
              color: "#b91c1c",
              background: "#fff7f7",
            }}
            disabled={blocked || !allocated || !sameProject || !sourcesReady}
            onClick={() => {
              setConfirmation(true);
              setConfirmName("");
              setAcknowledge(false);
            }}
          >
            Stop &amp; Simpan
          </button>

          {!sourcesReady && (
            <p className="cth-muted">
              Tunggu sumber proyek tersimpan di Drive sebelum Stop &amp; Simpan.{" "}
              {datasetsError || ""}
            </p>
          )}
        </section>

        <section className="cth-card">
          <h2>Pergantian proyek</h2>

          <p className="cth-muted">
            Memilih proyek di header halaman ini otomatis mengaitkan runtime T4
            yang tersedia ke ID proyek tersebut. CTH memeriksa pekerjaan aktif
            sebelum mengganti hubungannya.
          </p>

          <p className="cth-muted">
            Pemilihan proyek tidak membuat runtime baru, tidak menjalankan
            training, dan tidak menghapus folder proyek lain.
          </p>

          <h2>Urutan Stop &amp; Simpan</h2>

          <ol className="cth-checklist">
            <li>Periksa sumber proyek yang tersimpan di Drive.</li>
            <li>Minta worker menyimpan checkpoint.</li>
            <li>Arsipkan workspace proyek yang dipilih.</li>
            <li>Upload dan verifikasi cadangan.</li>
            <li>Hentikan T4 setelah pemeriksaan akhir berhasil.</li>
          </ol>
        </section>
      </div>

      {confirmation && (
        <section
          className="cth-card cth-section-gap"
          role="dialog"
          aria-modal="false"
          aria-labelledby="cth-stop-title"
        >
          <h2 id="cth-stop-title">Konfirmasi Stop &amp; Simpan</h2>

          <Notice tone="warning">
            Cadangan mencakup /content/cth/projects/{projectId}. RAM, cache
            model dan folder proyek lain tidak ikut disimpan. Pemeriksaan Stop
            tetap dapat menolak jika data proyek lain belum aman.
          </Notice>

          <div className="cth-form">
            <label>
              Ketik nama proyek: {selectedProject?.name}
              <input
                value={confirmName}
                disabled={busy}
                onChange={(event) => {
                  setConfirmName(event.target.value);
                }}
                autoComplete="off"
              />
            </label>

            <label
              style={{
                display: "flex",
                flexDirection: "row",
                alignItems: "flex-start",
              }}
            >
              <input
                type="checkbox"
                checked={acknowledge}
                disabled={busy}
                onChange={(event) => {
                  setAcknowledge(event.target.checked);
                }}
                style={{
                  width: "auto",
                  marginTop: 4,
                }}
              />

              <span>
                Saya menyetujui penghentian T4 setelah cadangan proyek ini
                terverifikasi.
              </span>
            </label>

            <div className="cth-actions">
              <button
                className="cth-button primary"
                disabled={
                  blocked ||
                  !sameProject ||
                  !sourcesReady ||
                  confirmName !== selectedProject?.name ||
                  !acknowledge
                }
                onClick={stopAndSave}
              >
                {busy ? "Menyimpan…" : "Simpan lalu hentikan T4"}
              </button>

              <button
                className="cth-button"
                disabled={busy || jobActive}
                onClick={() => setConfirmation(false)}
              >
                Batal
              </button>
            </div>
          </div>
        </section>
      )}

      <Notice>
        Biarkan backend berjalan selama penyimpanan. Mengganti proyek tidak
        mencadangkan atau mematikan runtime. Kuota dan durasi sesi Colab tetap
        terbatas.
      </Notice>
    </>
  );
}
