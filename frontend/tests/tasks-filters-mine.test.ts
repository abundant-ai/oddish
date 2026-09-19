import assert from "node:assert/strict";
import test from "node:test";
import {
  backendAuthorParams,
  FILTER_PARAM_KEYS,
  filterParams,
  searchParamsToFilters,
} from "../src/lib/tasks-filters.ts";

// `author` and `mine` are URL filters like every other: written by
// filterParams, read back by searchParamsToFilters, and known to the
// sidebar's clear-on-change loop so a sidebar edit cannot drop them.
test("author and mine round-trip through the URL", () => {
  const empty = searchParamsToFilters(new URLSearchParams());
  assert.deepEqual(empty.author, []);
  assert.equal(empty.mine, null);

  const params = new URLSearchParams(
    filterParams({ ...empty, author: ["alice", "me"], mine: "only" })
  );
  assert.equal(params.get("author"), "alice,me");
  assert.equal(params.get("mine"), "only");
  for (const key of params.keys()) {
    assert.ok((FILTER_PARAM_KEYS as readonly string[]).includes(key), key);
  }
  const back = searchParamsToFilters(params);
  assert.deepEqual(back.author, ["alice", "me"]);
  assert.equal(back.mine, "only");
  // An unknown mode reads as unset rather than as a string the bar can't show.
  assert.equal(
    searchParamsToFilters(new URLSearchParams("mine=sideways")).mine,
    null
  );
});

// The proxy turns the display params into the backend's author/pin_author:
// a bare /tasks pins the caller's tasks first, `mine=only` filters to them,
// `mine=off` does neither, and the `author` param unions with the search
// box's github:/author: tokens.
test("mine mode resolves to pin_author / author for the backend", () => {
  assert.deepEqual(backendAuthorParams(null, []), {
    author: [],
    pinAuthor: true,
  });
  assert.deepEqual(backendAuthorParams("first", ["", "bob"]), {
    author: ["bob"],
    pinAuthor: true,
  });
  assert.deepEqual(backendAuthorParams("only", ["bob", "bob"]), {
    author: ["bob", "me"],
    pinAuthor: false,
  });
  assert.deepEqual(backendAuthorParams("off", ["carol"]), {
    author: ["carol"],
    pinAuthor: false,
  });
});
