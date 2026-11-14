import { clsx, type ClassValue } from "clsx";
import { twMerge } from "tailwind-merge";

// Joins class names, and lets a later Tailwind class win over an earlier one
// (cn("px-4", "px-2") gives "px-2"), so components can accept a className override.
export function cn(...inputs: ClassValue[]): string {
  return twMerge(clsx(inputs));
}
