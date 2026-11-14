// The page frame: a header with the links for the user's role, and logout.

import { NavLink, Outlet, useNavigate } from "react-router-dom";

import { isReviewer, useAuth } from "../auth/auth";

const CUSTOMER_LINKS = [
  { to: "/orders", label: "My orders" },
  { to: "/complaints/new", label: "New complaint" },
  { to: "/tickets", label: "My tickets" },
  { to: "/memory", label: "My memory" },
];
const REVIEWER_LINKS = [{ to: "/approvals", label: "Approval queue" }];

export function Layout() {
  const { user, logout } = useAuth();
  const navigate = useNavigate();
  const links = isReviewer(user) ? REVIEWER_LINKS : CUSTOMER_LINKS;

  return (
    <div className="min-h-screen">
      <header className="border-b border-slate-200 bg-white">
        <div className="mx-auto flex max-w-4xl flex-wrap items-center gap-4 px-4 py-3">
          <span className="font-semibold text-indigo-700">Support Resolution</span>
          <nav className="flex flex-wrap gap-3 text-sm">
            {links.map((link) => (
              <NavLink
                key={link.to}
                to={link.to}
                className={({ isActive }) =>
                  isActive ? "font-medium text-indigo-700" : "text-slate-600 hover:text-slate-900"
                }
              >
                {link.label}
              </NavLink>
            ))}
          </nav>
          <div className="ml-auto flex items-center gap-3 text-sm">
            <span className="text-slate-600">{user?.name}</span>
            <button
              type="button"
              className="text-slate-600 underline hover:text-slate-900"
              onClick={() => {
                logout();
                void navigate("/login");
              }}
            >
              Log out
            </button>
          </div>
        </div>
      </header>
      <main className="mx-auto max-w-4xl px-4 py-6">
        <Outlet />
      </main>
    </div>
  );
}
