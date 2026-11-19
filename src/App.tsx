import {
  BarChart3,
  Clapperboard,
  FolderKanban,
  LayoutDashboard,
  Plus,
  Settings,
  Info,
  ListVideo,
} from "lucide-react";

const navigation = [
  { label: "Dashboard", icon: LayoutDashboard, active: true },
  { label: "New Project", icon: Plus },
  { label: "My Clips", icon: Clapperboard },
  { label: "Projects", icon: FolderKanban },
  { label: "Processing Queue", icon: ListVideo },
  { label: "Settings", icon: Settings },
  { label: "About", icon: Info },
];

export default function App() {
  return (
    <main className="app-shell">
      <aside className="sidebar">
        <div className="brand">
          <div className="brand-mark"><BarChart3 size={20} /></div>
          <span>AIClipping</span>
        </div>
        <nav aria-label="Primary navigation">
          {navigation.map(({ label, icon: Icon, active }) => (
            <button className={active ? "nav-item active" : "nav-item"} key={label}>
              <Icon size={18} />
              <span>{label}</span>
            </button>
          ))}
        </nav>
      </aside>

      <section className="content">
        <header className="page-header">
          <div>
            <p className="eyebrow">Workspace</p>
            <h1>Dashboard</h1>
          </div>
          <button className="primary-button"><Plus size={18} /> New Project</button>
        </header>

        <section className="empty-state">
          <div className="empty-icon"><Clapperboard size={28} /></div>
          <h2>Project foundation ready</h2>
          <p>No projects yet. Video processing will be enabled in a later development phase.</p>
        </section>
      </section>
    </main>
  );
}
