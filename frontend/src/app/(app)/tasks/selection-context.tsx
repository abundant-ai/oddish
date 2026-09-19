"use client";

import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
  type ReactNode,
} from "react";
import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
import { useAuth } from "@clerk/nextjs";
import useSWR from "swr";
import { ChevronDown, PackagePlus } from "lucide-react";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { fetcher } from "@/lib/api";
import { formatCostUsd } from "@/lib/format";
import { isOrgAdminRole } from "@/lib/org-roles";
import {
  parseStoredSelection,
  SELECTION_LIMIT,
  selectionStorageKey,
  selectionTotals,
  serializeSelection,
  type Selection,
} from "@/lib/tasks-selection";
import type {
  Customer,
  DeliveryListItem,
  TaskBrowseIdsResponse,
  TaskBrowseItem,
} from "@/lib/types";
import {
  browseIdsKey,
  useTaskBrowse,
  useTaskBrowseCount,
} from "@/lib/use-task-browse";
import { cn } from "@/lib/utils";

type SelectionContextValue = {
  selection: Selection;
  isSelected: (id: string) => boolean;
  toggle: (task: TaskBrowseItem) => void;
  addTasks: (tasks: TaskBrowseItem[]) => void;
  addIds: (ids: string[]) => void;
  clear: () => void;
  removeIds: (ids: string[]) => void;
  deliveryId: string | null;
  delivery: DeliveryListItem | undefined;
  deliveryError: string | null;
};

const SelectionContext = createContext<SelectionContextValue | null>(null);

export function useSelection(): SelectionContextValue {
  const ctx = useContext(SelectionContext);
  if (!ctx) {
    throw new Error("useSelection must be used within a SelectionProvider");
  }
  return ctx;
}

function entryFor(task: TaskBrowseItem) {
  return {
    name: task.name,
    cost: task.cost_usd,
    estimated: task.cost_has_estimated && !task.cost_has_native,
  };
}

// The selection outlives the page: it is restored from localStorage (one
// entry per org) after mount and written back on every change, so paging,
// changing filters, reloading, or closing the tab never loses the picks.
// Hydration happens in an effect, not during render, so the server and the
// first client render agree (an empty selection).
export function SelectionProvider({ children }: { children: ReactNode }) {
  const { orgId, isLoaded } = useAuth();
  const deliveryId = useSearchParams().get("delivery");
  const storageKey =
    selectionStorageKey(orgId) + (deliveryId ? `.delivery.${deliveryId}` : "");
  return (
    <SelectionStateProvider
      key={storageKey}
      storageKey={storageKey}
      isLoaded={isLoaded}
      deliveryId={deliveryId}
    >
      {children}
    </SelectionStateProvider>
  );
}

