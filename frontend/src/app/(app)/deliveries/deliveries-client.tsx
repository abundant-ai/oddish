"use client";

import { useState } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useAuth } from "@clerk/nextjs";
import useSWR from "swr";
import { Package, Plus } from "lucide-react";

import { fetcher } from "@/lib/api";
import { isOrgAdminRole } from "@/lib/org-roles";
import type { DeliveryListItem } from "@/lib/types";
import { DeliveryStatusBadge } from "@/components/delivery-status";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import {
  Dialog,
  DialogContent,
  DialogFooter,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";

export function DeliveriesClient() {
  const router = useRouter();
  const { orgRole } = useAuth();
  const isAdmin = isOrgAdminRole(orgRole);
  const { data, error, isLoading, mutate } = useSWR<DeliveryListItem[]>(
    "/api/deliveries",
    fetcher,
  );

  const [createOpen, setCreateOpen] = useState(false);
  const [name, setName] = useState("");
  const [customer, setCustomer] = useState("");
  const [creating, setCreating] = useState(false);
  const [createError, setCreateError] = useState<string | null>(null);

  const createDelivery = async () => {
    setCreating(true);
    setCreateError(null);
    try {
      const res = await fetch("/api/deliveries", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          name: name.trim(),
          customer_name: customer.trim() || null,
        }),
      });
      const payload = (await res.json().catch(() => null)) as {
        id?: string;
        detail?: string;
        error?: string;
      } | null;
      if (!res.ok || !payload?.id) {
        throw new Error(
          payload?.detail || payload?.error || `Create failed (${res.status})`,
        );
      }
      setCreateOpen(false);
      setName("");
      setCustomer("");
      void mutate();
      router.push(`/deliveries/${payload.id}`);
    } catch (err) {
      setCreateError(err instanceof Error ? err.message : "Create failed");
    } finally {
      setCreating(false);
    }
  };

  return (
    <Card>
      <CardHeader className="flex flex-row items-center justify-between gap-2">
        <CardTitle className="flex items-center gap-2">
          <Package className="h-5 w-5" />
          Deliveries
        </CardTitle>
        {isAdmin && (
          <Dialog open={createOpen} onOpenChange={setCreateOpen}>
            <DialogTrigger asChild>
              <Button size="sm">
                <Plus className="mr-1 h-4 w-4" />
                New delivery
              </Button>
            </DialogTrigger>
            <DialogContent>
              <DialogHeader>
                <DialogTitle>New delivery</DialogTitle>
              </DialogHeader>
              <div className="space-y-3">
                <div className="space-y-1">
                  <Label htmlFor="delivery-name">Name</Label>
                  <Input
                    id="delivery-name"
                    value={name}
                    onChange={(e) => setName(e.target.value)}
                    placeholder="e.g. August batch"
                  />
                </div>
                <div className="space-y-1">
                  <Label htmlFor="delivery-customer">Customer</Label>
                  <Input
                    id="delivery-customer"
                    value={customer}
                    onChange={(e) => setCustomer(e.target.value)}
                    placeholder="optional"
                  />
                </div>
                {createError && (
                  <p className="text-sm text-destructive">{createError}</p>
                )}
              </div>
              <DialogFooter>
                <Button
                  onClick={() => void createDelivery()}
                  disabled={creating || !name.trim()}
                >
                  {creating ? "Creating…" : "Create"}
                </Button>
              </DialogFooter>
            </DialogContent>
          </Dialog>
        )}
      </CardHeader>
      <CardContent>
        {error ? (
          <p className="text-sm text-destructive">
            Failed to load deliveries: {error.message}
          </p>
        ) : isLoading ? (
          <p className="text-sm text-muted-foreground">Loading…</p>
        ) : !data || data.length === 0 ? (
          <p className="text-sm text-muted-foreground">
            No deliveries yet. A delivery is a checklist that tracks whether a
            set of tasks is ready to ship to a customer.
          </p>
        ) : (
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>Name</TableHead>
                <TableHead>Customer</TableHead>
                <TableHead>Status</TableHead>
                <TableHead className="text-right">Tasks</TableHead>
                <TableHead>Created</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {data.map((delivery) => (
                <TableRow key={delivery.id}>
                  <TableCell>
                    <Link
                      href={`/deliveries/${delivery.id}`}
                      className="font-medium hover:underline"
                    >
                      {delivery.name}
                    </Link>
                  </TableCell>
                  <TableCell className="text-muted-foreground">
                    {delivery.customer_name || "—"}
                  </TableCell>
                  <TableCell>
                    <DeliveryStatusBadge status={delivery.status} />
                  </TableCell>
                  <TableCell className="text-right">
                    {delivery.task_count}
                  </TableCell>
                  <TableCell className="text-muted-foreground">
                    {new Date(delivery.created_at).toLocaleDateString()}
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        )}
      </CardContent>
    </Card>
  );
}
