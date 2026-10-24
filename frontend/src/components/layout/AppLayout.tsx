import { Menu, X } from 'lucide-react';
import { useState } from 'react';
import { Outlet } from 'react-router-dom';
import Sidebar from './Sidebar';

export default function AppLayout() {
  const [mobileSidebarOpen, setMobileSidebarOpen] = useState(false);

  return (
    <div className="flex h-screen overflow-hidden bg-surface-50 dark:bg-surface-900">
      <Sidebar className="hidden lg:flex" />

      {mobileSidebarOpen && (
        <div className="fixed inset-0 z-40 flex lg:hidden" data-testid="mobile-sidebar-overlay">
          <Sidebar
            className="relative z-10 h-full w-[min(20rem,85vw)] max-w-full shadow-2xl"
            onNavigate={() => setMobileSidebarOpen(false)}
          />
          <button
            className="flex-1 bg-surface-900/45"
            aria-label="关闭主导航"
            onClick={() => setMobileSidebarOpen(false)}
          />
        </div>
      )}

      <main className="flex flex-1 flex-col overflow-hidden">
        <div className="flex h-14 shrink-0 items-center justify-between border-b border-surface-200 bg-white px-4 dark:border-surface-700 dark:bg-surface-850 lg:hidden">
          <div className="flex items-center gap-3">
            <div className="flex h-8 w-8 items-center justify-center rounded-lg bg-primary-600 text-white">
              {mobileSidebarOpen ? <X size={18} /> : <Menu size={18} />}
            </div>
            <span className="text-base font-semibold text-surface-800 dark:text-surface-200">NovelCraft</span>
          </div>
          <button
            className="btn-ghost px-2.5"
            data-testid="toggle-mobile-sidebar"
            aria-label={mobileSidebarOpen ? '关闭主导航' : '打开主导航'}
            onClick={() => setMobileSidebarOpen((previous) => !previous)}
          >
            {mobileSidebarOpen ? <X size={18} /> : <Menu size={18} />}
          </button>
        </div>
        <Outlet />
      </main>
    </div>
  );
}
