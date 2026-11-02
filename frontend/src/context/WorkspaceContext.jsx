import { createContext, useCallback, useContext, useEffect, useRef, useState } from "react";
import { getDriveStatus, getProjects, createProject, getLocalState } from "../services/api";

import { listDatasets, uploadDataset, saveDatasetUrl, deleteDataset, syncDataset } from "../services/datasets";

const WorkspaceContext = createContext(null);
function savedProject() {
  try { return localStorage.getItem("cth-selected-project") || ""; } catch { return ""; }
}
function readableError(error) {
  return error.name === "AbortError" ? "Pemeriksaan melewati batas waktu. Coba lagi." : error.message;
}
export function WorkspaceProvider({ children }) {
  const [projects, setProjects] = useState([]);
  const [projectsLoading, setProjectsLoading] = useState(true);
  const [projectsError, setProjectsError] = useState("");
  const [selectedId, setSelectedId] = useState(savedProject);
  const [drive, setDrive] = useState({ status: "loading", message: "Memeriksa koneksi..." });
  const [drafts, setDrafts] = useState({});
  const [datasetLists, setDatasetLists] = useState({});
  const [datasetLoading, setDatasetLoading] = useState({});
  const [datasetErrors, setDatasetErrors] = useState({});
  const datasetRequests = useRef(new Map());
  const projectsRequest = useRef(null);
  const driveRequest = useRef(null);
  const creating = useRef(false);
  const [isCreating, setIsCreating] = useState(false);

  const selectProject = useCallback((id) => {
    const value = String(id || "");
    setSelectedId(value);
    try {
      if (value) localStorage.setItem("cth-selected-project", value);
      else localStorage.removeItem("cth-selected-project");
    } catch { /* Pemilihan tetap bekerja jika storage browser diblokir. */ }
  }, []);

  const refreshProjects = useCallback(async () => {
    projectsRequest.current?.abort();
    const controller = new AbortController();
    projectsRequest.current = controller;
    setProjectsLoading(true);
    setProjectsError("");
    try {
      const data = await getProjects(controller.signal);
      if (!controller.signal.aborted) setProjects(data);
      return data;
    } catch (error) {
      if (!controller.signal.aborted) setProjectsError(readableError(error));
      return null;
    } finally {
      if (!controller.signal.aborted) setProjectsLoading(false);
    }
  }, []);

  const refreshDrive = useCallback(async () => {
    driveRequest.current?.abort();
    const controller = new AbortController();
    driveRequest.current = controller;
    setDrive({ status: "loading", message: "Memeriksa koneksi..." });
    try {
      const data = await getDriveStatus(controller.signal);
      if (!controller.signal.aborted) setDrive(data);
    } catch (error) {
      if (!controller.signal.aborted) setDrive({ status: "error", message: readableError(error) });
    }
  }, []);

  useEffect(() => {
    const controller = new AbortController();
    getLocalState(controller.signal).then(({ generation }) => {
      if (controller.signal.aborted || !generation) return;
      try {
        const previous = localStorage.getItem("cth-generation");
        if (previous !== generation && generation !== "initial") {
          for (const key of Object.keys(localStorage)) {
            if (key.startsWith("cth-")) localStorage.removeItem(key);
          }
          setSelectedId("");
          setDrafts({});
          setDatasetLists({});
        }
        localStorage.setItem("cth-generation", generation);
      } catch { /* Storage may be disabled. */ }
    }).catch(() => {});
    refreshProjects();
    refreshDrive();
    return () => { controller.abort(); projectsRequest.current?.abort(); driveRequest.current?.abort(); };
  }, [refreshProjects, refreshDrive]);

  useEffect(() => {
    if (!Object.values(drafts).some(items => items.length > 0)) return;
    const warnUnsaved = event => { event.preventDefault(); event.returnValue = ""; };
    window.addEventListener("beforeunload", warnUnsaved);
    return () => window.removeEventListener("beforeunload", warnUnsaved);
  }, [drafts]);

  const addProject = useCallback(async (values) => {
    if (creating.current) throw new Error("Pembuatan proyek masih berjalan.");
    creating.current = true;
    setIsCreating(true);
    try {
      const result = await createProject(values);
      if (result.status !== "success" || result.project_id == null) throw new Error("Respons pembuatan proyek belum dapat dipastikan. Muat ulang daftar sebelum mencoba lagi.");
      selectProject(result.project_id);
      const data = await refreshProjects();
      return { result, listUpdated: data !== null };
    } catch (error) {
      throw new Error(`${readableError(error)} Periksa daftar proyek sebelum mengirim ulang; folder mungkin sudah dibuat oleh backend.`);
    } finally { creating.current = false; setIsCreating(false); }
  }, [refreshProjects, selectProject]);

  const refreshDatasets = useCallback(async (projectId) => {
    const key = String(projectId);
    if (!projectId) return;
    datasetRequests.current.get(key)?.abort();
    const controller = new AbortController();
    datasetRequests.current.set(key, controller);
    setDatasetLoading(previous => ({ ...previous, [key]: true }));
    try {
      const data = await listDatasets(projectId, controller.signal);
      if (!controller.signal.aborted) {
        setDatasetLists(previous => ({ ...previous, [key]: data }));
        setDatasetErrors(previous => ({ ...previous, [key]: "" }));
      }
      return data;
    } catch (error) {
      if (!controller.signal.aborted) setDatasetErrors(previous => ({ ...previous, [key]: error.message }));
      return null;
    } finally {
      if (!controller.signal.aborted) setDatasetLoading(previous => ({ ...previous, [key]: false }));
      if (datasetRequests.current.get(key) === controller) datasetRequests.current.delete(key);
    }
  }, []);

  useEffect(() => {
    if (!selectedId || !projects.some(project => String(project.id) === selectedId)) return;
    refreshDatasets(selectedId);
    const timer = window.setInterval(() => {
      if (document.visibilityState === "visible" && !datasetRequests.current.has(selectedId)) refreshDatasets(selectedId);
    }, 10000);
    return () => {
      window.clearInterval(timer);
      datasetRequests.current.get(selectedId)?.abort();
    };
  }, [selectedId, projects, refreshDatasets]);

  useEffect(() => () => {
    for (const controller of datasetRequests.current.values()) controller.abort();
  }, []);

  async function uploadDatasetFile(projectId, file) {
    const result = await uploadDataset(projectId, file);
    await refreshDatasets(projectId);
    return result;
  }
  async function addDatasetUrl(projectId, url) {
    const result = await saveDatasetUrl(projectId, url);
    await refreshDatasets(projectId);
    return result;
  }
  async function removeDataset(projectId, sourceId) {
    const result = await deleteDataset(projectId, sourceId);
    await refreshDatasets(projectId);
    return result;
  }
  async function retryDatasetSync(projectId, sourceId) {
    const result = await syncDataset(projectId, sourceId);
    await refreshDatasets(projectId);
    return result;
  }

  const selectedProject = projects.find(p => String(p.id) === selectedId) || null;
  const datasets = selectedProject ? datasetLists[String(selectedProject.id)] || [] : [];
  const datasetsLoading = Boolean(datasetLoading[selectedId]);
  const datasetsError = datasetErrors[selectedId] || "";
  const sources = selectedProject ? drafts[String(selectedProject.id)] || [] : [];
  const addSource = (projectId, source) => setDrafts(previous => ({
    ...previous, [projectId]: [...(previous[projectId] || []), source],
  }));
  const removeSource = (projectId, sourceId) => setDrafts(previous => ({
    ...previous, [projectId]: (previous[projectId] || []).filter(s => s.id !== sourceId),
  }));

  return <WorkspaceContext.Provider value={{ projects, projectsLoading, projectsError,
    selectedProject, selectProject, refreshProjects, drive, refreshDrive, addProject,
    isCreating, sources, drafts, addSource, removeSource,
    datasets, datasetsLoading, datasetsError, refreshDatasets, uploadDatasetFile,
    addDatasetUrl, removeDataset, retryDatasetSync }}>{children}</WorkspaceContext.Provider>;
}
export function useWorkspace() {
  const value = useContext(WorkspaceContext);
  if (!value) throw new Error("WorkspaceProvider belum dipasang.");
  return value;
}
