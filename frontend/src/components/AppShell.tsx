// The frame around every signed-in page: a fixed sidebar on wide screens (a slide-over menu on
// small ones), the top bar, and a centred content column with a consistent width.

import { useState } from "react";
import { Outlet } from "react-router-dom";

import { Sidebar } from "./Sidebar";
import { TopBar } from "./TopBar";
import { Sheet } from "./ui/dialog";

export function AppShell() {
  const [menuOpen, setMenuOpen] = useState(false);
  const closeMenu = () => {
    setMenuOpen(false);
  };

  return (
    <div className="min-h-dvh">
      <a
        href="#main"
        className="sr-only z-50 rounded-lg bg-white px-4 py-2 font-medium text-primary-700 shadow-lg focus:not-sr-only focus:fixed focus:top-3 focus:left-3"
      >
        Skip to content
      </a>

      <aside className="fixed inset-y-0 left-0 z-40 hidden w-60 border-r border-gray-200 bg-white lg:block">
        <Sidebar />
      </aside>
      <Sheet open={menuOpen} onClose={closeMenu} label="Menu">
        <Sidebar onNavigate={closeMenu} />
      </Sheet>

      <div className="lg:pl-60">
        <TopBar
          onOpenMenu={() => {
            setMenuOpen(true);
          }}
        />
        <main id="main" tabIndex={-1} className="mx-auto w-full max-w-6xl px-4 py-6 outline-none sm:px-6 lg:px-8 lg:py-8">
          <Outlet />
        </main>
      </div>
    </div>
  );
}
