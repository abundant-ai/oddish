"use client";

import dynamic from "next/dynamic";

// The single lazy entry for TagPicker: keeps cmdk out of every route's
// initial bundle; the chunk loads when a picker actually mounts
// (popover/dialog open). Import the component from here and types from
// "@/components/tag-picker" (type imports are erased at build time).
export const TagPicker = dynamic(
  () => import("@/components/tag-picker").then((mod) => mod.TagPicker),
  {
    ssr: false,
  },
);
