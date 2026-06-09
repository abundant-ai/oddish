"use client";

import { Tabs, TabsList, TabsTrigger, TabsContent } from "@/components/ui/tabs";
import { TagPicker } from "@/components/tag-picker";
import type { TagFilterAST } from "@/lib/types";

interface TagFilterPopoverProps {
  value: TagFilterAST;
  onChange: (next: TagFilterAST) => void;
}

export function TagFilterPopover({ value, onChange }: TagFilterPopoverProps) {
  return (
    <Tabs defaultValue="all" className="w-72">
      <TabsList className="grid grid-cols-3">
        <TabsTrigger value="all">AND</TabsTrigger>
        <TabsTrigger value="any">OR</TabsTrigger>
        <TabsTrigger value="none">NOT</TabsTrigger>
      </TabsList>
      <TabsContent value="all" className="p-2">
        <TagPicker
          selectedTagIds={value.all}
          onChange={(next) => onChange({ ...value, all: next })}
        />
      </TabsContent>
      <TabsContent value="any" className="p-2">
        <TagPicker
          selectedTagIds={value.any}
          onChange={(next) => onChange({ ...value, any: next })}
        />
      </TabsContent>
      <TabsContent value="none" className="p-2">
        <TagPicker
          selectedTagIds={value.none}
          onChange={(next) => onChange({ ...value, none: next })}
        />
      </TabsContent>
    </Tabs>
  );
}
