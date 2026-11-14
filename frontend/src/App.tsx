// Routes. Each area is guarded by role: customers and reviewers each see only their pages.
// The API checks every request again; these guards only keep people on the right pages.
// ToastProvider wraps everything so any page can confirm an action with a toast.

import { Navigate, Outlet, Route, Routes } from "react-router-dom";

import { homePathFor, isReviewer, useAuth } from "./auth/auth";
import { AppShell } from "./components/AppShell";
import { ToastProvider } from "./components/ui/toast";
import { ApprovalCasePage } from "./pages/ApprovalCasePage";
import { ApprovalQueuePage } from "./pages/ApprovalQueuePage";
import { HomePage } from "./pages/HomePage";
import { LoginPage } from "./pages/LoginPage";
import { MemoryPage } from "./pages/MemoryPage";
import { NewComplaintPage } from "./pages/NewComplaintPage";
import { OrdersPage } from "./pages/OrdersPage";
import { RunPage } from "./pages/RunPage";
import { TicketsPage } from "./pages/TicketsPage";

type Area = "customer" | "reviewer" | "any";

function RequireRole({ area }: { area: Area }) {
  const { user } = useAuth();
  if (user === null) return <Navigate to="/login" replace />;
  const reviewer = isReviewer(user);
  if ((area === "customer" && reviewer) || (area === "reviewer" && !reviewer)) {
    return <Navigate to={homePathFor(reviewer)} replace />;
  }
  return <Outlet />;
}

function Home() {
  const { user } = useAuth();
  return <Navigate to={user === null ? "/login" : homePathFor(isReviewer(user))} replace />;
}

export function App() {
  return (
    <ToastProvider>
      <Routes>
        <Route path="/login" element={<LoginPage />} />
        <Route element={<AppShell />}>
          <Route element={<RequireRole area="customer" />}>
            <Route path="/home" element={<HomePage />} />
            <Route path="/orders" element={<OrdersPage />} />
            <Route path="/complaints/new" element={<NewComplaintPage />} />
            <Route path="/tickets" element={<TicketsPage />} />
            <Route path="/memory" element={<MemoryPage />} />
          </Route>
          <Route element={<RequireRole area="reviewer" />}>
            <Route path="/approvals" element={<ApprovalQueuePage />} />
            <Route path="/approvals/:approvalId" element={<ApprovalCasePage />} />
          </Route>
          <Route element={<RequireRole area="any" />}>
            <Route path="/runs/:runId" element={<RunPage />} />
          </Route>
        </Route>
        <Route path="*" element={<Home />} />
      </Routes>
    </ToastProvider>
  );
}
