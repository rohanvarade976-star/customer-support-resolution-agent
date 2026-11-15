// Short dates and times for the run page: "3 Nov", "14:32:07". Display only.

// A timestamp as a day: "3 Nov" (in the viewer's time zone).
export function formatDay(iso: string): string {
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return iso;
  return date.toLocaleDateString("en-IN", { day: "numeric", month: "short" });
}

// A date with no time ("2026-11-12") as a day. Read as UTC, so the time zone cannot shift it.
export function formatDateOnly(date: string): string {
  const parsed = new Date(`${date}T00:00:00Z`);
  if (Number.isNaN(parsed.getTime())) return date;
  return parsed.toLocaleDateString("en-IN", { day: "numeric", month: "short", timeZone: "UTC" });
}

// The exact time of day, with seconds, for the activity list.
export function formatClock(iso: string): string {
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return iso;
  return date.toLocaleTimeString("en-IN", { hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false });
}
