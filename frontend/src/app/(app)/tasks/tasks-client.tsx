"use client";

import { useEffect, useRef, useState } from "react";
import { usePathname, useRouter, useSearchParams } from "next/navigation";
import { Input } from "@/components/ui/input";
import { ChatButton } from "@/components/cc-chat/chat-button";
import { ImportDialog } from "@/components/import-dialog";
import { SavedFiltersMenu } from "@/components/saved-filters-menu";
import { TagFilterDropdown } from "@/components/tag-filter-dropdown";
import {
  SearchSyntaxHelp,
  SearchSyntaxMultiRow,
  SearchSyntaxRow,
} from "@/components/search-syntax-help";

// Toolbar for the tasks page header. Owns the search box and pushes it into the
// URL `q` param (debounced), which re-runs the server-rendered results. Also
// preserves the old 60s auto-refresh by re-fetching the server component.
export function TasksToolbar({ orgId = null }: { orgId?: string | null }) {
  const router = useRouter();
  const pathname = usePathname();
  const searchParams = useSearchParams();
  const [searchQuery, setSearchQuery] = useState(searchParams.get("q") ?? "");
  const isFirstRender = useRef(true);

  useEffect(() => {
    // Don't rewrite the URL on the initial mount (seeded from the URL).
    if (isFirstRender.current) {
      isFirstRender.current = false;
      return;
    }
    const handle = window.setTimeout(() => {
      const params = new URLSearchParams(searchParams.toString());
      const trimmed = searchQuery.trim();
      if (trimmed) params.set("q", trimmed);
      else params.delete("q");
      params.delete("offset");
      router.replace(`${pathname}?${params.toString()}`, { scroll: false });
    }, 300);
    return () => window.clearTimeout(handle);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [searchQuery]);

  useEffect(() => {
    const id = window.setInterval(() => router.refresh(), 60000);
    return () => window.clearInterval(id);
  }, [router]);

  return (
    <div className="flex flex-col gap-2 sm:flex-row sm:items-center">
      {orgId ? <ChatButton scopeKind="global" scopeId={orgId} /> : null}
      <div className="relative w-full sm:w-[260px]">
        <Input
          value={searchQuery}
          onChange={(event) => setSearchQuery(event.target.value)}
          placeholder="Search anything..."
          className="h-8 w-full border-[#6f88b4]/20 pr-7"
        />
        <SearchSyntaxHelp>
          <p className="font-medium">Search syntax</p>
          <p className="text-muted-foreground">
            Matches task name, author, or tags. Add a prefix below to specify
            filters.
          </p>
          <SearchSyntaxRow
            example="node vulnerability"
            hint="every word must match (AND)"
          />
          <SearchSyntaxRow example="auth OR rbac" hint="either word (OR)" />
          <SearchSyntaxRow example={'"command exec"'} hint="exact phrase" />
          <SearchSyntaxRow example="-no-skill" hint="exclude" />
          <SearchSyntaxMultiRow
            examples={["github:alice", "author:alice", "user:alice"]}
            hint="by author — GitHub handle, email, or name"
          />
          <SearchSyntaxRow example="tag:smoke" hint="by a specific tag" />
          <p className="text-muted-foreground">
            Filters stack (AND) and are case-insensitive, e.g.{" "}
            <code className="bg-muted rounded px-1 font-mono">
              rbac github:alice tag:smoke
            </code>
          </p>
        </SearchSyntaxHelp>
      </div>
      <TagFilterDropdown
        query={searchQuery}
        onQueryChange={setSearchQuery}
        countField="task_count"
      />
      <SavedFiltersMenu
        query={searchQuery}
        onApply={(text) => setSearchQuery(text)}
      />
      <ImportDialog onImported={() => router.refresh()} />
    </div>
  );
}
