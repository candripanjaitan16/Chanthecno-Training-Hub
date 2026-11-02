import { useEffect, useRef, useState } from "react";
import { useWorkspace } from "../context/WorkspaceContext";
import SourceAnalysis from "../components/SourceAnalysis";
import { analyzeDatasets } from "../services/datasets";
import NeuralCanvas from "../components/NeuralCanvas";
import { Badge, Notice, PageTitle } from "../components/UI";

const API = (
  import.meta.env.VITE_API_BASE_URL || (import.meta.env.DEV ? "http://127.0.0.1:8000" : "")
).replace(/\/$/, "");

const TERMINAL = new Set(["idle", "completed", "stopped", "failed"]);

const LABELS = {
  idle: "Belum ada training",
  queued: "Dalam antrean",
  uploading: "Menyiapkan dataset",
  launching: "Memulai worker",
  preparing: "Menyiapkan model",
  training: "Sedang training",
  stopped: "Training dihentikan",
  completed: "Training selesai",
  failed: "Training gagal",
  uncertain: "Status perlu diperiksa",
};

function savedName(project) {
  if (!project) return "";

  try {
    return (
      localStorage.getItem(`cth-model-name-${project.id}`) ??
      project.model_name ??
      ""
    );
  } catch {
    return project.model_name || "";
  }
}

async function api(path, options = {}, timeout = 90000) {
  const controller = new AbortController();

  const timer = window.setTimeout(() => controller.abort(), timeout);

  try {
    const response = await fetch(`${API}/api/colab${path}`, {
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
        `Respons backend tidak sesuai (HTTP ${response.status}).`,
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
        "Permintaan melewati batas waktu. Periksa status sebelum mencoba lagi.",
      );
    }

    if (error instanceof TypeError) {
      throw new Error(
        "Backend belum dapat dihubungi. Pastikan chanthecno-hub berjalan.",
      );
    }

    throw error;
  } finally {
    window.clearTimeout(timer);
  }
}

function post(body) {
  return {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
    },
    body: JSON.stringify(body),
  };
}

