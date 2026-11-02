import UpdateStatus from "./UpdateStatus";
import Icon from "./Icon";
export const menus = [
  { id: "overview", label: "Overview", icon: "overview" },
  { id: "datasets", label: "Datasets", icon: "datasets" },
  { id: "training", label: "Training Workspace", icon: "training" },
  { id: "testing", label: "Model Testing", icon: "testing" },
  { id: "deployments", label: "Deployments", icon: "deployments" },
  { id: "drive", label: "Google Drive", icon: "drive" },
  { id: "colab", label: "Colab Connection", icon: "colab" },
];
export default function Sidebar({ activePage, onNavigate, open, onClose }) {
  return <>
    {open && <button className="cth-backdrop" aria-label="Tutup navigasi" onClick={onClose} />}
    <aside className={`cth-sidebar ${open ? "is-open" : ""}`}>
      <div className="cth-brand">
        <img src="/chanthecno.svg" alt="" onError={event => { event.currentTarget.hidden = true; }} />
        <div><strong>ChanThecno</strong><span>Training Hub</span></div>
      </div>
      <nav aria-label="Navigasi utama">{menus.map(menu => <a key={menu.id}
        href={`#${menu.id}`} aria-current={activePage === menu.id ? "page" : undefined}
        className={activePage === menu.id ? "is-active" : ""}
        onClick={() => { onNavigate(menu.id); onClose(); }}>
        <Icon name={menu.icon} /><span>{menu.label}</span>
      </a>)}</nav>
      <div className="cth-sidebar-footer"><UpdateStatus /></div>
    </aside>
  </>;
}
