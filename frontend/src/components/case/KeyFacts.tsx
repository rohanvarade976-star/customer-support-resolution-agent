// The handful of facts a reviewer needs for a normal case, next to the decision panel
// (ADR-022): the stored amount, the order, the checks passed, eligibility now, risk flags and
// the AI's recommended action. All values come from the API; the full evidence is in the
// collapsible sections below.

import { BadgeCheck, Check, ShieldCheck, ShieldExclamationPoint, Sparkles, X } from "lucide-react";
import type { ReactNode } from "react";

import type { ApprovalCase } from "../../api/types";
import { actionLabel, riskText } from "../../lib/analysis";
import { humanize } from "../../lib/labels";
import { formatMoney } from "../../lib/money";
import { Badge } from "../ui/badge";
import { Card, CardContent } from "../ui/card";

export function KeyFacts({ data }: { data: ApprovalCase }) {
  const { facts, risk_flags: riskFlags, ai_action: aiAction } = data;
  return (
    <Card data-testid="key-facts">
      <CardContent className="space-y-4 p-4 sm:p-5">
        <dl className="grid grid-cols-2 gap-x-4 gap-y-3 sm:grid-cols-4">
          <Fact label="Amount">
            <span className="text-lg font-semibold text-gray-900 tabular-nums">
              {formatMoney(data.proposal.amount_minor, data.proposal.currency)}
            </span>
          </Fact>
          <Fact label="Order">
            <span className="font-medium text-gray-900">{data.proposal.order_number}</span>
          </Fact>
          <Fact label="Eligible now">
            {facts === null ? (
              <Badge tone="danger">Unknown</Badge>
            ) : facts.eligible_now ? (
              <Badge tone="success">
                <BadgeCheck aria-hidden="true" />
                Yes
              </Badge>
            ) : (
              <Badge tone="danger">
                <X aria-hidden="true" />
                No
              </Badge>
            )}
          </Fact>
          <Fact label="Risk">
            {riskFlags.length === 0 ? (
              <Badge tone="success">
                <ShieldCheck aria-hidden="true" />
                No flags
              </Badge>
            ) : (
              <Badge tone="warning" title={riskFlags.map((code) => riskText(code).title).join(" · ")}>
                <ShieldExclamationPoint aria-hidden="true" />
                {riskFlags.length === 1 ? riskText(riskFlags[0] ?? "").title : `${String(riskFlags.length)} flags`}
              </Badge>
            )}
          </Fact>
        </dl>

        <div className="flex flex-col gap-3 border-t border-gray-100 pt-4 sm:flex-row sm:items-start sm:justify-between">
          <div className="min-w-0">
            <p className="text-xs font-medium text-gray-500">Checks passed</p>
            {data.proposal.checks_passed.length === 0 ? (
              <p className="mt-1 text-sm text-gray-500">None recorded.</p>
            ) : (
              <ul className="mt-1.5 flex flex-wrap gap-1.5">
                {data.proposal.checks_passed.map((check) => (
                  <li key={check}>
                    <Badge tone="success">
                      <Check aria-hidden="true" />
                      {humanize(check)}
                    </Badge>
                  </li>
                ))}
              </ul>
            )}
          </div>
          <div className="shrink-0">
            <p className="text-xs font-medium text-gray-500">AI recommends</p>
            <p className="mt-1.5">
              {data.proposed_by_reviewer ? (
                <Badge tone="neutral">Proposed by a reviewer</Badge>
              ) : (
                <Badge tone="ai" title="The AI's recommendation. Not verified; the amount comes from the refund rules.">
                  <Sparkles aria-hidden="true" />
                  {actionLabel(aiAction)}
                </Badge>
              )}
            </p>
          </div>
        </div>
      </CardContent>
    </Card>
  );
}

function Fact({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="min-w-0">
      <dt className="text-xs font-medium text-gray-500">{label}</dt>
      <dd className="mt-1">{children}</dd>
    </div>
  );
}