export default function TrainingWorkspace({ onNavigate }) {
  const {
    selectedProject,
    datasets = [],
    datasetsLoading,
    datasetsError,
  } = useWorkspace();

  const projectId = selectedProject?.id;

  const [modelName, setModelName] = useState(() => savedName(selectedProject));

  const [runtime, setRuntime] = useState(null);
  const [job, setJob] = useState({ status: "idle" });
  const [epochs, setEpochs] = useState("1");
  const [busy, setBusy] = useState("");
  const [checking, setChecking] = useState(false);
  const [error, setError] = useState("");
  const [runtimeError, setRuntimeError] = useState("");
  const [jobError, setJobError] = useState("");
  const [message, setMessage] = useState("");
  const [confirmed, setConfirmed] = useState(false);
  const [selectedDatasetIds, setSelectedDatasetIds] = useState([]);
  const [analysisResult, setAnalysisResult] = useState({ key: "", report: null, error: "" });
  const [emptyAcceptedFor, setEmptyAcceptedFor] = useState("");
  const [lastChecked, setLastChecked] = useState(null);
  const [runtimeChecked, setRuntimeChecked] = useState(0);
  const [preflight, setPreflight] = useState(false);
  const [clock, setClock] = useState(Date.now());

  const currentProject = useRef(projectId);
  const mounted = useRef(false);
  const polling = useRef(null);
  const action = useRef(null);

  currentProject.current = projectId;

  const txtSources = datasets.filter((source) => source.type === "txt");

  const urlCount = datasets.filter((source) => source.type === "url").length;

  const selectedSources = txtSources.filter(source => selectedDatasetIds.includes(source.id));
  const selectedBytes = selectedSources.reduce((total, source) => total + source.size, 0);
  const allSourcesReady = selectedSources.length > 0 && selectedSources.length <= 500 &&
    selectedSources.every(source => source.local_saved && source.drive_status === "saved" && source.receipt_id);

  const analysisKey = selectedSources.map(source => `${source.id}:${source.sha256}:${source.source_mode || "filtered"}`).join("|");
  const sourceAnalysis = analysisResult.key === analysisKey ? analysisResult.report : null;
  const analysisError = analysisResult.key === analysisKey ? analysisResult.error : "";
  const emptyAccepted = Boolean(analysisKey && emptyAcceptedFor === analysisKey);
  useEffect(() => {
    const controller = new AbortController();
    if (!projectId || !analysisKey) return () => controller.abort();
    const ids = analysisKey.split("|").map(value => value.split(":")[0]);
    const timer = setTimeout(() => analyzeDatasets(projectId, ids, controller.signal).then(report => {
      if (!controller.signal.aborted) setAnalysisResult({ key: analysisKey, report, error: "" });
    }).catch(error => { if (!controller.signal.aborted) setAnalysisResult({ key: analysisKey, report: null, error: error.message }); }), 250);
    return () => { clearTimeout(timer); controller.abort(); };
  }, [projectId, analysisKey]);

  const activeJob = !TERMINAL.has(job.status);

  const runtimeReady =
    runtime?.status === "allocated" &&
    String(runtime.project_id) === String(projectId);

  const runtimeFresh = runtimeChecked > 0 && clock - runtimeChecked < 30000;

  const jobFresh = lastChecked && clock - lastChecked.getTime() < 30000;

  const training =
    job.status === "training" && job.worker_running === true && jobFresh && !jobError && !job.connection_error;

  const name = modelName.trim();

  const validModel = /^[A-Za-z0-9][A-Za-z0-9_-]{0,79}$/.test(name);

  const canCheck =
    projectId &&
    runtimeReady &&
    runtimeFresh &&
    !activeJob &&
    !busy &&
    !checking &&
    !runtimeError &&
    !jobError;

  const canStart =
    canCheck &&
    preflight &&
    allSourcesReady &&
    sourceAnalysis?.training_samples > 0 &&
    (sourceAnalysis.sources_ready || emptyAccepted) &&
    !datasetsLoading &&
    !datasetsError &&
    validModel &&
    confirmed &&
    !error;

  const progress =
    job.max_steps > 0
      ? Math.min(100, Math.round(((job.step || 0) / job.max_steps) * 100))
      : null;

  const loss =
    job.metrics?.loss ?? job.metrics?.eval_loss ?? job.metrics?.train_loss;

  function changeName(value) {
    setModelName(value);

    if (!projectId) return;

    try {
      localStorage.setItem(`cth-model-name-${projectId}`, value);
    } catch {
      setMessage(
        "Nama belum dapat disimpan di browser, tetapi tetap bisa dipakai untuk permintaan training ini.",
      );
    }
  }

  function navigate(page) {
    if (onNavigate) {
      onNavigate(page);
    } else {
      window.location.hash = page;
    }
  }

  async function refresh(id = currentProject.current) {
    if (!id || polling.current || action.current) return;

    const token = {};
    polling.current = token;

    if (mounted.current) {
      setChecking(true);
    }

    try {
      const [connection, status] = await Promise.allSettled([
        api("/runtime"),
        api(`/training/status/${id}`),
      ]);

      if (!mounted.current || currentProject.current !== id) {
        return;
      }

      if (connection.status === "fulfilled") {
        setRuntime(connection.value);
        setRuntimeChecked(Date.now());
        setRuntimeError("");
      } else {
        setRuntimeError(connection.reason.message);
      }

      if (status.status === "fulfilled") {
        setJob(status.value);
        setLastChecked(new Date());
        setJobError("");
      } else {
        setJobError(status.reason.message);
      }
    } finally {
      if (polling.current === token) {
        polling.current = null;
      }

      if (mounted.current && currentProject.current === id) {
        setChecking(false);
      }
    }
  }

  useEffect(() => {
    mounted.current = true;

    const timer = window.setInterval(() => setClock(Date.now()), 1000);

    return () => {
      mounted.current = false;
      window.clearInterval(timer);
    };
  }, []);

  useEffect(() => {
    setModelName(savedName(selectedProject));
    setRuntime(null);
    setJob({ status: "idle" });
    setLastChecked(null);
    setRuntimeChecked(0);
    setConfirmed(false);
    setPreflight(false);
    setError("");
    setRuntimeError("");
    setJobError("");
    setMessage("");
    setBusy("");

    polling.current = null;
    action.current = null;

    const first = window.setTimeout(() => refresh(projectId), 0);

    const timer = window.setInterval(() => {
      if (document.visibilityState === "visible") {
        refresh(projectId);
      }
    }, 10000);

    return () => {
      window.clearTimeout(first);
      window.clearInterval(timer);
    };
  }, [projectId]);

  async function perform(label, task) {
    if (!projectId || action.current || polling.current) {
      return;
    }

    const id = projectId;
    const token = {};

    action.current = token;

    setBusy(label);
    setError("");
    setMessage("");

    const valid = () =>
      mounted.current &&
      currentProject.current === id &&
      action.current === token;

    try {
      await task(id, valid);
    } catch (err) {
      if (valid()) {
        setError(err.message);
      }
    } finally {
      if (action.current === token) {
        action.current = null;
      }

      if (mounted.current && currentProject.current === id) {
        setBusy("");
      }
    }
  }

  function checkStorage() {
    if (!canCheck) return;

    perform("Memeriksa penyimpanan…", async (id, valid) => {
      const result = await api(
        "/training/preflight",
        post({
          project_id: id,
        }),
        300000,
      );

      if (result.status !== "ready" || result.verified !== true) {
        throw new Error("Jalur penyimpanan belum terverifikasi.");
      }

      if (valid()) {
        setPreflight(true);
        setMessage(result.message);
      }
    });
  }

  function startTraining() {
    if (!canStart) return;

    const datasetIds = selectedSources.map(source => source.id);

    const selectedName = name;
    const selectedEpochs = Number(epochs);

    perform("Meminta training…", async (id, valid) => {
      const started = await api(
        "/training/start",
        post({
          project_id: id,
          model_name: selectedName,
          epochs: selectedEpochs,
          dataset_ids: datasetIds,
          acknowledge_empty_sources: emptyAccepted,
        }),
      );

      if (valid()) {
        setJob(started);

        setMessage(
          `Permintaan training ${selectedName} diterima. Jangan menekan Mulai lagi.`,
        );
      }
    });
  }

  function stopTraining() {
    if (!activeJob) return;

    perform("Meminta penghentian…", async (id, valid) => {
      const result = await api(
        "/training/stop",
        post({
          project_id: id,
        }),
      );

      if (valid()) {
        setMessage(result.message);
      }
    });
  }

  return (
    <>
      <PageTitle
        title="Training Workspace"
        description="Latih Qwen 0.5B menggunakan TXT proyek di GPU T4 Colab."
        action={
          <Badge tone={training ? "info" : "neutral"}>
            {LABELS[job.status] || job.status}
          </Badge>
        }
      />

      {error && <Notice tone="error">{error}</Notice>}

      {runtimeError && <Notice tone="warning">{runtimeError}</Notice>}

      {jobError && <Notice tone="warning">{jobError}</Notice>}

      {datasetsError && <Notice tone="error">{datasetsError}</Notice>}

      {job.connection_error && (
        <Notice tone="warning">{job.connection_error}</Notice>
      )}

      {message && <Notice>{message}</Notice>}

      {txtSources.length > 0 && !allSourcesReady && (
        <Notice tone="warning">
          Pilih 1–500 TXT untuk training baru. Semua pilihan wajib tersimpan lokal dan terverifikasi di Drive.
        </Notice>
      )}

      <section className="cth-card cth-section-gap" aria-label="Pilihan dataset training">
        <h2>Dataset untuk training baru</h2>
        <p className="cth-muted">Pilih versi TXT yang akan dipakai. URL tidak dipakai untuk training.</p>
        <fieldset disabled={activeJob || Boolean(busy)} style={{ border: 0, padding: 0, maxHeight: 260, overflow: "auto" }}>
          <legend className="cth-muted">{txtSources.length} TXT tersedia pada proyek ini</legend>
          {txtSources.map(source => <label key={source.id} style={{ display: "flex", gap: 10, padding: "10px 0", alignItems: "flex-start" }}>
            <input type="checkbox" aria-label={`Pilih dataset ${source.name}`} checked={selectedDatasetIds.includes(source.id)} onChange={event => {
              setSelectedDatasetIds(previous => event.target.checked ? [...previous, source.id] : previous.filter(id => id !== source.id));
              setConfirmed(false);
            }} />
            <span style={{ minWidth: 0, overflowWrap: "anywhere" }}><strong>{source.name}</strong><br />
              <small>{(source.size / 1024).toFixed(1)} KiB · {source.local_saved ? "Salinan lokal tersedia" : "Pulihkan salinan lokal dahulu"} · {source.drive_status === "saved" ? "Drive terverifikasi" : source.drive_status === "failed" ? "Sinkronisasi gagal — retry di Datasets" : "Drive belum terverifikasi"}</small>
            </span>
          </label>)}
        </fieldset>
        <p role="status">{selectedSources.length} TXT dipilih · {(selectedBytes / 1024).toFixed(1)} KiB · {allSourcesReady ? "Siap diperiksa ulang backend sebelum training" : "Belum siap dilatih"}</p>
        {selectedSources.length > 0 && <details><summary>Ringkasan versi yang dipilih</summary>
          {selectedSources.map(source => <p key={source.id} style={{ overflowWrap: "anywhere" }}><strong>{source.name}</strong> · Drive ID: {source.drive_file_id || "belum ada"}<br /><small>SHA-256: {source.sha256}</small></p>)}
        </details>}
        <SourceAnalysis report={sourceAnalysis} />
        {analysisError && <Notice tone="error">Analisis sumber gagal: {analysisError}</Notice>}
        {sourceAnalysis && !sourceAnalysis.sources_ready && <label><input type="checkbox" aria-label="Lanjutkan tanpa indeks sumber" checked={emptyAccepted} onChange={event => setEmptyAcceptedFor(event.target.checked ? analysisKey : "")} /> Saya memahami indeks kosong dan memilih latihan tanpa kutipan sumber. Ini tidak membuat jawaban ASSISTANT menjadi fakta.</label>}
        {job.dataset_stats?.counts && <p>Sampel aktual setelah tokenizer: train {job.dataset_stats.counts.train || 0} · evaluasi {job.dataset_stats.counts.eval || 0}</p>}
        <p className="cth-muted">Training baru membuat run dan manifest baru. Resume checkpoint adalah tindakan CLI terpisah yang mensyaratkan dataset, konfigurasi dan state cocok. Menambah dataset tidak melanjutkan run lama.</p>
      </section>

      <div className="cth-grid training-layout">
        <section className="cth-card cth-visual-card">
          <div className="cth-card-heading">
            <h2>Neural Activity</h2>

            <Badge tone={training ? "info" : "neutral"}>
              {training ? "Training aktif" : "Diam"}
            </Badge>
          </div>

          <NeuralCanvas active={Boolean(training)} />

          <div className="cth-visual-footer">
            <p>
              {training
                ? "Animasi mengikuti status training yang baru diperiksa."
                : "Belum ada heartbeat worker aktif yang terverifikasi."}
            </p>
          </div>
        </section>

        <section className="cth-card">
          <div className="cth-card-heading">
            <h2>Training Session</h2>

            <button
              className="cth-text-button"
              disabled={!projectId || checking || Boolean(busy)}
              onClick={() => {
                setError("");
                refresh();
              }}
            >
              {checking ? "Memeriksa…" : "Periksa ulang"}
            </button>
          </div>

          <dl className="cth-details">
            <dt>Proyek</dt>
            <dd>{selectedProject?.name || "Pilih proyek di header"}</dd>

            <dt>ID proyek</dt>
            <dd>{projectId ?? "—"}</dd>

            <dt>Model dasar</dt>
            <dd>Qwen2.5-0.5B-Instruct · LoRA</dd>

            <dt>Nama pada pekerjaan terakhir</dt>
            <dd>{job.model_name || "Belum ada pekerjaan"}</dd>

            <dt>Dataset tersimpan</dt>
            <dd>
              {txtSources.length} TXT · {urlCount} URL belum diproses
            </dd>

            <dt>Runtime</dt>
            <dd>
              {runtimeReady
                ? "T4 Standard tersedia untuk proyek ini"
                : "Hubungkan T4 untuk proyek ini"}
            </dd>

            <dt>Uji penyimpanan</dt>
            <dd>
              {preflight
                ? "Berhasil pada halaman ini"
                : "Klik Uji penyimpanan untuk memeriksa proyek ini"}
            </dd>

            <dt>Uji CUDA worker</dt>
            <dd>
              {job.gpu_verified
                ? "T4 diverifikasi oleh worker"
                : "Diperiksa saat persiapan training"}
            </dd>

            <dt>Run</dt>
            <dd>{job.run_id || "—"}</dd>

            <dt>Step</dt>
            <dd>
              {job.step ?? "—"} / {job.max_steps ?? "—"}
            </dd>

            <dt>Epoch / Loss</dt>
            <dd>
              {Number.isFinite(job.epoch) ? job.epoch.toFixed(2) : "—"} /{" "}
              {Number.isFinite(loss) ? loss.toFixed(4) : "—"}
            </dd>

            <dt>Status</dt>
            <dd>{job.message || LABELS[job.status] || "Belum tersedia"}</dd>

            <dt>Cadangan hasil</dt>
            <dd>
              {job.backup_status === "saved"
                ? "Terverifikasi"
                : job.backup_status || "Belum diketahui"}
            </dd>

            <dt>Terakhir diperiksa</dt>
            <dd>{lastChecked?.toLocaleTimeString("id-ID") || "—"}</dd>
          </dl>

          {progress !== null && (
            <div style={{ marginBottom: 18 }}>
              <progress
                aria-label="Progres training"
                value={progress}
                max="100"
                style={{
                  width: "100%",
                  accentColor: "#2563eb",
                }}
              />

              <p className="cth-muted">
                {progress}% langkah optimizer. Penyimpanan hasil memiliki status
                terpisah.
              </p>
            </div>
          )}

          <div className="cth-form">
            <label>
              Nama model hasil training
              <input
                value={modelName}
                onChange={(event) => {
                  changeName(event.target.value);
                }}
                disabled={!projectId || activeJob || Boolean(busy)}
                maxLength={80}
                placeholder="chanthecno-alpha"
              />
            </label>

            <p className="cth-muted">
              Nama disimpan per proyek di browser ini. Dipakai untuk pekerjaan
              training berikutnya; tidak mengganti nama hasil training lama atau
              metadata proyek di database.
            </p>

            {name && !validModel && (
              <p role="alert" className="cth-muted">
                Gunakan 1–80 karakter: huruf, angka, tanda minus atau garis
                bawah. Karakter pertama harus huruf atau angka.
              </p>
            )}

            <label>
              Jumlah epoch
              <select
                value={epochs}
                onChange={(event) => {
                  setEpochs(event.target.value);
                }}
                disabled={activeJob || Boolean(busy)}
                style={{
                  width: "100%",
                  padding: 12,
                  border: "1px solid #dbe3ef",
                  borderRadius: 8,
                  background: "white",
                }}
              >
                <option value="1">1 epoch · uji awal</option>

                <option value="2">2 epoch</option>

                <option value="3">3 epoch</option>
              </select>
            </label>

            <button
              className="cth-button"
              disabled={!canCheck}
              onClick={checkStorage}
            >
              {busy === "Memeriksa penyimpanan…" ? busy : "Uji penyimpanan"}
            </button>

            <label
              style={{
                display: "flex",
                gap: 10,
                alignItems: "flex-start",
              }}
            >
              <input
                type="checkbox"
                checked={confirmed}
                onChange={(event) => {
                  setConfirmed(event.target.checked);
                }}
                disabled={activeJob || Boolean(busy)}
                style={{
                  width: "auto",
                  marginTop: 4,
                }}
              />

              <span>
                Saya setuju memakai TXT tersimpan proyek ini di Colab serta
                menggunakan kuota T4.
              </span>
            </label>

            <button
              className="cth-button primary"
              disabled={!canStart}
              onClick={startTraining}
            >
              {busy || "Mulai training baru"}
            </button>

            {activeJob && (
              <button
                className="cth-button"
                disabled={
                  Boolean(busy) ||
                  checking ||
                  ["queued", "uploading", "launching"].includes(job.status)
                }
                onClick={stopTraining}
              >
                Hentikan training · simpan checkpoint
              </button>
            )}

            <button
              className="cth-button"
              disabled={Boolean(busy)}
              onClick={() => navigate("colab")}
            >
              Buka Colab Connection
            </button>
          </div>

          <p className="cth-muted">
            Untuk proyek baru, klik Uji penyimpanan dan tunggu berhasil sebelum
            meminta training. Uji ini tidak memulai training. Backend tetap
            memeriksa masa berlaku preflight.
          </p>
        </section>
      </div>

      <Notice>
        TXT melatih pola bahasa; fine-tuning tidak menjamin semua jawaban sesuai
        fakta dataset. URL belum diproses. Checkpoint berkala di Colab masih
        perlu dicadangkan. Jangan mematikan runtime sebelum hasil tersimpan dan
        terverifikasi.
      </Notice>
    </>
  );
}
