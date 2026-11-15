// How many escalated tickets wait for a person: the sidebar's badge (ADR-022). Reviewers only.
// Re-read when the reviewer moves to another page (so it drops right after they resolve one)
// and every minute. A failed read hides the badge rather than showing a wrong number.

import { useEffect, useState } from "react";

import type { EscalationList } from "../api/types";
import { useAuth } from "../auth/auth";

const REFRESH_MS = 60_000;

export function useEscalationCount(reviewer: boolean, pathname: string): number | null {
  const { api } = useAuth();
  const [count, setCount] = useState<number | null>(null);

  useEffect(() => {
    if (!reviewer) return;
    const controller = new AbortController();
    const load = () => {
      api.request<EscalationList>("/reviewer/escalations", { signal: controller.signal }).then(
        (list) => {
          setCount(list.items.length);
        },
        () => {
          if (!controller.signal.aborted) setCount(null);
        },
      );
    };
    load();
    const timer = setInterval(load, REFRESH_MS);
    return () => {
      controller.abort();
      clearInterval(timer);
    };
  }, [api, reviewer, pathname]);

  return reviewer ? count : null;
}
