// Loads one GET endpoint for a page: data, error, loading, and reload().
//
// The fetch runs in an effect and sets state only when it answers, so a page that unmounts
// (or reloads) mid-request never sees a stale answer: the old request is aborted.

import { useCallback, useEffect, useState } from "react";

import { describeError } from "../api/client";
import { useAuth } from "../auth/auth";

export interface ApiGet<T> {
  data: T | null;
  error: string | null;
  loading: boolean; // true until the first answer (or error)
  reload: () => void;
}

export function useApiGet<T>(path: string): ApiGet<T> {
  const { api } = useAuth();
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [version, setVersion] = useState(0); // bumped by reload() to fetch again

  useEffect(() => {
    const controller = new AbortController();
    api.request<T>(path, { signal: controller.signal }).then(
      (result) => {
        setData(result);
        setError(null);
      },
      (caught: unknown) => {
        if (!controller.signal.aborted) setError(describeError(caught));
      },
    );
    return () => {
      controller.abort();
    };
  }, [api, path, version]);

  const reload = useCallback(() => {
    setVersion((v) => v + 1);
  }, []);
  return { data, error, loading: data === null && error === null, reload };
}
