import assert from "node:assert/strict";
import test from "node:test";
import {
  observeFileListRevision,
  receiveFileListRevision,
  type FileListRevision,
} from "../src/lib/file-list-revision.ts";

const pending: FileListRevision = {
  identity: "task/v1",
  observedHash: null,
  requestHash: null,
  receivedHash: null,
};

test("late details do not restart an in-flight listing for the same contents", () => {
  const details = observeFileListRevision(pending, "task/v1", "a");
  assert.equal(details.requestHash, null);
  assert.equal(receiveFileListRevision(details, "a").requestHash, null);
});

test("details arriving after the matching listing preserve the request identity", () => {
  const listing = receiveFileListRevision(pending, "a");
  assert.equal(
    observeFileListRevision(listing, "task/v1", "a").requestHash,
    null
  );
});

test("a listing from another revision is re-requested whichever response arrives first", () => {
  const details = observeFileListRevision(pending, "task/v1", "new");
  assert.equal(receiveFileListRevision(details, "old").requestHash, "new");
  const listing = receiveFileListRevision(pending, "old");
  assert.equal(
    observeFileListRevision(listing, "task/v1", "new").requestHash,
    "new"
  );
});

test("an in-place overwrite invalidates an already loaded listing", () => {
  const loaded = receiveFileListRevision(
    observeFileListRevision(pending, "task/v1", "old"),
    "old"
  );
  assert.equal(
    observeFileListRevision(loaded, "task/v1", "new").requestHash,
    "new"
  );
});

test("task or version navigation resets received-source bookkeeping", () => {
  const loaded = receiveFileListRevision(pending, "a");
  assert.deepEqual(observeFileListRevision(loaded, "task/v2", "b"), {
    identity: "task/v2",
    observedHash: "b",
    requestHash: "b",
    receivedHash: null,
  });
});

test("older servers without response fingerprints revalidate only once", () => {
  const details = observeFileListRevision(pending, "task/v1", "a");
  const retry = receiveFileListRevision(details, null);
  assert.equal(retry.requestHash, "a");
  assert.equal(receiveFileListRevision(retry, null).requestHash, "a");
  assert.equal(receiveFileListRevision(pending, null).requestHash, null);
});
