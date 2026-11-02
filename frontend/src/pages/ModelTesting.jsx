import { useCallback, useEffect, useLayoutEffect, useRef, useState } from "react";
import { useWorkspace } from "../context/WorkspaceContext";
import "./ModelTesting.css";
import SourceAnalysis from "../components/SourceAnalysis";

const API = (
  import.meta.env.VITE_API_BASE_URL || (import.meta.env.DEV ? "http://127.0.0.1:8000" : "")
).replace(/\/$/, "");

const ACTIVE = ["queued", "downloading", "indexing", "uploading", "installing"];

async function request(path, options = {}, signal, timeout = 420000) {
  const controller = new AbortController();
  const abort = () => controller.abort();

  if (signal?.aborted) abort();

  signal?.addEventListener("abort", abort, { once: true });

  const timer = window.setTimeout(abort, timeout);

  try {
    const response = await fetch(`${API}/api/model-testing${path}`, {
      ...options,
      signal: controller.signal,
      cache: "no-store",
      headers: {
        Accept: "application/json",
        ...options.headers,
      },
    });

    const data = await response.json();

    if (!response.ok) {
      throw new Error(
        typeof data.detail === "string"
          ? data.detail
          : `Permintaan gagal (${response.status}).`,
      );
    }

    return data;
  } catch (error) {
    if (error.name === "AbortError") {
      throw new Error(
        "Permintaan terputus. Pekerjaan di Colab mungkin " +
          "masih berjalan; periksa status sebelum mengulang.",
      );
    }

    if (error instanceof TypeError) {
      throw new Error("Backend belum dapat dihubungi. Pastikan CTH berjalan.");
    }

    throw error;
  } finally {
    clearTimeout(timer);
    signal?.removeEventListener("abort", abort);
  }
}

function Glyph({ name, ...props }) {
  const paths = {
    model: "M12 3 3 8l9 5 9-5-9-5Zm-9 9 9 5 9-5M3 16l9 5 9-5",
    refresh:
      "M20 7v5h-5M4 17v-5h5M6 7a7 7 0 0 1 12-1l2 6M4 12l2 6a7 7 0 0 0 12-1",
    send: "M12 19V5m-6 6 6-6 6 6",
    restore:
      "M7 18H6a4 4 0 0 1-1-7 7 7 0 0 1 13-2 5 5 0 0 1 0 9h-1m-5-8v10m-4-4 4 4 4-4",
    chat: "M5 4h14a2 2 0 0 1 2 2v10a2 2 0 0 1-2 2H9l-6 3V6a2 2 0 0 1 2-2Z",
    copy: "M9 9h11v12H9V9Zm6-4V3H3v13h2",
    check: "m5 12 4 4L19 6",
    more: "M5 6h14M5 12h14M5 18h14",
  };

  return (
    <svg
      width="19"
      height="19"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.65"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
      {...props}
    >
      <path d={paths[name] || paths.model} />
    </svg>
  );
}

function dateLabel(run) {
  const value = /^(\d{4})(\d{2})(\d{2})T(\d{2})(\d{2})(\d{2})Z/.exec(run || "");

  if (!value) return "Run tersimpan";

  const date = new Date(
    `${value[1]}-${value[2]}-${value[3]}` +
      `T${value[4]}:${value[5]}:${value[6]}Z`,
  );

  return date.toLocaleString("id-ID", {
    day: "numeric",
    month: "short",
    hour: "2-digit",
    minute: "2-digit",
    timeZone: "Asia/Jakarta",
  });
}

