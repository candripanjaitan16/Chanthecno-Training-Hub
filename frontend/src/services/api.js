const API_BASE = (import.meta.env.VITE_API_BASE_URL || (import.meta.env.DEV ? "http://127.0.0.1:8000" : "")).replace(/\/$/, "");

async function request(path, { signal, timeout = 30000, ...options } = {}) {
  const controller = new AbortController();
  const abort = () => controller.abort();
  if (signal?.aborted) controller.abort();
  else signal?.addEventListener("abort", abort, { once: true });
  const timer = window.setTimeout(abort, timeout);
  try {
    const response = await fetch(`${API_BASE}${path}`, {
      ...options, signal: controller.signal, cache: "no-store",
      headers: { Accept: "application/json", ...options.headers },
    });
    if (!response.ok) throw new Error(`Permintaan gagal (HTTP ${response.status}).`);
    return await response.json();
  } catch (error) {
    if (error.name === "AbortError") throw error;
    if (error instanceof TypeError) throw new Error("Backend tidak dapat dihubungi. Periksa backend dan alamat frontend localhost:5173.");
    throw error;
  } finally {
    window.clearTimeout(timer);
    signal?.removeEventListener("abort", abort);
  }
}

export async function getProjects(signal) {
  const data = await request("/api/projects/", { signal });
  if (!Array.isArray(data) || !data.every(p => p && typeof p === "object" && ["string", "number"].includes(typeof p.id) && typeof p.name === "string")) {
    throw new Error("Format daftar proyek dari backend tidak sesuai.");
  }
  return data;
}

export async function getDriveStatus(signal) {
  const data = await request("/api/connections/google-drive", { signal });
  if (!data || !["connected", "disconnected", "error"].includes(data.status)) throw new Error("Format status Drive tidak sesuai.");
  return { status: data.status, message: typeof data.message === "string" ? data.message : "" };
}

export function createProject(project) {
  return request("/api/projects/", {
    method: "POST", timeout: 120000,
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(project),
  });
}

export function getLocalState(signal) { return request("/api/local-state", { signal }); }
