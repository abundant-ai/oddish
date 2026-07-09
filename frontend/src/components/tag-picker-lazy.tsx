"use client";

import dynamic from "next/dynamic";

export const TagPicker = dynamic(
  () => import("@/components/tag-picker").then((mod) => mod.TagPicker),
  {
    ssr: false,
  },
);
