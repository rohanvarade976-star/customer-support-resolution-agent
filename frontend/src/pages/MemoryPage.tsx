// What the system remembers about the customer's past cases (GET /me/memory, ADR-013), and
// a button to delete all of it (DELETE /me/memory) after a confirmation. Codes only: there is
// no free text in memory.

import { Brain, Clock, Database, FileText, Trash } from "lucide-react";
import { useState } from "react";

import { describeError } from "../api/client";
import type { MemoryDeleted, MemoryItem } from "../api/types";
import { useAuth } from "../auth/auth";
import { ConfirmDialog } from "../components/ConfirmDialog";
import { PageHeader } from "../components/PageHeader";
import { EmptyState, ErrorState, ListSkeleton } from "../components/states";
import { Alert } from "../components/ui/alert";
import { Button } from "../components/ui/button";
import { Card, CardDescription, CardHeader, CardTitle } from "../components/ui/card";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "../components/ui/table";
import { useToast } from "../components/ui/toast-context";
import { useApiGet } from "../hooks/useApiGet";
import { formatDateTime, humanize, memoryOutcomeLabel } from "../lib/labels";

const PRINCIPLES = [
  { icon: FileText, title: "Outcomes, not messages", text: "We keep a short code for how a case ended, never what you wrote." },
  { icon: Database, title: "Facts stay live", text: "Order facts always come from your live records, not from memory." },
  { icon: Clock, title: "Deleted automatically", text: "Each item expires on its own date. You can delete everything now." },
];

export function MemoryPage() {
  const { api } = useAuth();
  const toast = useToast();
  const { data, error, loading, reload } = useApiGet<MemoryItem[]>("/me/memory");
  const [confirming, setConfirming] = useState(false);
  const [busy, setBusy] = useState(false);
  const [actionError, setActionError] = useState<string | null>(null);

  async function deleteAll() {
    setBusy(true);
    setActionError(null);
    try {
      const result = await api.request<MemoryDeleted>("/me/memory", { method: "DELETE" });
      toast.success("Memory deleted", `${result.deleted} item${result.deleted === 1 ? "" : "s"} removed.`);
      reload();
    } catch (caught) {
      setActionError(describeError(caught));
    } finally {
      setBusy(false);
      setConfirming(false);
    }
  }

  const count = data?.length ?? 0;

  return (
    <>
      <PageHeader
        title="Privacy and memory"
        description="What we remember from your past cases, and how to delete it."
        actions={
          <Button
            variant="danger-outline"
            disabled={count === 0}
            onClick={() => {
              setConfirming(true);
            }}
          >
            <Trash aria-hidden="true" />
            Delete all
          </Button>
        }
      />

      <ul className="mb-6 grid gap-4 sm:grid-cols-3">
        {PRINCIPLES.map(({ icon: Icon, title, text }) => (
          <li key={title} className="flex gap-3 rounded-xl border border-gray-200 bg-white p-4 shadow-sm">
            <span className="grid size-9 shrink-0 place-items-center rounded-lg bg-primary-50 text-primary-600">
              <Icon className="size-4.5" aria-hidden="true" />
            </span>
            <div>
              <p className="text-sm font-medium text-gray-900">{title}</p>
              <p className="mt-0.5 text-xs leading-relaxed text-gray-600">{text}</p>
            </div>
          </li>
        ))}
      </ul>

      {actionError !== null && (
        <div className="mb-4">
          <Alert tone="danger">{actionError}</Alert>
        </div>
      )}

      {error !== null ? (
        <ErrorState message={error} onRetry={reload} />
      ) : loading || data === null ? (
        <ListSkeleton rows={3} />
      ) : (
        <Card>
          <CardHeader className="pb-4">
            <div>
              <CardTitle>Remembered outcomes</CardTitle>
              <CardDescription>
                {count === 0 ? "Nothing is stored." : `${count} item${count === 1 ? "" : "s"}, used only as background for new cases.`}
              </CardDescription>
            </div>
          </CardHeader>
          {count === 0 ? (
            <EmptyState
              icon={Brain}
              title="Nothing is remembered"
              description="When a case ends, a short outcome may be kept here to help with your next one."
              className="border-t border-gray-100"
            />
          ) : (
            <>
              {/* Phones: a list. Wider screens: a table. */}
              <ul className="divide-y divide-gray-100 border-t border-gray-100 md:hidden">
                {data.map((item, index) => (
                  <li key={`${item.created_at}-${index}`} className="px-5 py-3.5">
                    <p className="text-sm font-medium text-gray-900">{memoryOutcomeLabel(item.outcome_code)}</p>
                    <p className="mt-0.5 text-xs text-gray-600">
                      {item.category === null ? "No category" : humanize(item.category)}
                      {item.order_number !== null && ` · ${item.order_number}`}
                    </p>
                    <p className="mt-0.5 text-xs text-gray-500">Expires {formatDateTime(item.expires_at)}</p>
                  </li>
                ))}
              </ul>
              <div className="hidden md:block">
                <Table>
                  <TableHeader>
                    <TableRow>
                      <TableHead>Outcome</TableHead>
                      <TableHead>Category</TableHead>
                      <TableHead>Order</TableHead>
                      <TableHead>Recorded</TableHead>
                      <TableHead>Expires</TableHead>
                    </TableRow>
                  </TableHeader>
                  <TableBody>
                    {data.map((item, index) => (
                      <TableRow key={`${item.created_at}-${index}`}>
                        <TableCell className="font-medium text-gray-900">{memoryOutcomeLabel(item.outcome_code)}</TableCell>
                        <TableCell className="text-gray-700">{item.category === null ? "—" : humanize(item.category)}</TableCell>
                        <TableCell className="text-gray-700">{item.order_number ?? "—"}</TableCell>
                        <TableCell className="text-gray-600">{formatDateTime(item.created_at)}</TableCell>
                        <TableCell className="text-gray-600">{formatDateTime(item.expires_at)}</TableCell>
                      </TableRow>
                    ))}
                  </TableBody>
                </Table>
              </div>
            </>
          )}
        </Card>
      )}

      <ConfirmDialog
        open={confirming}
        title="Delete everything we remember?"
        description={`This permanently removes ${count} remembered outcome${count === 1 ? "" : "s"}. Your orders and tickets are not affected.`}
        confirmLabel="Delete all"
        tone="danger"
        busy={busy}
        onConfirm={() => void deleteAll()}
        onCancel={() => {
          setConfirming(false);
        }}
      />
    </>
  );
}
