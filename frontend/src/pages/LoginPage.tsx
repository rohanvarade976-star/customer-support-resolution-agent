// Log in. Split screen: what the product does on one side, the form on the other.
// The demo account cards only fill in the email; the password is never in the code.
// The role comes from the API (GET /me), never from which card was picked.

import { Activity, ArrowRight, LifeBuoy, PackageSearch, ShieldCheck, type LucideIcon } from "lucide-react";
import { useRef, useState, type SyntheticEvent } from "react";
import { Navigate, useNavigate } from "react-router-dom";

import { describeError } from "../api/client";
import { homePathFor, isReviewer, useAuth } from "../auth/auth";
import { Brand } from "../components/Sidebar";
import { Alert } from "../components/ui/alert";
import { Badge } from "../components/ui/badge";
import { Button } from "../components/ui/button";
import { Input, Label } from "../components/ui/form";
import { cn } from "../lib/utils";

// Seeded demo accounts (backend/data/seed). Emails only.
const DEMO_ACCOUNTS = [
  { email: "priya@example.com", name: "Priya Sharma", role: "Customer" },
  { email: "arjun@example.com", name: "Arjun Mehta", role: "Customer" },
  { email: "anita@example.com", name: "Anita Desai", role: "Reviewer" },
];

const VALUE_POINTS: { icon: LucideIcon; title: string; text: string }[] = [
  {
    icon: PackageSearch,
    title: "Answers grounded in real records",
    text: "Every reply is checked against your live order data and our written policy.",
  },
  {
    icon: ShieldCheck,
    title: "A person approves every refund",
    text: "Specialist agents propose, code verifies, and a reviewer signs off before money moves.",
  },
  {
    icon: Activity,
    title: "Follow every step live",
    text: "Watch your request move from received to resolved, as it happens.",
  },
];

export function LoginPage() {
  const { user, login } = useAuth();
  const navigate = useNavigate();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const passwordRef = useRef<HTMLInputElement>(null);

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
    <div className="grid min-h-dvh lg:grid-cols-[minmax(0,5fr)_minmax(0,6fr)]">
      {/* The pitch: a full panel on wide screens. */}
      <aside className="relative hidden overflow-hidden bg-primary-950 px-12 py-12 text-white lg:flex lg:flex-col xl:px-16">
        <div
          aria-hidden="true"
          className="absolute -top-32 -right-32 size-96 rounded-full bg-primary-600/30 blur-3xl"
        />
        <div aria-hidden="true" className="absolute -bottom-40 -left-24 size-96 rounded-full bg-primary-800/40 blur-3xl" />
        <div className="relative flex items-center gap-2.5">
          <span className="grid size-8 place-items-center rounded-lg bg-white/10 ring-1 ring-white/20">
            <LifeBuoy className="size-4.5" aria-hidden="true" />
          </span>
          <span className="text-[0.9375rem] font-semibold tracking-tight">Support Resolution</span>
        </div>
        <div className="relative my-auto max-w-md py-12">
          <h2 className="text-3xl leading-tight font-semibold tracking-tight">
            Customer support that resolves, not just responds.
          </h2>
          <p className="mt-4 text-base text-primary-200">
            AI specialists investigate each case, plain code checks every proposal, and people stay
            in charge of the money.
          </p>
          <ul className="mt-10 space-y-6">
            {VALUE_POINTS.map(({ icon: Icon, title, text }) => (
              <li key={title} className="flex gap-4">
                <span className="grid size-10 shrink-0 place-items-center rounded-lg bg-white/10 ring-1 ring-white/15">
                  <Icon className="size-5 text-primary-200" aria-hidden="true" />
                </span>
                <div>
                  <p className="font-medium">{title}</p>
                  <p className="mt-0.5 text-sm text-primary-200">{text}</p>
                </div>
              </li>
            ))}
          </ul>
        </div>
        <p className="relative text-xs text-primary-300">A demo of a multi-agent support system.</p>
      </aside>

      {/* The form. */}
      <main className="flex items-center justify-center px-4 py-10 sm:px-8">
        <div className="w-full max-w-sm">
          <div className="mb-8 lg:hidden">
            <Brand />
          </div>
          <h1 className="text-2xl font-semibold tracking-tight text-gray-900">Log in</h1>
          <p className="mt-1 text-sm text-gray-600">Welcome back. Sign in to your account to continue.</p>

          <form onSubmit={(event) => void submit(event)} className="mt-8 space-y-5">
            <div>
              <Label htmlFor="email">Email</Label>
              <Input
                id="email"
                type="email"
                autoComplete="username"
                placeholder="you@example.com"
                required
                value={email}
                onChange={(event) => {
                  setEmail(event.target.value);
                }}
              />
            </div>
            <div>
              <Label htmlFor="password">Password</Label>
              <Input
                id="password"
                ref={passwordRef}
                type="password"
                autoComplete="current-password"
                required
                value={password}
                onChange={(event) => {
                  setPassword(event.target.value);
                }}
              />
            </div>
            {error !== null && <Alert tone="danger">{error}</Alert>}
            <Button type="submit" size="lg" className="w-full" loading={busy}>
              {busy ? "Logging in…" : "Log in"}
              {!busy && <ArrowRight aria-hidden="true" />}
            </Button>
          </form>

          <section aria-labelledby="demo-heading" className="mt-10">
            <div className="flex items-center gap-3">
              <span className="h-px flex-1 bg-gray-200" />
              <h2 id="demo-heading" className="text-xs font-medium tracking-wide text-gray-500 uppercase">
                Demo accounts
              </h2>
              <span className="h-px flex-1 bg-gray-200" />
            </div>
            <p className="mt-3 text-center text-xs text-gray-500">
              Pick one to fill in its email, then enter the demo password from your setup.
            </p>
            <ul className="mt-4 grid gap-2">
              {DEMO_ACCOUNTS.map((account) => {
                const selected = email === account.email;
                return (
                  <li key={account.email}>
                    <button
                      type="button"
                      aria-pressed={selected}
                      onClick={() => {
                        setEmail(account.email);
                        passwordRef.current?.focus();
                      }}
                      className={cn(
                        "flex w-full cursor-pointer items-center gap-3 rounded-lg border bg-white px-3 py-2.5 text-left shadow-xs transition-colors hover:bg-gray-50",
                        selected ? "border-primary-500 ring-1 ring-primary-500" : "border-gray-200",
                      )}
                    >
                      <span
                        aria-hidden="true"
                        className="grid size-9 shrink-0 place-items-center rounded-full bg-primary-100 text-xs font-semibold text-primary-800"
                      >
                        {account.name
                          .split(" ")
                          .map((part) => part.charAt(0))
                          .join("")}
                      </span>
                      <span className="min-w-0 flex-1">
                        <span className="block text-sm font-medium text-gray-900">{account.name}</span>
                        <span className="block truncate text-xs text-gray-500">{account.email}</span>
                      </span>
                      <Badge tone={account.role === "Reviewer" ? "primary" : "neutral"}>{account.role}</Badge>
                    </button>
                  </li>
                );
              })}
            </ul>
          </section>
        </div>
      </main>
    </div>
  );
}
