/** Revision bookkeeping for a listing that can start before task details arrive. */
export interface FileListRevision {
  identity: string;
  observedHash: string | null;
  requestHash: string | null;
  receivedHash: string | null;
}

export function observeFileListRevision(
  previous: FileListRevision,
  identity: string,
  hash: string | null
): FileListRevision {
  if (previous.identity !== identity) {
    return {
      identity,
      observedHash: hash,
      requestHash: hash,
      receivedHash: null,
    };
  }
  if (hash === previous.observedHash) return previous;
  const changed =
    hash !== null &&
    (previous.observedHash !== null ||
      (previous.receivedHash !== null && previous.receivedHash !== hash));
  return {
    ...previous,
    observedHash: hash,
    requestHash: changed ? hash : previous.requestHash,
  };
}

export function receiveFileListRevision(
  previous: FileListRevision,
  hash: string | null
): FileListRevision {
  // Older servers omit the response fingerprint. Revalidate once with the
  // known hash, then accept that response to preserve rolling-deploy support.
  const changed =
    previous.observedHash !== null &&
    previous.observedHash !== hash &&
    (hash !== null || previous.requestHash !== previous.observedHash);
  return {
    ...previous,
    receivedHash: hash,
    requestHash: changed ? previous.observedHash : previous.requestHash,
  };
}
