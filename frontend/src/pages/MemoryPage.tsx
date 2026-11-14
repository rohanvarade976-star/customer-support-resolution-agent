// What the system remembers about the customer's past cases (GET /me/memory, ADR-013), and
// a button to delete all of it (DELETE /me/memory). Codes only: there is no free text.

import { useState } from "react";

import { describeError } from "../api/client";
import type { MemoryDeleted, MemoryItem } from "../api/types";
import { useAuth } from "../auth/auth";
import { Card, ErrorBanner, Loading, PageTitle, secondaryButtonClass } from "../components/ui";
import { useApiGet } from "../hooks/useApiGet";
import { formatDateTime, humanize, memoryOutcomeLabel } from "../lib/labels";

export function MemoryPage() {
  const { api } = useAuth();
  const { data, error, loading, reload } = useApiGet<MemoryItem[]>("/me/memory");
  const [notice, setNotice] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);

  async function deleteAll() {
    if (!window.confirm("Delete everything we remember about your past cases?")) return;
    setActionError(null);
    try {
      const result = await api.request<MemoryDeleted>("/me/memory", { method: "DELETE" });
      setNotice(`Deleted ${result.deleted} item${result.deleted === 1 ? "" : "s"}.`);
      reload();
    } catch (caught) {
      setActionError(describeError(caught));
    }
  }

  return (
    <>
      <PageTitle>My memory</PageTitle>
      <p className="mb-4 text-sm text-slate-600">
        Short outcomes of your past cases, used only as background for new ones. Facts always
        come from your live order records. Each item is deleted automatically when it expires.
      </p>
      <ErrorBanner message={error ?? actionError} />
      {notice !== null && <p className="mb-3 text-sm text-emerald-700">{notice}</p>}
      {loading && <Loading />}
      {data !== null && (
        <Card>
          {data.length === 0 ? (
            <p className="text-sm text-slate-600">Nothing is remembered.</p>
          ) : (
            <table className="w-full text-left text-sm">
              <thead className="text-xs text-slate-500">
                <tr>
                  <th className="py-1">Outcome</th>
                  <th>Category</th>
                  <th>Order</th>
                  <th>Recorded</th>
                  <th>Expires</th>
                </tr>
              </thead>
              <tbody>
                {data.map((item, index) => (
                  <tr key={`${item.created_at}-${index}`} className="border-t border-slate-100">
                    <td className="py-1">{memoryOutcomeLabel(item.outcome_code)}</td>
                    <td>{item.category === null ? "—" : humanize(item.category)}</td>
                    <td>{item.order_number ?? "—"}</td>
                    <td>{formatDateTime(item.created_at)}</td>
                    <td>{formatDateTime(item.expires_at)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
          <button
            type="button"
            className={`${secondaryButtonClass} mt-3`}
            disabled={data.length === 0}
            onClick={() => void deleteAll()}
          >
            Delete all
          </button>
        </Card>
      )}
    </>
  );
}