function SelectionStateProvider({
  children,
  storageKey,
  isLoaded,
  deliveryId,
}: {
  children: ReactNode;
  storageKey: string;
  isLoaded: boolean;
  deliveryId: string | null;
}) {
  const { data: deliveries, error } = useSWR<DeliveryListItem[]>(
    deliveryId ? "/api/deliveries" : null,
    fetcher
  );
  const delivery = deliveries?.find((item) => item.id === deliveryId);
  const deliveryError = error
    ? "Could not load the delivery."
    : deliveries && !delivery
      ? "Delivery not found."
      : delivery && delivery.status !== "active"
        ? "This delivery is finalized."
        : null;
  const [selection, setSelection] = useState<Selection>(() => new Map());
  // React commits this marker with the restored selection. A ref would let
  // the persistence effect see "loaded" while still holding the empty render.
  const [hydratedKey, setHydratedKey] = useState<string | null>(null);

  useEffect(() => {
    if (!isLoaded) return;
    try {
      setSelection(
        parseStoredSelection(window.localStorage.getItem(storageKey))
      );
    } catch {
      setSelection(new Map());
    }
    setHydratedKey(storageKey);
  }, [isLoaded, storageKey]);

  useEffect(() => {
    if (!isLoaded || hydratedKey !== storageKey) return;
    try {
      if (selection.size === 0) window.localStorage.removeItem(storageKey);
      else
        window.localStorage.setItem(storageKey, serializeSelection(selection));
    } catch {
      // Private mode or a full store: the in-memory selection still works.
    }
  }, [selection, storageKey, hydratedKey, isLoaded]);

  const toggle = useCallback((task: TaskBrowseItem) => {
    setSelection((prev) => {
      const next = new Map(prev);
      if (next.has(task.id)) next.delete(task.id);
      else if (next.size < SELECTION_LIMIT) next.set(task.id, entryFor(task));
      return next;
    });
  }, []);
  const addTasks = useCallback((tasks: TaskBrowseItem[]) => {
    setSelection((prev) => {
      const next = new Map(prev);
      for (const task of tasks) {
        if (next.size >= SELECTION_LIMIT) break;
        if (!next.has(task.id)) next.set(task.id, entryFor(task));
      }
      return next;
    });
  }, []);
  const addIds = useCallback((ids: string[]) => {
    setSelection((prev) => {
      const next = new Map(prev);
      for (const id of ids) {
        if (next.size >= SELECTION_LIMIT) break;
        if (!next.has(id)) next.set(id, { cost: null, estimated: false });
      }
      return next;
    });
  }, []);
  const clear = useCallback(() => setSelection(new Map()), []);
  const removeIds = useCallback(
    (ids: string[]) =>
      setSelection((prev) => {
        const next = new Map(prev);
        for (const id of ids) next.delete(id);
        return next;
      }),
    []
  );

  const value = useMemo<SelectionContextValue>(
    () => ({
      selection,
      isSelected: (id) => selection.has(id),
      toggle,
      addTasks,
      addIds,
      clear,
      removeIds,
      deliveryId,
      delivery,
      deliveryError,
    }),
    [
      selection,
      toggle,
      addTasks,
      addIds,
      clear,
      removeIds,
      deliveryId,
      delivery,
      deliveryError,
    ]
  );

  return (
    <SelectionContext.Provider value={value}>
      {children}
    </SelectionContext.Provider>
  );
}

export function TasksSelectionControls() {
  const { selection, addTasks, addIds, removeIds } = useSelection();
  const sp = new URLSearchParams(useSearchParams().toString());
  const { data: page, isLoading, error } = useTaskBrowse(sp);
  const { total, isStale } = useTaskBrowseCount(sp);
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);
  const items = page?.items ?? [];
  const all = items.length > 0 && items.every((task) => selection.has(task.id));
  const selectAll = async () => {
    setBusy(true);
    setNotice(null);
    try {
      const result = await fetcher<TaskBrowseIdsResponse>(browseIdsKey(sp));
      addIds(result.ids);
      if (
        result.truncated ||
        selection.size + result.ids.filter((id) => !selection.has(id)).length >
          SELECTION_LIMIT
      )
        setNotice(
          `Selection is limited to ${SELECTION_LIMIT.toLocaleString()} tasks. Narrow the filters to select a different set.`
        );
    } catch (err) {
      setNotice(err instanceof Error ? err.message : "Could not select tasks.");
    } finally {
      setBusy(false);
    }
  };
  if (!items.length) return null;
  return (
    <div className="space-y-2">
      <div className="flex flex-wrap items-center gap-3 text-sm">
        <label className="flex items-center gap-2">
          <input
            type="checkbox"
            aria-label="Select this page"
            checked={all}
            disabled={busy || isLoading || !!error}
            onChange={() =>
              all ? removeIds(items.map((t) => t.id)) : addTasks(items)
            }
          />
          Select page
        </label>
        <Button
          variant="ghost"
          size="sm"
          disabled={busy || isStale || !total}
          onClick={() => void selectAll()}
        >
          Select{" "}
          {total !== null && total > SELECTION_LIMIT
            ? `first ${SELECTION_LIMIT.toLocaleString()} matching tasks`
            : `all ${total?.toLocaleString() ?? ""} matching tasks`}
        </Button>
      </div>
      {notice ? (
        <p role="status" className="text-muted-foreground text-sm">
          {notice}
        </p>
      ) : null}
    </div>
  );
}

type Notice = { tone: "info" | "error"; text: ReactNode };

