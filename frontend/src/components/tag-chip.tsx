"use client";

import { Tag } from "lucide-react";

import { Badge } from "@/components/ui/badge";
import { tagColor } from "@/lib/tag-colors";
import type { UserTagRef } from "@/lib/types";
import { cn } from "@/lib/utils";

export interface TagChipProps {
  tag: UserTagRef;
  className?: string;
}

export function TagChip({ tag, className }: TagChipProps) {
  const dim = tag.older && !tag.current;
  const color = tagColor(tag.key, tag.color);
  return (
    <Badge
      variant="outline"
      className={cn(
        "inline-flex items-center gap-1 text-xs px-1.5 py-0",
        dim && "opacity-60",
        className,
      )}
    >
      <Tag
        className="h-3 w-3 shrink-0"
        style={{ color }}
        fill={color}
        fillOpacity={0.25}
        aria-label={tag.visibility === "PUBLIC" ? "Public tag" : "Private tag"}
      />
      <span>{tag.key}</span>
      {tag.value ? <span className="opacity-60">:{tag.value}</span> : null}
    </Badge>
  );
}
