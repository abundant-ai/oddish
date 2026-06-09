import type {
  UserTagRef,
  TagFilterAST,
  TaskBrowseItem,
  Task,
} from "@/lib/types";

const _ref: UserTagRef = {
  tag_id: "t-1",
  key: "flaky",
  visibility: "PRIVATE",
  current: true,
  older: false,
};

const _ast: TagFilterAST = { all: ["flaky"], any: ["regression"], none: ["wip"] };

// Field-existence checks (avoid brittle full-object literals):
const _browseTags: TaskBrowseItem["user_tags"] = [_ref];
const _taskTags: NonNullable<Task["user_tags"]> = [_ref];

void _ast;
void _browseTags;
void _taskTags;
