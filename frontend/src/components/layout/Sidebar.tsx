import { useEffect } from 'react';
import { useLocation, useNavigate } from 'react-router-dom';
import { BookOpen, LayoutDashboard, Moon, PenLine, Settings, Sun, UserCircle, Users } from 'lucide-react';

import { projectApi } from '../../services/api';
import { usePermissionStore, useProjectStore, useSettingsStore } from '../../stores';

// `adminOnly`：只对管理员显示的入口。模型的密钥与档位属于**服务器级**配置，
// 普通作者不需要也不应该看到；判定结果来自服务端（`/auth/permissions`），
// 与后端 `require_maintainer` 同源。
const NAV_ITEMS = [
  { path: '/', icon: LayoutDashboard, label: '项目总览', adminOnly: false, testId: 'nav-dashboard' },
  { path: '/settings', icon: Settings, label: '模型设置', adminOnly: true, testId: 'nav-settings' },
  { path: '/admin', icon: Users, label: '运营后台', adminOnly: true, testId: 'nav-admin' },
  { path: '/account', icon: UserCircle, label: '账户', adminOnly: false, testId: 'nav-account' },
];

export default function Sidebar({
  className = '',
  onNavigate,
}: {
  className?: string;
  onNavigate?: () => void;
}) {
  const navigate = useNavigate();
  const location = useLocation();
  const { theme, toggleTheme } = useSettingsStore();
  const isAdmin = usePermissionStore((state) => state.isAdmin === true);
  const navItems = NAV_ITEMS.filter((item) => !item.adminOnly || isAdmin);

  const isActive = (path: string) => {
    if (path === '/') return location.pathname === '/';
    return location.pathname.startsWith(path);
  };

  return (
    <aside className={`flex w-56 shrink-0 flex-col border-r border-surface-200 bg-white dark:border-surface-700 dark:bg-surface-850 ${className}`}>
      <div
        className="flex cursor-pointer items-center gap-2.5 border-b border-surface-200 px-4 py-4 dark:border-surface-700"
        onClick={() => {
          navigate('/');
          onNavigate?.();
        }}
      >
        <div className="flex h-8 w-8 items-center justify-center rounded-lg bg-primary-600">
          <PenLine size={18} className="text-white" />
        </div>
        <span className="text-base font-bold text-surface-800 dark:text-surface-200">NovelCraft</span>
      </div>

      <nav className="flex-1 space-y-1 p-3">
        {navItems.map((item) => (
          <button
            key={item.path}
            data-testid={item.testId}
            onClick={() => {
              navigate(item.path);
              onNavigate?.();
            }}
            className={`sidebar-link w-full ${isActive(item.path) ? 'active' : ''}`}
          >
            <item.icon size={18} />
            <span>{item.label}</span>
          </button>
        ))}

        <div className="mt-3 border-t border-surface-200 pt-3 dark:border-surface-700">
          <p className="mb-2 px-3 text-xs font-semibold uppercase tracking-wider text-surface-400">最近项目</p>
          <ProjectQuickList />
        </div>
      </nav>

      <div className="border-t border-surface-200 p-3 dark:border-surface-700">
        <button onClick={toggleTheme} className="sidebar-link w-full">
          {theme === 'light' ? <Moon size={18} /> : <Sun size={18} />}
          <span>{theme === 'light' ? '切换深色模式' : '切换浅色模式'}</span>
        </button>
      </div>
    </aside>
  );
}

function ProjectQuickList() {
  const navigate = useNavigate();
  const location = useLocation();
  const projects = useProjectStore((state) => state.projects);
  const setProjects = useProjectStore((state) => state.setProjects);

  useEffect(() => {
    let active = true;

    projectApi.list()
      .then((json) => {
        if (active) setProjects(json.data || []);
      })
      .catch(() => {
        // Keep the last local snapshot when the backend is temporarily unavailable.
      });

    return () => {
      active = false;
    };
  }, [setProjects]);

  const visibleProjects = [...projects]
    .sort((a, b) => {
      if (a.isStarred !== b.isStarred) return a.isStarred ? -1 : 1;
      return b.updatedAt.localeCompare(a.updatedAt);
    })
    .slice(0, 8);

  if (visibleProjects.length === 0) {
    return <div className="px-3 text-xs text-surface-400">还没有最近项目。</div>;
  }

  return (
    <div className="space-y-0.5">
      {visibleProjects.map((project) => (
        <button
          key={project.id}
          onClick={() => navigate(`/project/${project.id}`)}
          className={`sidebar-link w-full text-xs ${location.pathname.includes(project.id) ? 'active' : ''}`}
        >
          <BookOpen size={14} />
          <div className="truncate text-left">
            <div className="truncate">{project.name}</div>
          </div>
        </button>
      ))}
    </div>
  );
}