export default function ModelTesting({ onNavigate }) {
  const { selectedProject, datasets = [] } = useWorkspace();
  const projectId = selectedProject?.id;

  const [models, setModels] = useState([]);
  const [selected, setSelected] = useState("");
  const [model, setModel] = useState(null);
  const [loading, setLoading] = useState(false);
  const [checking, setChecking] = useState(false);
  const [sending, setSending] = useState(false);
  const [restore, setRestore] = useState({ status: "idle" });
  const [startingRestore, setStartingRestore] = useState(false);
  const [messages, setMessages] = useState([]);
  const [question, setQuestion] = useState("");
  const [mode, setMode] = useState("qwen");
  const [error, setError] = useState("");
  const [copied, setCopied] = useState("");
  const [sourceDatasetIds, setSourceDatasetIds] = useState([]);
  const [libraryOpen, setLibraryOpen] = useState(() => window.matchMedia("(min-width: 701px)").matches);

  const scope = useRef("");
  const pending = useRef(false);
  const scroll = useRef(null);

  const selectedModel = models.find((item) => item.job_id === selected);

  const factSources = datasets.filter(item => item.type === "txt" && ["facts_only", "curated_qa"].includes(item.source_mode));
  const selectedFacts = factSources.filter(item => sourceDatasetIds.includes(item.id));
  const factsReady = selectedFacts.length > 0 && selectedFacts.every(item => item.drive_status === "saved" && item.local_saved);

  const restoring = startingRestore || ACTIVE.includes(restore.status);

  const busy = sending || restoring;

  const available = model?.status === "available" && model.job_id === selected;

  useLayoutEffect(() => {
    scope.current = Symbol(`${projectId || ""}:${selected}`);
    return () => { scope.current = null; };
  }, [projectId, selected]);

  const refresh = useCallback(
    async (signal) => {
      if (!projectId || !selected) return;

      const key = scope.current;

      setChecking(true);
      setError("");

      try {
        const result = await request(
          `/status/${projectId}?job_id=${selected}`,
          {},
          signal,
          120000,
        );

        if (scope.current === key && !signal?.aborted) {
          setModel(result);
        }
      } catch (err) {
        if (scope.current === key && !signal?.aborted) {
          setModel(null);
          setError(err.message);
        }
      } finally {
        if (scope.current === key && !signal?.aborted) {
          setChecking(false);
        }
      }
    },
    [projectId, selected],
  );

  useEffect(() => {
    const controller = new AbortController();

    setModels([]);
    setSelected("");
    setModel(null);
    setMessages([]);
    setRestore({ status: "idle" });
    setQuestion("");
    setError("");
    setSending(false);
    setStartingRestore(false);

    pending.current = false;

    if (!projectId) {
      return () => controller.abort();
    }

    setLoading(true);

    request(`/models/${projectId}`, {}, controller.signal, 30000)
      .then((data) => {
        if (controller.signal.aborted) return;

        if (!Array.isArray(data.models)) {
          throw new Error("Daftar model tidak sesuai.");
        }

        setModels(data.models);
        setSelected(data.models[0]?.job_id || "");
      })
      .catch((err) => {
        if (!controller.signal.aborted) {
          setError(err.message);
        }
      })
      .finally(() => {
        if (!controller.signal.aborted) {
          setLoading(false);
        }
      });

    return () => controller.abort();
  }, [projectId]);

  useEffect(() => {
    const controller = new AbortController();

    setModel(null);
    setMessages([]);
    setSourceDatasetIds([]);
    setQuestion("");
    setSending(false);

    pending.current = false;

    const timer = window.setTimeout(() => refresh(controller.signal), 0);

    return () => {
      clearTimeout(timer);
      controller.abort();
    };
  }, [refresh]);

  useEffect(() => {
    if (!projectId || !selected) return;

    const controller = new AbortController();
    const key = scope.current;
    let timer;

    async function poll() {
      try {
        const value = await request(
          `/restore/${projectId}`,
          {},
          controller.signal,
          30000,
        );

        if (controller.signal.aborted || scope.current !== key) {
          return;
        }

        if (value.job_id === selected) {
          setRestore(value);
        }

        if (value.job_id === selected && ACTIVE.includes(value.status)) {
          timer = setTimeout(poll, 2000);
        } else if (value.job_id === selected && value.status === "completed") {
          refresh(controller.signal);
        }
      } catch (err) {
        if (!controller.signal.aborted && scope.current === key) {
          setError(err.message);
        }
      }
    }

    const start = setTimeout(poll, 0);

    return () => {
      clearTimeout(start);
      clearTimeout(timer);
      controller.abort();
    };
  }, [projectId, selected, startingRestore, refresh]);

  useEffect(() => {
    if (scroll.current) {
      scroll.current.scrollTop = scroll.current.scrollHeight;
    }
  }, [messages, sending]);

  async function restoreModel(refreshSources = false) {
    const refreshSourcesExplicitly = refreshSources === true;
    if (busy || !selected || pending.current) {
      return;
    }

    const key = scope.current;

    pending.current = true;
    setStartingRestore(true);
    setError("");

    try {
      const value = await request(
        "/restore",
        {
          method: "POST",
          headers: {
            "Content-Type": "application/json",
          },
          body: JSON.stringify({
            project_id: projectId,
            job_id: selected,
            refresh_sources: refreshSourcesExplicitly,
            source_dataset_ids: refreshSourcesExplicitly ? selectedFacts.map(item => item.id) : [],
          }),
        },
        undefined,
        30000,
      );

      if (scope.current === key) {
        setRestore(value);
        if (refreshSourcesExplicitly) { setMessages([]); setModel(null); }
      }
    } catch (err) {
      if (scope.current === key) {
        setError(err.message);
      }
    } finally {
      if (scope.current === key) {
        setStartingRestore(false);
        pending.current = false;
      }
    }
  }

  async function send(event) {
    event?.preventDefault();

    if (!available || !question.trim() || busy || checking || pending.current) {
      return;
    }

    const key = scope.current;
    const text = question.trim();
    const id = `${Date.now()}-${Math.random()}`;

    pending.current = true;

    setSending(true);
    setError("");
    setQuestion("");

    setMessages((previous) => [
      ...previous.slice(-38),
      {
        id,
        role: "user",
        text,
      },
    ]);

    try {
      const value = await request("/chat", {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
        },
        body: JSON.stringify({
          project_id: projectId,
          job_id: selected,
          question: text,
          mode,
        }),
      });

      if (scope.current !== key) return;

      if (typeof value.answer !== "string" || !Array.isArray(value.sources)) {
        throw new Error("Jawaban backend tidak sesuai.");
      }

      setMessages((previous) => [
        ...previous,
        {
          id: `${id}-reply`,
          role: "assistant",
          text: value.answer,
          sources: value.sources,
          evidence: value.evidence || [],
          mode: value.mode,
          notice: value.notice,
          basis: value.basis,
        },
      ]);
    } catch (err) {
      if (scope.current === key) {
        setError(err.message);
        setQuestion((previous) => previous || text);
      }
    } finally {
      if (scope.current === key) {
        setSending(false);
        pending.current = false;
      }
    }
  }

  function navigate(page) {
    if (onNavigate) {
      onNavigate(page);
    } else {
      window.location.hash = page;
    }
  }

  async function copy(item) {
    try {
      await navigator.clipboard.writeText(item.text);
      setCopied(item.id);
    } catch {
      setError("Teks belum dapat disalin oleh browser.");
    }
  }

  return (
    <div className="cth-test">
      <header className="mt-title">
        <div>
          <span className="mt-eyebrow">CHANTHECNO / MODEL STUDIO</span>
          <h1>
            Model Testing<span>.</span>
          </h1>
          <p>Model yang sudah kamu latih, siap diuji kembali.</p>
        </div>

        <span className="mt-project">
          {selectedProject?.name || "Pilih proyek"}
        </span>
      </header>

      {error && (
        <div className="mt-alert" role="alert">
          {error}
          <button
            type="button"
            onClick={() => setError("")}
            aria-label="Tutup pesan"
          >
            ×
          </button>
        </div>
      )}

      <div className="mt-layout">
        <details className="mt-library" open={libraryOpen} onToggle={(event) => setLibraryOpen(event.currentTarget.open)}>
          <summary className="mt-library-head">
            <strong>Model tersimpan</strong>
            <span>{models.length} · buka/tutup</span>
          </summary>

          <p className="mt-library-note">
            Hasil training selesai pada proyek ini.
          </p>

          <div className="mt-models">
            {loading && <p className="mt-muted">Memuat daftar model…</p>}

            {!loading && !models.length && (
              <div className="mt-no-model">
                <Glyph name="model" />
                <strong>
                  {projectId
                    ? "Belum ada model selesai"
                    : "Pilih proyek dahulu"}
                </strong>
                <p>
                  Daftar ini membaca riwayat hasil training, bukan hanya
                  pekerjaan terakhir.
                </p>
              </div>
            )}

            {models.map((item) => (
              <button
                type="button"
                className={`mt-model ${
                  selected === item.job_id ? "is-selected" : ""
                }`}
                key={item.job_id}
                disabled={busy}
                onClick={() => {
                  setSelected(item.job_id);
                  setRestore({ status: "idle" });
                }}
              >
                <span className="mt-model-icon">
                  <Glyph name="model" />
                </span>

                <span className="mt-model-info">
                  <strong>{item.model_name}</strong>
                  <small>{dateLabel(item.run_id)} WIB</small>
                  <span className="mt-backup">
                    {item.backup_verified
                      ? "Cadangan Drive terverifikasi"
                      : item.local_available
                        ? "Salinan lokal tersedia"
                        : "Cadangan perlu diperiksa"}
                  </span>
                </span>

                {selected === item.job_id && (
                  <span className="mt-selected-dot" />
                )}
              </button>
            ))}
          </div>

          <div className="mt-library-footer">
            <Glyph name="restore" />
            <p>
              <strong>Tidak perlu training ulang.</strong>
              <br />
              Pulihkan hasil tersimpan ketika runtime berganti.
            </p>
          </div>
        </details>

        <section className="mt-chat">
          <header className="mt-chat-head">
            <div className="mt-chat-heading">
              <span className="mt-model-mark">CT</span>
              <div>
                <h2>{selectedModel?.model_name || "Pilih model tersimpan"}</h2>

                <span className={`mt-runtime ${available ? "is-ready" : ""}`}>
                  <i />
                  {checking
                    ? "Memeriksa berkas…"
                    : available
                      ? "Berkas tersedia di runtime"
                      : "Menunggu pemulihan model"}
                </span>
              </div>
            </div>

            <button
              className="mt-icon-button"
              type="button"
              onClick={() => refresh()}
              disabled={!selected || busy || checking}
              aria-label="Periksa model"
              title="Periksa model"
            >
              <Glyph name="refresh" />
            </button>
          </header>

          <div className="mt-toolbar">
            <button type="button" className="mt-new-chat" disabled={busy || !messages.length} onClick={() => { setMessages([]); setQuestion(""); setError(""); }}>Percakapan baru</button>
            <label>
              Mode
              <select
                aria-label="Mode jawaban"
                value={mode}
                onChange={(event) => setMode(event.target.value)}
                disabled={busy}
              >
                <option value="qwen">{selectedModel?.model_name || "Model"} · generatif</option>
                <option value="extractive">Kutipan sumber</option>
              </select>
            </label>

            <details className="mt-details">
              <summary>
                <Glyph name="more" />
                Detail model
              </summary>

              <div>
                <strong>Run yang dipilih</strong>
                <code>{selectedModel?.run_id || "—"}</code>
                <p>
                  Model dimuat di Colab ketika pertanyaan dikirim. Setiap
                  pertanyaan diproses mandiri.
                </p>
                <p>{model?.training_sample_counts ? `Sampel aktual run tercatat: train ${model.training_sample_counts.train || 0} · evaluasi ${model.training_sample_counts.eval || 0}` : "Jumlah sampel training aktual belum tersedia dari catatan run ini."}</p>
                <SourceAnalysis report={model?.source_analysis || restore.source_analysis} />
                <h3>Fakta proyek untuk model ini</h3>
                <p>Pilih fakta proyek atau QA terkurasi yang sudah Anda setujui sebagai sumber. Ini memperbarui indeks inferensi, bukan melatih ulang atau mengubah manifest run lama.</p>
                {!factSources.length && <p>Belum ada fakta proyek atau QA terkurasi disetujui. Atur perannya di halaman Datasets.</p>}
                {factSources.map(item => <label key={item.id} style={{ display: "block", overflowWrap: "anywhere" }}><input type="checkbox" disabled={busy} checked={sourceDatasetIds.includes(item.id)} onChange={event => setSourceDatasetIds(previous => event.target.checked ? [...previous, item.id] : previous.filter(id => id !== item.id))} /> {item.name} · {item.drive_status === "saved" ? "Drive terverifikasi" : "belum terverifikasi"}</label>)}
                <button type="button" disabled={busy || !selected || !factsReady} onClick={() => restoreModel(true)}>Perbarui sumber tanpa training</button>
                <button
                  type="button"
                  disabled={busy || !messages.length}
                  onClick={() => setMessages([])}
                >
                  Bersihkan percakapan
                </button>
              </div>
            </details>
          </div>

          <div
            className="mt-transcript"
            ref={scroll}
            aria-live="polite"
            aria-busy={busy}
          >
            {!messages.length && (
              <div className="mt-welcome">
                <div className="mt-orbit">
                  <span>CT</span>
                </div>

                <span className="mt-eyebrow">WORKSPACE PERCAKAPAN</span>

                <h3>
                  {available
                    ? "Mulai dari satu pertanyaan."
                    : "Bawa kembali modelmu."}
                </h3>

                <p>
                  {available
                    ? "Uji identitas, pengetahuan, dan kesesuaian jawaban dengan sumber proyekmu."
                    : "Pilih hasil training di sebelah kiri, hubungkan T4, lalu pulihkan model beserta indeks sumbernya."}
                </p>

                {available ? (
                  <div className="mt-prompts">
                    {["Ringkas informasi utama sumber proyek.", "Apa yang dapat dijelaskan dari data proyek?"].map(
                      (text) => (
                        <button
                          type="button"
                          key={text}
                          onClick={() => setQuestion(text)}
                        >
                          {text}
                          <span>↗</span>
                        </button>
                      ),
                    )}
                  </div>
                ) : (
                  <div className="mt-welcome-actions">
                    <button
                      className="mt-primary"
                      type="button"
                      onClick={restoreModel}
                      disabled={!selected || busy || checking}
                    >
                      <Glyph name="restore" />
                      {restoring ? "Memulihkan…" : "Pulihkan & jalankan"}
                    </button>

                    <button
                      className="mt-link"
                      type="button"
                      onClick={() => navigate("colab")}
                      disabled={busy}
                    >
                      Hubungkan T4 ↗
                    </button>
                  </div>
                )}
              </div>
            )}

            {messages.map((item) => (
              <article className={`mt-message ${item.role}`} key={item.id}>
                <span className="mt-message-avatar">
                  {item.role === "user" ? "K" : "CT"}
                </span>

                <div className="mt-message-content">
                  <div className="mt-message-label">
                    <strong>
                      {item.role === "user"
                        ? "Kamu"
                        : item.mode === "qwen"
                          ? selectedModel?.model_name
                          : "Kutipan sumber"}
                    </strong>

                    {item.role === "assistant" && (
                      <button
                        type="button"
                        className="mt-copy"
                        onClick={() => copy(item)}
                        aria-label="Salin jawaban"
                      >
                        <Glyph name={copied === item.id ? "check" : "copy"} />
                      </button>
                    )}
                  </div>

                  <p>{item.text}</p>
                  {item.notice && <small className="mt-answer-basis">{item.notice}</small>}

                  {item.sources?.length > 0 && (
                    <details className="mt-sources">
                      <summary>{item.sources.length} sumber</summary>
                      <p>{item.sources.join(", ")}</p>
                      {item.evidence?.map((span, index) => <blockquote key={index}>
                        <p>{span.quote}</p><small>{span.source} · passage {span.passage_id}</small>
                      </blockquote>)}
                    </details>
                  )}
                </div>
              </article>
            ))}

            {sending && (
              <div className="mt-thinking" role="status">
                <span />
                <span />
                <span />
                Colab sedang memproses pertanyaan…
              </div>
            )}
          </div>

          {(restoring ||
            ["completed", "failed", "uncertain"].includes(restore.status)) && (
            <div className={`mt-restore-state ${restore.status}`} role="status">
              <strong>
                {restore.status === "completed"
                  ? restore.source_analysis?.sources_ready === false ? "Model dipulihkan · indeks sumber kosong" : "Pemulihan selesai; periksa status sumber"
                  : restoring
                    ? "Memulihkan model"
                    : "Pemulihan perlu diperiksa"}
              </strong>

              <span>{restore.message}</span>

              {restoring && (
                <progress
                  max="100"
                  {...(restore.status === "uploading"
                    ? { value: restore.percent || 0 }
                    : {})}
                />
              )}
            </div>
          )}

          {model?.source_warning && !restoring && <p role="alert" className="mt-state-note">{model.source_warning}</p>}
          {!available && model?.message && !restoring && (
            <p className="mt-state-note">{model.message}</p>
          )}

          <footer className="mt-compose-wrap">
            <form className="mt-compose" onSubmit={send}>
              <textarea
                aria-label="Pesan untuk model"
                placeholder={
                  available
                    ? model?.sources_ready === false ? "Indeks kosong; jawaban generatif memakai pengetahuan umum." : "Tanyakan sesuatu tentang sumber proyekmu…"
                    : "Pertanyaan aktif setelah model dipulihkan."
                }
                value={question}
                onChange={(event) => setQuestion(event.target.value)}
                onKeyDown={(event) => {
                  if (
                    event.key === "Enter" &&
                    !event.shiftKey &&
                    !event.nativeEvent.isComposing
                  ) {
                    event.preventDefault();
                    send();
                  }
                }}
                maxLength={2000}
                rows={2}
                disabled={!selected || busy}
              />

              <button
                className="mt-send"
                aria-label="Kirim pertanyaan"
                type="submit"
                disabled={!available || !question.trim() || busy || checking}
              >
                <Glyph name="send" />
              </button>
            </form>

            <div className="mt-compose-note">
              <span>
                Jawaban model dapat keliru. Pertanyaan tidak menjadi data
                training.
              </span>
              <span>{question.length}/2.000</span>
            </div>
          </footer>
        </section>
      </div>
    </div>
  );
}
