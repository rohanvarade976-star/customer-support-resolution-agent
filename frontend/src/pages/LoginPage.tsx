// Log in as a demo customer or reviewer. The role comes from the API (GET /me), not the form.

import { useState, type SyntheticEvent } from "react";
import { Navigate, useNavigate } from "react-router-dom";

import { describeError } from "../api/client";
import { homePathFor, isReviewer, useAuth } from "../auth/auth";
import { buttonClass, ErrorBanner, inputClass } from "../components/ui";

// Seeded demo accounts (backend/data/seed). Emails only: the password is never in the code.
const DEMO_ACCOUNTS = [
  { email: "priya@example.com", label: "Priya (customer)" },
  { email: "arjun@example.com", label: "Arjun (customer)" },
  { email: "anita@example.com", label: "Anita (reviewer)" },
];

export function LoginPage() {
  const { user, login } = useAuth();
  const navigate = useNavigate();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  if (user !== null) return <Navigate to={homePathFor(isReviewer(user))} replace />;

  async function submit(event: SyntheticEvent) {
    event.preventDefault();
    setBusy(true);
    setError(null);
    try {
      const me = await login(email.trim(), password);
      void navigate(homePathFor(isReviewer(me)), { replace: true });
    } catch (caught) {
      setError(describeError(caught));
      setBusy(false);
    }
  }

  return (
    <main className="mx-auto mt-16 max-w-sm px-4">
      <h1 className="mb-1 text-2xl font-semibold">Support Resolution</h1>
      <p className="mb-6 text-sm text-slate-600">Log in with a demo account.</p>
      <form onSubmit={(event) => void submit(event)} className="space-y-4 rounded-lg border bg-white p-5 shadow-sm">
        <div>
          <label htmlFor="email" className="mb-1 block text-sm font-medium">
            Email
          </label>
          <input
            id="email"
            type="email"
            autoComplete="username"
            required
            className={inputClass}
            value={email}
            onChange={(event) => {
              setEmail(event.target.value);
            }}
          />
        </div>
        <div>
          <label htmlFor="password" className="mb-1 block text-sm font-medium">
            Password
          </label>
          <input
            id="password"
            type="password"
            autoComplete="current-password"
            required
            className={inputClass}
            value={password}
            onChange={(event) => {
              setPassword(event.target.value);
            }}
          />
        </div>
        <ErrorBanner message={error} />
        <button type="submit" className={`${buttonClass} w-full`} disabled={busy}>
          {busy ? "Logging in…" : "Log in"}
        </button>
      </form>
      <div className="mt-4 text-sm text-slate-600">
        <p className="mb-1">Demo accounts (password: your DEMO_PASSWORD):</p>
        <ul className="flex flex-wrap gap-2">
          {DEMO_ACCOUNTS.map((account) => (
            <li key={account.email}>
              <button
                type="button"
                className="rounded border border-slate-300 bg-white px-2 py-0.5 hover:bg-slate-50"
                onClick={() => {
                  setEmail(account.email);
                }}
              >
                {account.label}
              </button>
            </li>
          ))}
        </ul>
      </div>
    </main>
  );
}
