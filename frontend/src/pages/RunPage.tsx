// A run's live page (customer owner or reviewer): status, the verified reply, a timeline of
// steps, and a reply box while the run is waiting for the customer.

import { useState, type SyntheticEvent } from "react";
import { useParams } from "react-router-dom";

import { describeError } from "../api/client";
import type { Run, RunStepEvent } from "../api/types";
import { isReviewer, useAuth } from "../auth/auth";
import { buttonClass, Card, ErrorBanner, inputClass, Loading, PageTitle, StatusBadge } from "../components/ui";
import { useRunStream, type RunStreamOptions, type StreamMode } from "../hooks/useRunStream";
import { eventLabel, formatDateTime, runStatusLabel } from "../lib/labels";

const MAX_REPLY_LENGTH = 2000; // the API's limit (schemas/messages.py)

const MODE_TEXT: Record<StreamMode, string> = {
  connecting: "Connecting…",
  live: "Live",
  polling: "Reconnecting (checking every few seconds)",
  done: "Up to date",
};

export function RunPage({ streamOptions }: { streamOptions?: RunStreamOptions }) {
  const { runId = "" } = useParams();
  const { api, user } = useAuth();
  const reviewer = isReviewer(user);
  const live = useRunStream(api, runId, streamOptions);

  return (
    <>
      <PageTitle>Your request</PageTitle>
      <ErrorBanner message={live.error} />
      {live.status === null && live.error === null && <Loading />}
      {live.status !== null && (
        <>
          <Card>
            <div className="flex flex-wrap items-center gap-3">
              <StatusBadge status={live.status.status} label={runStatusLabel(live.status.status)} />
              <span className="ml-auto text-xs text-slate-500" aria-live="polite">
                {MODE_TEXT[live.mode]}
              </span>
            </div>
            {live.status.reply !== null && (
              <div className="mt-3">
                <h2 className="text-sm font-medium text-slate-600">Latest update</h2>
                {/* Verified by the backend before it was saved; shown as plain text. */}
                <p className="mt-1 whitespace-pre-line" data-testid="verified-reply">
                  {live.status.reply}
                </p>
              </div>
            )}
          </Card>
          {live.status.status === "awaiting_customer" && !reviewer && (
            <ReplyBox runId={runId} onSent={live.refresh} />
          )}
          <Card title="Progress">
            <Timeline steps={live.steps} reviewer={reviewer} />
          </Card>
        </>
      )}
    </>
  );
}

function ReplyBox({ runId, onSent }: { runId: string; onSent: () => Promise<void> }) {
  const { api } = useAuth();
  const [text, setText] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function submit(event: SyntheticEvent) {
    event.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await api.request<Run>(`/runs/${runId}/messages`, { method: "POST", body: { text } });
      setText("");
      await onSent();
    } catch (caught) {
      setError(describeError(caught));
    } finally {
      setBusy(false);
    }
  }

  return (
    <Card title="Reply">
      <form onSubmit={(event) => void submit(event)} className="space-y-2">
        <label htmlFor="reply" className="sr-only">
          Your reply
        </label>
        <textarea
          id="reply"
          rows={3}
          maxLength={MAX_REPLY_LENGTH}
          className={inputClass}
          value={text}
          onChange={(event) => {
            setText(event.target.value);
          }}
        />
        <ErrorBanner message={error} />
        <button type="submit" className={buttonClass} disabled={busy || text.trim() === ""}>
          {busy ? "Sending…" : "Send reply"}
        </button>
      </form>
    </Card>
  );
}

function Timeline({ steps, reviewer }: { steps: RunStepEvent[]; reviewer: boolean }) {
  if (steps.length === 0) return <p className="text-sm text-slate-500">Nothing yet.</p>;
  return (
    <ol className="space-y-2">
      {steps.map((step) => (
        <li key={step.id} className="text-sm">
          <span className="mr-2 text-xs text-slate-500">{formatDateTime(step.at)}</span>
          {reviewer ? <code className="text-slate-800">{step.type}</code> : eventLabel(step.type)}
          {reviewer && Object.keys(step.details).length > 0 && (
            <dl className="ml-4 mt-1 grid grid-cols-[auto_1fr] gap-x-3 text-xs text-slate-600">
              {Object.entries(step.details).map(([key, value]) => (
                <div key={key} className="contents">
                  <dt className="font-mono">{key}</dt>
                  <dd className="break-all font-mono">{Array.isArray(value) ? value.join(", ") : String(value)}</dd>
                </div>
              ))}
            </dl>
          )}
        </li>
      ))}
    </ol>
  );
}
