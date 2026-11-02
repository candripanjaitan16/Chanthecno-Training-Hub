const API = (import.meta.env.VITE_API_BASE_URL || (import.meta.env.DEV ? "http://127.0.0.1:8000" : "")).replace(/\/$/, "");

async function request(path, options = {}, timeout = 30000) {
  const controller = new AbortController();
  const signal = options.signal;
  const abort = () => controller.abort();
  if (signal?.aborted) controller.abort();
  else signal?.addEventListener("abort", abort, { once: true });
  const timer = window.setTimeout(abort, timeout);
  try {
    const response = await fetch(`${API}/api/datasets${path}`, {
      ...options, signal: controller.signal, cache: "no-store",
      headers: { Accept: "application/json", ...options.headers },
    });
    const data = await response.json();
    if (!response.ok) throw new Error(typeof data.detail === "string" ? data.detail : `Permintaan gagal (HTTP ${response.status}).`);
    return data;
  } catch (error) {
    if (error.name === "AbortError") throw new Error("Permintaan terputus atau melewati batas waktu. Muat ulang daftar sebelum mencoba lagi.");
    if (error instanceof TypeError) throw new Error("Backend belum dapat dihubungi. Pastikan chanthecno-hub berjalan.");
    throw error;
  } finally {
    window.clearTimeout(timer);
    signal?.removeEventListener("abort", abort);
  }
}
const projectPath = id => `/projects/${encodeURIComponent(id)}`;
const sourcePath = (id, source) => `${projectPath(id)}/${encodeURIComponent(source)}`;

export async function listDatasets(id, signal) {
  const data = await request(projectPath(id), { signal });
  if (!Array.isArray(data) || data.some(item => !item?.id || !["txt", "url"].includes(item.type))) {
    throw new Error("Format daftar dataset dari backend tidak sesuai.");
  }
  return data;
}
export function uploadDataset(id, file) {
  return request(`${projectPath(id)}/txt`, {
    method: "POST", body: file,
    headers: { "Content-Type": "text/plain; charset=utf-8", "X-CTH-Source-Name": encodeURIComponent(file.name) },
  }, 1800000);
}
export function saveDatasetUrl(id, url) {
  return request(`${projectPath(id)}/url`, {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ url }),
  });
}
export const deleteDataset = (id, source) => request(sourcePath(id, source), { method: "DELETE" });
export const syncDataset = (id, source) => request(`${sourcePath(id, source)}/sync`, { method: "POST" });
export const restoreDataset = (id, source) => request(`${sourcePath(id, source)}/restore`, { method: "POST" }, 1800000);
export const previewDataset = (id, source) => request(`${sourcePath(id, source)}/preview`);
export const analyzeDatasets = (id, datasetIds, signal) => request(`${projectPath(id)}/analysis`, {
  method: "POST", signal, headers: { "Content-Type": "application/json" }, body: JSON.stringify({ dataset_ids: datasetIds }),
}, 120000);
export const setSourceMode = (id, source, mode) => request(`${sourcePath(id, source)}/source-mode`, {
  method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ source_mode: mode }),
}, 120000);
