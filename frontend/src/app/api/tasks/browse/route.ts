import { NextRequest } from "next/server";
import { proxyBackendJson } from "@/lib/backend-response";
import { parseTaskSearch } from "@/lib/tag-query";
import {
  BROWSE_FORWARD_KEYS,
  PRESET_MS,
  TASKS_PAGE_SIZE,
} from "@/lib/tasks-filters";

// Same-origin proxy for the task grid's client-side browse fetch (see
// lib/use-task-browse.ts — one fetch per filter state, cached between
// visits). The request arrives in display form — the page URL's filter
// params, rolling *_within presets still as tokens — and is resolved here,
// per request, into the backend query. The resolution ran in the
// server-rendered grid before (recent-tasks-results.tsx) and keeps its
// exact meaning; only the route it runs in changed.
export async function GET(request: NextRequest) {
  const display = request.nextUrl.searchParams;
  const query = new URLSearchParams();
  query.set("limit", String(TASKS_PAGE_SIZE));
  query.set(
    "offset",
    String(Math.max(Number(display.get("offset") ?? "0") || 0, 0))
  );

  // Tags are a structured filter (tags/tags_any/tags_none params), so only
  // free text + author are taken from the search box. `query` is the legacy
  // search param (e.g. worker-job deep links); `q` is the current one.
  const parsed = parseTaskSearch(
    display.get("q") ?? display.get("query") ?? ""
  );
  if (parsed.text) query.set("query", parsed.text);
  if (parsed.authors.length) query.set("author", parsed.authors.join(","));

  // Rolling "Created" preset: resolve the token to (now - window) at request
  // time — including every background revalidation — so the window is always
  // relative to this request, not when it was picked. Resolved here —
  // `created_within` is not a backend param.
  const within = display.get("created_within");
  const presetActive = !!(within && within in PRESET_MS);
  if (presetActive) {
    const ms = PRESET_MS[within as keyof typeof PRESET_MS];
    query.set("created_after", new Date(Date.now() - ms).toISOString());
  }
  const trialFinishedWithin = display.get("trial_finished_within");
  const trialFinishedPresetActive = Boolean(
    trialFinishedWithin && trialFinishedWithin in PRESET_MS
  );
  if (trialFinishedPresetActive) {
    const ms = PRESET_MS[trialFinishedWithin as keyof typeof PRESET_MS];
    query.set("trial_finished_after", new Date(Date.now() - ms).toISOString());
  }

  for (const key of BROWSE_FORWARD_KEYS) {
    if (key === "created_within" || key === "trial_finished_within") continue;
    // A live preset owns created_after; don't let a stale absolute bound in
    // the URL / saved filter clobber the rolling window.
    if (key === "created_after" && presetActive) continue;
    if (key === "trial_finished_after" && trialFinishedPresetActive) continue;
    const value = display.get(key);
    if (value) query.set(key, value);
  }

  // `tag:` tokens typed in the search box are parsed out of `q` above, so they
  // must be forwarded too (unioned with the structured Tags-filter params).
  // Without this they were stripped from the free text AND dropped, so shared
  // URLs / saved searches with tag tokens filtered nothing. Mirrors the
  // dashboard page, which forwards the same parsed tag buckets.
  const mergeTags = (param: string, extra: string[]) => {
    if (!extra.length) return;
    const existing = query.get(param)?.split(",") ?? [];
    query.set(param, Array.from(new Set([...existing, ...extra])).join(","));
  };
  mergeTags("tags", parsed.all);
  mergeTags("tags_any", parsed.any);
  mergeTags("tags_none", parsed.none);

  // The request signal rides along so a client abort (superseded filter
  // state, timeout) cancels the backend query instead of letting it run
  // for a result nobody will render.
  return proxyBackendJson({
    path: `tasks/browse?${query.toString()}`,
    signal: request.signal,
  });
}