// Pinned above the results: select the page or the whole filter set, see
// what is ticked, and hand the picks to a delivery in one action. Only
// admins can create or fill deliveries (the API requires it), so everyone
// else gets the selection without the button.
export function SelectionBar() {
  const { selection, removeIds, clear, deliveryId, delivery, deliveryError } =
    useSelection();
  const { orgRole } = useAuth();
  const router = useRouter();
  const searchParams = useSearchParams();
  const sp = new URLSearchParams(searchParams.toString());
  // Same SWR keys as the grid and the header count: cached, never a second
  // fetch for the same filter state.
  const totals = selectionTotals(selection);
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState<Notice | null>(null);
  const [newOpen, setNewOpen] = useState(false);
  const [menuOpen, setMenuOpen] = useState(false);
  const [reviewOpen, setReviewOpen] = useState(false);
  const { data: deliveries } = useSWR<DeliveryListItem[]>(
    menuOpen ? "/api/deliveries" : null,
    fetcher
  );
  const active = (deliveries ?? []).filter((d) => d.status === "active");

  const addToExisting = async (delivery: DeliveryListItem) => {
    setBusy(true);
    setNotice(null);
    const ids = Array.from(selection.keys());
    try {
      const { added } = await fetcher<{ added: number }>(
        `/api/deliveries/${encodeURIComponent(delivery.id)}/tasks`,
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ task_ids: ids }),
        }
      );
      removeIds(ids);
      if (deliveryId) {
        router.push(`/deliveries/${encodeURIComponent(delivery.id)}`);
        return;
      }
      setNotice({
        tone: "info",
        text: (
          <>
            Added {added} of {totals.count} to {delivery.name}
            {added < totals.count ? " (the rest were already on it)" : ""}.{" "}
            <Link
              href={`/deliveries/${encodeURIComponent(delivery.id)}`}
              className="font-medium underline underline-offset-2"
            >
              Open the board
            </Link>
          </>
        ),
      });
    } catch (err) {
      setNotice({
        tone: "error",
        text: err instanceof Error ? err.message : "Adding failed.",
      });
    } finally {
      setBusy(false);
    }
  };

  const createDelivery = async (name: string, customer: string) => {
    setBusy(true);
    setNotice(null);
    const ids = Array.from(selection.keys());
    try {
      const delivery = await fetcher<{ id: string }>("/api/deliveries", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          name,
          customer,
          task_ids: ids,
        }),
      });
      removeIds(ids);
      setNewOpen(false);
      router.push(`/deliveries/${delivery.id}`);
    } catch (err) {
      setNotice({
        tone: "error",
        text: err instanceof Error ? err.message : "Create failed.",
      });
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="space-y-2" data-testid="tasks-selection-bar">
      {totals.count > 0 ? (
        <div className="bg-muted/40 flex flex-wrap items-center gap-x-3 gap-y-2 rounded-md border border-[#6f88b4]/20 px-3 py-2 text-xs">
          <span className="font-medium tabular-nums">
            {totals.count.toLocaleString()} selected
          </span>
          {totals.cost !== null ? (
            <span className="text-muted-foreground tabular-nums">
              {totals.anyEstimated ? "~" : ""}
              {formatCostUsd(totals.cost)}
            </span>
          ) : null}
          <Button variant="ghost" size="sm" onClick={() => setReviewOpen(true)}>
            Review selection
          </Button>
          <Button variant="ghost" size="sm" disabled={busy} onClick={clear}>
            Clear selection
          </Button>
          {deliveryId && isOrgAdminRole(orgRole) ? (
            <Button
              className="ml-auto"
              size="sm"
              disabled={busy || !delivery || !!deliveryError}
              onClick={() => delivery && void addToExisting(delivery)}
            >
              Add {totals.count.toLocaleString()} to{" "}
              {delivery?.name ?? "delivery"}
            </Button>
          ) : null}
          {!deliveryId && isOrgAdminRole(orgRole) ? (
            <DropdownMenu open={menuOpen} onOpenChange={setMenuOpen}>
              <DropdownMenuTrigger asChild>
                <Button
                  type="button"
                  size="sm"
                  className="ml-auto h-7 gap-1.5 text-[11px]"
                  disabled={busy || totals.count === 0}
                >
                  <PackagePlus className="h-3.5 w-3.5" />
                  Add {totals.count.toLocaleString()} to delivery
                  <ChevronDown className="h-3 w-3" />
                </Button>
              </DropdownMenuTrigger>
              <DropdownMenuContent
                align="end"
                className="z-30 max-h-80 overflow-auto"
              >
                <DropdownMenuItem
                  onSelect={() => {
                    setNotice(null);
                    setNewOpen(true);
                  }}
                >
                  New delivery…
                </DropdownMenuItem>
                <DropdownMenuSeparator />
                <DropdownMenuLabel className="text-muted-foreground text-[11px] uppercase">
                  Active deliveries
                </DropdownMenuLabel>
                {deliveries === undefined ? (
                  <DropdownMenuItem disabled>Loading…</DropdownMenuItem>
                ) : active.length === 0 ? (
                  <DropdownMenuItem disabled>None yet</DropdownMenuItem>
                ) : (
                  active.map((d) => (
                    <DropdownMenuItem
                      key={d.id}
                      onSelect={() => void addToExisting(d)}
                    >
                      {d.name}
                      <span className="text-muted-foreground ml-2 text-[11px]">
                        {d.customer_name ?? "—"} · {d.task_count} tasks
                      </span>
                    </DropdownMenuItem>
                  ))
                )}
              </DropdownMenuContent>
            </DropdownMenu>
          ) : null}
        </div>
      ) : null}
      {notice && !newOpen ? (
        <p
          role={notice.tone === "error" ? "alert" : "status"}
          className={cn(
            "text-xs",
            notice.tone === "error"
              ? "text-destructive"
              : "text-muted-foreground"
          )}
        >
          {notice.text}
        </p>
      ) : null}
      <Dialog open={reviewOpen} onOpenChange={setReviewOpen}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>
              {totals.count.toLocaleString()} selected tasks
            </DialogTitle>
            <DialogDescription>
              Selection across all pages and filters.
            </DialogDescription>
          </DialogHeader>
          <div className="max-h-80 space-y-1 overflow-auto">
            {Array.from(selection, ([id, entry]) => (
              <div key={id} className="flex items-center justify-between gap-2">
                <Link
                  className="truncate text-sm underline"
                  href={`/tasks/${encodeURIComponent(id)}`}
                >
                  {entry.name ?? id}
                </Link>
                <Button
                  variant="ghost"
                  size="sm"
                  disabled={busy}
                  onClick={() => removeIds([id])}
                >
                  Remove
                </Button>
              </div>
            ))}
          </div>
        </DialogContent>
      </Dialog>
      <NewDeliveryDialog
        open={newOpen}
        onOpenChange={setNewOpen}
        busy={busy}
        count={totals.count}
        defaultCustomer={sp.get("not_delivered_to") ?? ""}
        onCreate={createDelivery}
        error={notice?.tone === "error" ? notice.text : null}
      />
    </div>
  );
}

