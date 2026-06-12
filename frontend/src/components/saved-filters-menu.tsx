"use client";

import useSWR from "swr";

import { Button } from "@/components/ui/button";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { fetcher } from "@/lib/api";
import type { TagFilterAST } from "@/lib/types";

interface SavedFilterItem {
  id: string;
  name: string;
  filter_ast: TagFilterAST;
  visibility: "PRIVATE" | "ORG";
}

interface SavedFilterListResponse {
  items: SavedFilterItem[];
}

interface SavedFiltersMenuProps {
  currentFilter: TagFilterAST;
  onApply: (filter: TagFilterAST) => void;
}

export function SavedFiltersMenu({ currentFilter, onApply }: SavedFiltersMenuProps) {
  const { data, mutate } = useSWR<SavedFilterListResponse>("/api/tag-filters", fetcher);

  async function saveCurrent() {
    const name = window.prompt("Filter name?");
    if (!name) return;
    await fetch("/api/tag-filters", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name, filter_ast: currentFilter, visibility: "PRIVATE" }),
    });
    mutate();
  }

  const items = data?.items ?? [];

  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <Button variant="outline" size="sm">Saved filters</Button>
      </DropdownMenuTrigger>
      <DropdownMenuContent>
        {items.map((it) => (
          <DropdownMenuItem key={it.id} onClick={() => onApply(it.filter_ast)}>
            {it.name} ({it.visibility})
          </DropdownMenuItem>
        ))}
        <DropdownMenuItem onClick={saveCurrent}>Save current filter…</DropdownMenuItem>
      </DropdownMenuContent>
    </DropdownMenu>
  );
}
