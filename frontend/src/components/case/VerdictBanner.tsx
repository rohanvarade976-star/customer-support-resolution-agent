// The top of a refund case: code's verdict in one glance (ADR-022). Green "Ready to approve"
// with a one-line summary, or amber "Needs attention" listing the reasons in plain words, each
// a button that opens and scrolls to the section holding the evidence. The verdict comes from
// the API (domain/readiness.py); the page only displays it.

import { ArrowDown, CircleCheckBig, TriangleAlert } from "lucide-react";

import type { ReadinessView } from "../../api/types";
import { readinessText, type CaseSection } from "../../lib/readiness";

interface VerdictBannerProps {
  readiness: ReadinessView;
  checksPassed: number;
  onShow: (section: CaseSection) => void;
}

export function VerdictBanner({ readiness, checksPassed, onShow }: VerdictBannerProps) {
  if (readiness.status === "ready_to_approve") {
    return (
      <section
        aria-label="Verdict"
        data-testid="verdict"
        className="flex items-start gap-3 rounded-xl border border-success-200 bg-success-50 px-4 py-3.5 sm:px-5"
      >
        <CircleCheckBig className="mt-0.5 size-5 shrink-0 text-success-600" aria-hidden="true" />
        <div className="min-w-0">
          <h2 className="text-base font-semibold text-success-900">Ready to approve</h2>
          <p className="mt-0.5 text-sm text-success-800">
            All {checksPassed} eligibility checks passed and still hold · no risk flags · AI recommends a refund ·
            critic approved · citations verified
          </p>
        </div>
      </section>
    );
  }
  return (
    <section
      aria-label="Verdict"
      data-testid="verdict"
      className="flex items-start gap-3 rounded-xl border border-warning-200 bg-warning-50 px-4 py-3.5 sm:px-5"
    >
      <TriangleAlert className="mt-0.5 size-5 shrink-0 text-warning-600" aria-hidden="true" />
      <div className="min-w-0 flex-1">
        <h2 className="text-base font-semibold text-warning-900">Needs attention</h2>
        <ul className="mt-1.5 space-y-1">
          {readiness.reasons.map((reason) => {
            const { text, section } = readinessText(reason);
            return (
              <li key={reason.code}>
                <button
                  type="button"
                  onClick={() => {
                    onShow(section);
                  }}
                  className="group inline-flex cursor-pointer items-start gap-1.5 rounded text-left text-sm text-warning-900 hover:underline"
                >
                  <ArrowDown className="mt-0.5 size-3.5 shrink-0 text-warning-600" aria-hidden="true" />
                  {text}
                </button>
              </li>
            );
          })}
        </ul>
      </div>
    </section>
  );
}
