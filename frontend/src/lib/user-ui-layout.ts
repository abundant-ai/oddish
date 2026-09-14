export type TrialDrawerLayout = {
  version: 1;
  preferredWidthPx: number | null;
  maximized: boolean;
  taskPanePercent: number;
  showTask: boolean;
  showTrial: boolean;
};

export const DEFAULT_TRIAL_DRAWER_LAYOUT: TrialDrawerLayout = {
  version: 1,
  preferredWidthPx: null,
  maximized: false,
  taskPanePercent: 42,
  showTask: true,
  showTrial: true,
};

export function parseTrialDrawerLayout(value: unknown): TrialDrawerLayout {
  if (!value || typeof value !== "object") throw new Error("Invalid layout");
  const layout = value as Record<string, unknown>;
  if (
    layout.version !== 1 ||
    (layout.preferredWidthPx !== null &&
      (typeof layout.preferredWidthPx !== "number" ||
        !Number.isFinite(layout.preferredWidthPx) ||
        layout.preferredWidthPx < 420 ||
        layout.preferredWidthPx > 16384)) ||
    typeof layout.taskPanePercent !== "number" ||
    !Number.isFinite(layout.taskPanePercent) ||
    typeof layout.maximized !== "boolean" ||
    typeof layout.showTask !== "boolean" ||
    typeof layout.showTrial !== "boolean" ||
    (!layout.showTask && !layout.showTrial)
  )
    throw new Error("Invalid layout");
  return {
    version: 1,
    preferredWidthPx: layout.preferredWidthPx,
    maximized: layout.maximized,
    taskPanePercent: Math.max(15, Math.min(85, layout.taskPanePercent)),
    showTask: layout.showTask,
    showTrial: layout.showTrial,
  };
}

type Snapshot = {
  layout: TrialDrawerLayout;
  status: "loading" | "ready" | "saving" | "error";
};

const PATH = "/api/users/me/ui-layouts/experiment.trial-drawer";

/** Owns one mounted account's load and ordered, coalesced saves. */
export class UserUiLayoutStore {
  private request: typeof fetch | null;
  private snapshot: Snapshot;
  private listeners = new Set<() => void>();
  private active = false;
  private loaded = false;
  private edits: Partial<TrialDrawerLayout> = {};
  private revision = 0;
  private savedRevision = 0;
  private saving = false;
  private timer: ReturnType<typeof setTimeout> | undefined;
  private readAbort: AbortController | undefined;

  constructor(request: typeof fetch | null) {
    this.request = request;
    this.snapshot = {
      layout: DEFAULT_TRIAL_DRAWER_LAYOUT,
      status: request ? "loading" : "ready",
    };
  }

  getSnapshot = () => this.snapshot;
  subscribe = (listener: () => void) => {
    this.listeners.add(listener);
    return () => {
      this.listeners.delete(listener);
    };
  };

  private publish(layout: TrialDrawerLayout, status: Snapshot["status"]) {
    this.snapshot = { layout, status };
    this.listeners.forEach((listener) => listener());
  }

  start = () => {
    this.active = true;
    if (this.request && !this.loaded) void this.load();
    return () => {
      this.active = false;
      clearTimeout(this.timer);
      this.readAbort?.abort();
    };
  };

  private async load() {
    const controller = new AbortController();
    this.readAbort = controller;
    this.publish(this.snapshot.layout, "loading");
    try {
      const response = await this.request!(PATH, {
        signal: controller.signal,
        cache: "no-store",
      });
      if (!response.ok) throw new Error("Could not load layout");
      const layout = parseTrialDrawerLayout(await response.json());
      if (!this.active || controller.signal.aborted) return;
      this.loaded = true;
      // Resizing before the GET finishes wins over the older server value.
      const restored = { ...layout, ...this.edits };
      if (!restored.showTask && !restored.showTrial) {
        if (this.edits.showTask === false) restored.showTrial = true;
        else restored.showTask = true;
      }
      this.publish(restored, "ready");
      void this.flush();
    } catch {
      if (this.active && !controller.signal.aborted)
        this.publish(this.snapshot.layout, "error");
    }
  }

  update = (patch: Partial<TrialDrawerLayout>) => {
    if (
      !Object.entries(patch).some(
        ([key, value]) =>
          this.snapshot.layout[key as keyof TrialDrawerLayout] !== value
      )
    )
      return;
    const layout = { ...this.snapshot.layout, ...patch };
    if (!layout.showTask && !layout.showTrial) return;
    this.edits = { ...this.edits, ...patch };
    this.revision++;
    this.publish(
      layout,
      this.request && this.loaded ? "saving" : this.snapshot.status
    );
    clearTimeout(this.timer);
    this.timer = setTimeout(() => void this.flush(), 500);
  };

  flush = async () => {
    clearTimeout(this.timer);
    if (
      !this.active ||
      !this.request ||
      !this.loaded ||
      this.saving ||
      this.revision === this.savedRevision
    )
      return;
    this.saving = true;
    const revision = this.revision;
    this.publish(this.snapshot.layout, "saving");
    try {
      const response = await this.request(PATH, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(this.snapshot.layout),
        keepalive: true,
      });
      if (!response.ok) throw new Error("Could not save layout");
      this.savedRevision = revision;
      if (this.active) this.publish(this.snapshot.layout, "ready");
    } catch {
      if (this.active) this.publish(this.snapshot.layout, "error");
      return;
    } finally {
      this.saving = false;
    }
    // A slow earlier save must finish before the newer layout is sent.
    if (this.active && this.revision !== revision) void this.flush();
  };

  retry = () => {
    if (!this.loaded) void this.load();
    else void this.flush();
  };
}
