"use client";

import { useEffect, useId, useState } from "react";
import useSWR from "swr";
import { fetcher } from "@/lib/api";
import type { Customer } from "@/lib/types";
import { CustomerCreateDialog } from "@/components/customer-create-dialog";
import { Button } from "@/components/ui/button";
import { Label } from "@/components/ui/label";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";

export function CustomerPicker({
  value,
  onValueChange,
  defaultName = "",
}: {
  value: string;
  onValueChange: (id: string) => void;
  defaultName?: string;
}) {
  const id = useId();
  const [createOpen, setCreateOpen] = useState(false);
  const {
    data: customers,
    error,
    isLoading,
    mutate,
  } = useSWR<Customer[]>("/api/customers", fetcher);

  // Resolve the delivery-history filter's customer name after the list loads.
  useEffect(() => {
    if (value || !defaultName) return;
    const match = customers?.find(
      (customer) => customer.name.toLowerCase() === defaultName.toLowerCase()
    );
    if (match) onValueChange(match.id);
  }, [customers, defaultName, value, onValueChange]);

  return (
    <div className="space-y-1">
      <Label htmlFor={id}>Customer</Label>
      <Select
        value={value}
        onValueChange={(next) => {
          if (next === "__new__") setCreateOpen(true);
          else onValueChange(next);
        }}
      >
        <SelectTrigger id={id}>
          <SelectValue
            placeholder={isLoading ? "Loading customers…" : "Choose a customer"}
          />
        </SelectTrigger>
        <SelectContent>
          {(customers ?? []).map((customer) => (
            <SelectItem key={customer.id} value={customer.id}>
              {customer.name}
            </SelectItem>
          ))}
          <SelectItem value="__new__">New customer…</SelectItem>
        </SelectContent>
      </Select>
      {error ? (
        <div role="alert" className="text-destructive text-sm">
          Could not load customers.
          <Button
            type="button"
            variant="link"
            size="sm"
            onClick={() => void mutate()}
          >
            Retry
          </Button>
        </div>
      ) : customers?.length === 0 ? (
        <p className="text-muted-foreground text-sm">No customers yet.</p>
      ) : null}
      <CustomerCreateDialog
        open={createOpen}
        onOpenChange={setCreateOpen}
        onCreated={(customer) => {
          void mutate(
            (current) => [
              ...(current ?? []).filter((entry) => entry.id !== customer.id),
              customer,
            ],
            { revalidate: false }
          );
          onValueChange(customer.id);
        }}
      />
    </div>
  );
}
