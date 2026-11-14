// Routes. Each area is guarded by role: customers and reviewers each see only their pages.
// The API checks every request again; these guards only keep people on the right pages.

import { Navigate, Outlet, Route, Routes } from "react-router-dom";

import { homePathFor, isReviewer, useAuth } from "./auth/auth";
import { Layout } from "./components/Layout";
import { ApprovalCasePage } from "./pages/ApprovalCasePage";
import { ApprovalQueuePage } from "./pages/ApprovalQueuePage";
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
    <Routes>
      <Route path="/login" element={<LoginPage />} />
      <Route element={<Layout />}>
        <Route element={<RequireRole area="customer" />}>
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
  );
}
