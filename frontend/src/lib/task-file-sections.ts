export type TaskFileSectionId =
  | "prompt"
  | "solution"
  | "verification"
  | "environment"
  | "metadata"
  | "tooling"
  | "other";

export interface TaskPathNode {
  name: string;
  path: string;
  type: "file" | "dir";
  children?: TaskPathNode[];
}

export type TaskFileSectionItem<T extends TaskPathNode> =
  | { kind: "node"; node: T }
  | { kind: "directory"; node: T };

export interface TaskFileSection<T extends TaskPathNode> {
  id: TaskFileSectionId;
  label: string;
  items: TaskFileSectionItem<T>[];
}

const SECTION_ORDER: Array<{
  id: TaskFileSectionId;
  label: string;
}> = [
  { id: "prompt", label: "Prompt" },
  { id: "solution", label: "Reference solution" },
  { id: "verification", label: "Verification" },
  { id: "environment", label: "Agent environment" },
  { id: "metadata", label: "Task metadata" },
  { id: "tooling", label: "Task tooling" },
  { id: "other", label: "Other files" },
];

export function taskFileSectionIdForRootNode(
  node: TaskPathNode
): TaskFileSectionId {
  if (
    (node.type === "file" && node.name === "instruction.md") ||
    (node.type === "dir" && node.name === "prompt")
  ) {
    return "prompt";
  }
  if (node.type === "dir" && node.name === "solution") return "solution";
  if (
    (node.type === "dir" && node.name === "tests") ||
    (node.type === "dir" && node.name === "verifier") ||
    (node.type === "file" && node.name === "test.sh")
  ) {
    return "verification";
  }
  if (node.type === "dir" && node.name === "environment") {
    return "environment";
  }
  if (node.type === "file" && node.name === "task.toml") return "metadata";
  if (node.type === "dir" && (node.name === "ops" || node.name === "tools")) {
    return "tooling";
  }
  return "other";
}

function hasTaskConvention(nodes: readonly TaskPathNode[]): boolean {
  return nodes.some((node) => taskFileSectionIdForRootNode(node) !== "other");
}

/**
 * Eager archive listings may contain one neutral wrapper directory. Directory-
 * paged listings resolve that wrapper in the file panel before calling this
 * function because their directory nodes deliberately have no children.
 */
function taskRootNodes<T extends TaskPathNode>(nodes: readonly T[]): T[] {
  if (nodes.length !== 1) return [...nodes];
  const only = nodes[0];
  if (only.type !== "dir" || !only.children) return [...nodes];
  if (taskFileSectionIdForRootNode(only) !== "other") return [...nodes];
  if (!hasTaskConvention(only.children)) return [...nodes];
  return only.children as T[];
}

/**
 * Classify only task-root names. A conventional directory is returned as a
 * directory source instead of being flattened here: eager callers read its
 * children from the node, while paged callers read the same path lazily.
 */
export function buildTaskFileSections<T extends TaskPathNode>(
  tree: readonly T[]
): TaskFileSection<T>[] {
  const buckets = new Map<TaskFileSectionId, TaskFileSectionItem<T>[]>();
  for (const node of taskRootNodes(tree)) {
    const sectionId = taskFileSectionIdForRootNode(node);
    const bucket = buckets.get(sectionId) ?? [];
    bucket.push(
      node.type === "dir" && sectionId !== "other"
        ? { kind: "directory", node }
        : { kind: "node", node }
    );
    buckets.set(sectionId, bucket);
  }

  const sections = SECTION_ORDER.flatMap(({ id, label }) => {
    const items = buckets.get(id);
    return items?.length ? [{ id, label, items }] : [];
  });

  if (sections.length === 1 && sections[0].id === "other") {
    return [{ ...sections[0], label: "Files" }];
  }
  return sections;
}
