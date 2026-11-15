// True when the person has asked their device for less motion. Animations that run in script
// (the counters) check this; CSS animations are switched off by the media query in index.css.
// A browser that cannot answer counts as "reduced": nothing moves unless we know it may.

import { useEffect, useState } from "react";

const QUERY = "(prefers-reduced-motion: reduce)";

function currentPreference(): boolean {
  if (typeof window.matchMedia !== "function") return true;
  return window.matchMedia(QUERY).matches;
}

export function usePrefersReducedMotion(): boolean {
  const [reduced, setReduced] = useState(currentPreference);
  useEffect(() => {
    if (typeof window.matchMedia !== "function") return;
    const list = window.matchMedia(QUERY);
    const onChange = () => {
      setReduced(list.matches);
    };
    list.addEventListener("change", onChange);
    return () => {
      list.removeEventListener("change", onChange);
    };
  }, []);
  return reduced;
}
