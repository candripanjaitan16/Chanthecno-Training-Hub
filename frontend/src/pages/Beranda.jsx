import { useEffect, useState } from "react";
import { useWorkspace } from "../context/WorkspaceContext";
import Icon from "../components/Icon";
import {
  Badge,
  ConnectionCard,
  Empty,
  Notice,
  PageTitle,
} from "../components/UI";

const API = (
  import.meta.env.VITE_API_BASE_URL || (import.meta.env.DEV ? "http://127.0.0.1:8000" : "")
).replace(/\/$/, "");

export default function Beranda({ onNavigate }) {
  const {
    drive,
    projects,
    projectsLoading,
    projectsError,
    selectedProject,
    selectProject,
    datasets = [],
    datasetsLoading,
    datasetsError,
  } = useWorkspace();

  const projectId = selectedProject?.id;

  const [runtime, setRuntime] = useState(null);
  const [job, setJob] = useState(null);
  const [checking, setChecking] = useState(true);
  const [runtimeError, setRuntimeError] = useState("");
  const [jobError, setJobError] = useState("");
  const [reload, setReload] = useState(0);

  function navigate(page) {
    if (onNavigate) {
      onNavigate(page);
    } else {
      window.location.hash = page;
    }
  }

  useEffect(() => {
    let active = true;
    let pending = false;
    let controller = null;

    setRuntime(null);
    setJob(null);
    setRuntimeError("");
    setJobError("");

    async function read(path, signal) {
      const response = await fetch(`${API}${path}`, {
        signal,
        cache: "no-store",
        headers: { Accept: "application/json" },
      });

      const data = await response.json();

      if (!response.ok) {
        throw new Error(
          typeof data.detail === "string"
            ? data.detail
            : `Permintaan gagal (HTTP ${response.status}).`,
        );
      }

      return data;
    }

    async function refresh() {
      if (pending || !active) return;

      pending = true;
      controller = new AbortController();

      setChecking(true);

      const timer = window.setTimeout(() => controller.abort(), 90000);

      try {
        const results = await Promise.allSettled([
          read("/api/colab/runtime", controller.signal),
          projectId
            ? read(`/api/colab/training/status/${projectId}`, controller.signal)
            : Promise.resolve(null),
        ]);

        if (!active) return;

        const [connection, training] = results;

        if (connection.status === "fulfilled") {
          setRuntime(connection.value);
          setRuntimeError("");
        } else {
          setRuntime(null);
          setRuntimeError(
            "Status Colab belum dapat diperiksa. Buka Colab Connection untuk memeriksa kembali.",
          );
        }

        if (training.status === "fulfilled") {
          setJob(training.value);
          setJobError("");
        } else {
          setJob(null);
          setJobError("Status training belum dapat diperiksa.");
        }
      } finally {
        window.clearTimeout(timer);
        pending = false;

        if (active) setChecking(false);
      }
    }

    const first = window.setTimeout(refresh, 0);

    const interval = window.setInterval(() => {
      if (document.visibilityState === "visible") refresh();
    }, 30000);

    return () => {
      active = false;
      window.clearTimeout(first);
      window.clearInterval(interval);
      controller?.abort();
    };
  }, [projectId, reload]);

  const allocated = runtime?.status === "allocated";

  const sameProject =
    projectId != null &&
    runtime?.project_id != null &&
    String(runtime.project_id) === String(projectId);

  const colabStatus = checking
    ? "loading"
    : runtimeError
      ? "error"
      : allocated
        ? "connected"
        : runtime?.status === "provisioning"
          ? "loading"
          : runtime?.status === "disconnected"
            ? "disconnected"
            : "error";

  const colabMessage = checking
    ? "Memeriksa runtime melalui backend…"
    : runtimeError ||
      (allocated
        ? sameProject
          ? "Runtime T4 tersedia untuk proyek ini"
          : `Runtime T4 tersedia untuk proyek #${runtime.project_id ?? "—"}`
        : runtime?.message || "Runtime belum terverifikasi");

  const trainingLabels = {
    idle: "Belum ada training",
    queued: "Dalam antrean",
    uploading: "Menyiapkan dataset",
    launching: "Memulai worker",
    preparing: "Menyiapkan model",
    training: "Sedang training",
    completed: "Training selesai",
    stopped: "Training dihentikan",
    failed: "Training gagal",
    uncertain: "Status perlu diperiksa",
  };

  const progress =
    job?.max_steps > 0
      ? Math.min(100, Math.round(((job.step || 0) / job.max_steps) * 100))
      : null;

  const loss =
    job?.metrics?.eval_loss ?? job?.metrics?.loss ?? job?.metrics?.train_loss;

  const backupSaved = job?.backup_status === "saved";

  const statistics = [
    {
      icon: "drive",
      count: projectsLoading || projectsError ? "—" : projects.length,
      label: "Total Projects",
      note: "Daftar proyek dari backend",
    },
    {
      icon: "datasets",
      count:
        !selectedProject || datasetsLoading || datasetsError
          ? "—"
          : datasets.length,
      label: "Project Datasets",
      note: selectedProject
        ? `Sumber tersimpan · ${selectedProject.name}`
        : "Pilih proyek terlebih dahulu",
    },
    {
      icon: "model",
      count: checking || jobError ? "—" : backupSaved ? "1" : "—",
      label: "Latest Model Backup",
      note: backupSaved
        ? "Cadangan hasil terakhir terverifikasi"
        : "Belum ada cadangan hasil yang terverifikasi",
    },
  ];

  return (
    <>
      <PageTitle
        title={
          <>
            Welcome to
            <br />
            ChanThecno Training Hub
          </>
        }
        description="Kelola dataset, pantau training, dan simpan hasil model dalam satu tempat."
        action={
          <button
            className="cth-button primary"
            type="button"
            onClick={() => navigate("drive")}
          >
            Buka proyek <span>↗</span>
          </button>
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
          description="Runtime GPU T4 Standard"
          status={colabStatus}
          message={colabMessage}
        />
      </div>

      <div className="cth-grid three cth-section-gap">
        {statistics.map((item) => (
          <section className="cth-card cth-stat" key={item.label}>
            <div className="cth-stat-icon">
              <Icon name={item.icon} />
            </div>

            <div>
              <strong>{item.count}</strong>
              <p>{item.label}</p>
              <small>{item.note}</small>
            </div>
          </section>
        ))}
      </div>

      {datasetsError && <Notice tone="error">{datasetsError}</Notice>}

      <div className="cth-grid two cth-section-gap">
        <section className="cth-card">
          <div className="cth-card-heading">
            <h2>Training Performance</h2>

            <Badge tone={job?.status === "training" ? "info" : "neutral"}>
              {checking
                ? "Memeriksa…"
                : trainingLabels[job?.status] || "Belum diketahui"}
            </Badge>
          </div>

          {!selectedProject ? (
            <Empty icon="activity" title="Pilih proyek">
              Status training akan ditampilkan untuk proyek yang dipilih.
            </Empty>
          ) : jobError ? (
            <Notice tone="error">{jobError}</Notice>
          ) : !job || job.status === "idle" ? (
            <Empty icon="activity" title="Belum ada pekerjaan training">
              Siapkan dataset lalu buka Training Workspace.
            </Empty>
          ) : (
            <>
              <dl className="cth-details">
                <dt>Proyek</dt>
                <dd>{selectedProject.name}</dd>

                <dt>Model</dt>
                <dd>{job.model_name || "—"}</dd>

                <dt>Step</dt>
                <dd>
                  {job.step ?? "—"} / {job.max_steps ?? "—"}
                </dd>

                <dt>Epoch</dt>
                <dd>
                  {Number.isFinite(job.epoch) ? job.epoch.toFixed(2) : "—"}
                </dd>

                <dt>Loss terakhir</dt>
                <dd>{Number.isFinite(loss) ? loss.toFixed(4) : "—"}</dd>

                <dt>Cadangan hasil</dt>
                <dd>
                  {backupSaved
                    ? "Tersimpan dan terverifikasi"
                    : job.backup_status === "not_verified"
                      ? "Catatan lama · belum terverifikasi"
                      : job.backup_status || "Belum terkonfirmasi"}
                </dd>
              </dl>

              {progress !== null && (
                <div>
                  <progress
                    aria-label="Progres langkah training"
                    value={progress}
                    max={100}
                    style={{
                      width: "100%",
                      accentColor: "#2563eb",
                    }}
                  />

                  <p className="cth-muted">
                    {progress}% langkah training. Penyimpanan hasil memiliki
                    status terpisah.
                  </p>
                </div>
              )}
            </>
          )}

          <div className="cth-actions">
            <button
              className="cth-button"
              type="button"
              onClick={() => navigate("training")}
            >
              Buka Training Workspace
            </button>

            <button
              className="cth-text-button"
              type="button"
              disabled={checking}
              onClick={() => setReload((value) => value + 1)}
            >
              {checking ? "Memeriksa…" : "Periksa ulang"}
            </button>
          </div>
        </section>

        <section className="cth-card">
          <div className="cth-card-heading">
            <h2>Recent Projects</h2>

            <button
              className="cth-text-button"
              type="button"
              onClick={() => navigate("drive")}
            >
              View All →
            </button>
          </div>

          {projectsLoading ? (
            <Empty title="Memuat proyek…" />
          ) : projectsError ? (
            <Notice tone="error">{projectsError}</Notice>
          ) : projects.length === 0 ? (
            <Empty title="Belum ada proyek">
              Buat proyek pertama melalui Google Drive.
            </Empty>
          ) : (
            <div className="cth-project-list">
              {projects.slice(0, 4).map((project) => (
                <button
                  key={project.id}
                  type="button"
                  onClick={() => {
                    selectProject(project.id);
                    navigate("drive");
                  }}
                >
                  <span className="cth-folder-icon">
                    <Icon name="drive" />
                  </span>

                  <span>
                    <strong>{project.name}</strong>
                    <small>
                      {project.model_name || "Model belum ditentukan"}
                    </small>
                  </span>

                  <span>
                    {String(selectedProject?.id) === String(project.id)
                      ? "Aktif"
                      : "→"}
                  </span>
                </button>
              ))}
            </div>
          )}
        </section>
      </div>
    </>
  );
}