function NewDeliveryDialog({
  open,
  onOpenChange,
  busy,
  count,
  defaultCustomer,
  onCreate,
  error,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  busy: boolean;
  count: number;
  defaultCustomer: string;
  onCreate: (name: string, customer: string) => void;
  error: ReactNode;
}) {
  const { data: customers } = useSWR<Customer[]>(
    open ? "/api/customers" : null,
    fetcher
  );
  const [name, setName] = useState("");
  const [customerId, setCustomerId] = useState("");
  // The lab picked in the bar ("not yet sent to X") is the obvious
  // recipient, so it is preselected when it names a known customer.
  useEffect(() => {
    if (!open || !customers || customerId) return;
    const match = customers.find(
      (c) => c.name.toLowerCase() === defaultCustomer.toLowerCase()
    );
    if (match) setCustomerId(match.id);
  }, [open, customers, customerId, defaultCustomer]);

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="sm:max-w-md">
        <DialogHeader>
          <DialogTitle>New delivery</DialogTitle>
          <DialogDescription>
            Starts a delivery with the {count.toLocaleString()} selected tasks
            and opens its board.
          </DialogDescription>
        </DialogHeader>
        <div className="space-y-3">
          <div className="space-y-1">
            <Label htmlFor="selection-delivery-name">Name</Label>
            <Input
              id="selection-delivery-name"
              value={name}
              onChange={(e) => setName(e.target.value)}
              placeholder="xAI · September batch"
            />
          </div>
          <div className="space-y-1">
            <Label>Customer</Label>
            <Select value={customerId} onValueChange={setCustomerId}>
              <SelectTrigger aria-label="Customer">
                <SelectValue placeholder="Choose a customer" />
              </SelectTrigger>
              <SelectContent>
                {(customers ?? []).map((c) => (
                  <SelectItem key={c.id} value={c.id}>
                    {c.name}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>
        </div>
        {error ? (
          <p role="alert" className="text-destructive text-sm">
            {error}
          </p>
        ) : null}
        <DialogFooter>
          <Button
            type="button"
            disabled={busy || !name.trim() || !customerId}
            onClick={() => onCreate(name.trim(), customerId)}
          >
            Create and add {count.toLocaleString()}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
