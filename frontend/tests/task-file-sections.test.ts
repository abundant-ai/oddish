import assert from "node:assert/strict";
import test from "node:test";

import {
  buildTaskFileSections,
  taskFileSectionIdForRootNode,
  type TaskPathNode,
} from "../src/lib/task-file-sections.ts";

function file(path: string): TaskPathNode {
  return {
    name: path.split("/").at(-1) ?? path,
    path,
    type: "file",
  };
}

function dir(path: string, children?: TaskPathNode[]): TaskPathNode {
  return {
    name: path.split("/").at(-1) ?? path,
    path,
    type: "dir",
    children,
  };
}

function sectionLabels(nodes: TaskPathNode[]) {
  return buildTaskFileSections(nodes).map((section) => section.label);
}

test("classifies shallow task-root directories without loaded children", () => {
  const solution = dir("solution");
  const tests = dir("tests");
  const environment = dir("environment");
  const sections = buildTaskFileSections([
    environment,
    file("instruction.md"),
    solution,
    tests,
    file("task.toml"),
  ]);

  assert.deepEqual(
    sections.map((section) => section.label),
    [
      "Prompt",
      "Reference solution",
      "Verification",
      "Agent environment",
      "Task metadata",
    ]
  );
  assert.deepEqual(sections[1].items, [{ kind: "directory", node: solution }]);
  assert.deepEqual(sections[2].items, [{ kind: "directory", node: tests }]);
  assert.deepEqual(sections[3].items, [
    { kind: "directory", node: environment },
  ]);
});

test("groups conventional task files in review order", () => {
  const instruction = file("instruction.md");
  const solve = dir("solution", [file("solution/solve.sh")]);
  const verifier = dir("verifier", [file("verifier/grade.py")]);
  const tests = dir("tests", [file("tests/test.sh")]);
  const app = dir("environment", [file("environment/Dockerfile")]);
  const metadata = file("task.toml");
  const tools = dir("tools", [file("tools/calibrate.py")]);
  const ops = dir("ops", [file("ops/evidence.json")]);
  const notes = file("notes.txt");
  const sections = buildTaskFileSections([
    app,
    instruction,
    notes,
    solve,
    metadata,
    tests,
    verifier,
    ops,
    tools,
  ]);

  assert.deepEqual(
    sections.map((section) => section.label),
    [
      "Prompt",
      "Reference solution",
      "Verification",
      "Agent environment",
      "Task metadata",
      "Task tooling",
      "Other files",
    ]
  );
  assert.deepEqual(
    sections[2].items.map((item) => item.node),
    [tests, verifier]
  );
  assert.deepEqual(
    sections[5].items.map((item) => item.node),
    [ops, tools]
  );
  assert.equal(sections[0].items[0].node, instruction);
});

test("unwraps one eager archive wrapper without changing canonical paths", () => {
  const instruction = file("task-name/instruction.md");
  const solution = dir("task-name/solution", [
    file("task-name/solution/solve.sh"),
  ]);
  const sections = buildTaskFileSections([
    dir("task-name", [instruction, solution, file("task-name/task.toml")]),
  ]);

  assert.deepEqual(
    sections.map((section) => section.label),
    ["Prompt", "Reference solution", "Task metadata"]
  );
  assert.equal(sections[0].items[0].node, instruction);
  assert.equal(sections[1].items[0].node.path, "task-name/solution");
});

test("keeps a sole semantic directory as one directory source", () => {
  const tests = dir("tests", [
    file("tests/instruction.md"),
    file("tests/test.sh"),
    file("tests/helper.py"),
  ]);
  const sections = buildTaskFileSections([tests]);

  assert.deepEqual(sectionLabels([tests]), ["Verification"]);
  assert.deepEqual(sections[0].items, [{ kind: "directory", node: tests }]);
});

test("does not classify nested test-like application files as verification", () => {
  const environment = dir("environment", [
    dir("environment/app", [file("environment/app/src/test_helpers.py")]),
  ]);
  const sections = buildTaskFileSections([environment, file("test.sh")]);

  assert.deepEqual(
    sections.map((section) => section.label),
    ["Verification", "Agent environment"]
  );
  assert.equal(sections[1].items[0].node, environment);
});

test("falls back to the unchanged tree for a custom layout", () => {
  const source = dir("src", [file("src/main.py")]);
  const readme = file("README.md");
  const sections = buildTaskFileSections([source, readme]);

  assert.deepEqual(sections, [
    {
      id: "other",
      label: "Files",
      items: [
        { kind: "node", node: source },
        { kind: "node", node: readme },
      ],
    },
  ]);
});

test("recognizes only exact task-root conventions", () => {
  assert.equal(taskFileSectionIdForRootNode(file("instruction.md")), "prompt");
  assert.equal(taskFileSectionIdForRootNode(file("Instruction.md")), "other");
  assert.equal(taskFileSectionIdForRootNode(dir("tests")), "verification");
  assert.equal(taskFileSectionIdForRootNode(dir("test")), "other");
  assert.equal(taskFileSectionIdForRootNode(dir("tools")), "tooling");
  assert.equal(taskFileSectionIdForRootNode(file("tools")), "other");
});
