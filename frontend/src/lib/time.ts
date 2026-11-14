// Times for people: "5 minutes ago", "expires in 12 min". Display only.

const MINUTE = 60_000;
const HOUR = 60 * MINUTE;
const DAY = 24 * HOUR;

const relative = new Intl.RelativeTimeFormat("en-IN", { numeric: "auto" });

// "just now", "5 minutes ago", "yesterday", "3 days ago"; after a week, the date.
export function formatRelative(iso: string, now: number = Date.now()): string {
  const time = new Date(iso).getTime();
  if (Number.isNaN(time)) return iso;
  const diff = time - now; // negative: in the past
  const abs = Math.abs(diff);
  if (abs < MINUTE) return "just now";
  if (abs < HOUR) return relative.format(Math.round(diff / MINUTE), "minute");
  if (abs < DAY) return relative.format(Math.round(diff / HOUR), "hour");
  if (abs < 7 * DAY) return relative.format(Math.round(diff / DAY), "day");
  return new Date(time).toLocaleDateString("en-IN", { dateStyle: "medium" });
}

export type Urgency = "expired" | "urgent" | "soon" | "normal";

export interface TimeLeft {
  text: string; // "Expires in 12 min", "Expired"
  short: string; // "12 min", "Expired"
  urgency: Urgency;
  ms: number;
}

// How long until an approval expires, and how urgent that is (under 10 min urgent, under 30 soon).
export function timeUntil(iso: string, now: number = Date.now()): TimeLeft {
  const ms = new Date(iso).getTime() - now;
  if (Number.isNaN(ms)) return { text: iso, short: iso, urgency: "normal", ms: 0 };
  if (ms <= 0) return { text: "Expired", short: "Expired", urgency: "expired", ms };
  const minutes = Math.ceil(ms / MINUTE);
  let short: string;
  if (minutes < 60) short = `${minutes} min`;
  else if (minutes < 24 * 60) short = `${Math.floor(minutes / 60)} h ${minutes % 60} min`;
  else short = `${Math.floor(minutes / (24 * 60))} days`;
  const urgency: Urgency = minutes <= 10 ? "urgent" : minutes <= 30 ? "soon" : "normal";
  return { text: `Expires in ${short}`, short, urgency, ms };
}
