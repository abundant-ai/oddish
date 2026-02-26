import { type ClassValue, clsx } from "clsx";
import { twMerge } from "tailwind-merge";

export function cn(...inputs: ClassValue[]) {
  return twMerge(clsx(inputs));
}

/**
 * Returns the task's display name.
 */
export function taskDisplayName(task: { name: string }) {
  return task.name;
}

/**
 * Returns a short ID suffix for display (the UUID portion of the task ID).
 * e.g. if id is "axios-12345678", this returns "12345678".
 */
export function taskIdSuffix(task: { id: string; name: string }) {
  // If the ID starts with the name, extract the suffix
  if (task.id.startsWith(task.name)) {
    let suffix = task.id.slice(task.name.length);
    if (suffix.startsWith("-")) suffix = suffix.slice(1);
    if (suffix) return suffix;
  }
  return task.id;
}

export function formatShortDateTime(iso: string) {
  const d = new Date(iso);
  // e.g. "01/15 14:03"
  return d.toLocaleString(undefined, {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  });
}

export function encodeExperimentRouteParam(experimentId: string) {
  return encodeURIComponent(encodeURIComponent(experimentId));
}

export function decodeExperimentRouteParam(value: string) {
  try {
    return decodeURIComponent(value);
  } catch {
    return value;
  }
}
