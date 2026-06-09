"use client";

import { Eye, EyeOff } from "lucide-react";

import { Badge } from "@/components/ui/badge";
import type { UserTagRef } from "@/lib/types";
import { cn } from "@/lib/utils";

export interface TagChipProps {
  tag: UserTagRef;
  className?: string;
}

export function TagChip({ tag, className }: TagChipProps) {
  const dim = tag.older && !tag.current;
  const color = tag.color || undefined;
  return (
    <Badge
      variant="outline"
      className={cn(
        "inline-flex items-center gap-1 text-xs px-1.5 py-0",
        dim && "opacity-60",
        className,
      )}
      style={color ? { borderColor: color, color } : undefined}
    >
      {tag.visibility === "PUBLIC" ? (
        <Eye className="h-3 w-3" aria-label="Public tag" />
      ) : (
        <EyeOff className="h-3 w-3" aria-label="Private tag" />
      )}
      <span>{tag.key}</span>
      {tag.value ? <span className="opacity-60">:{tag.value}</span> : null}
    </Badge>
  );
}
