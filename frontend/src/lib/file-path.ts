/**
 * Whether two paths address the same file. Deep links may carry a bare
 * file name or partial path (the file trees resolve those by suffix
 * match), while panels report the canonical full path back up — an exact
 * compare would treat that echo as a file change and drop line anchors.
 */
export function sameFilePath(a: string | null, b: string | null): boolean {
  if (a === b) return true;
  if (!a || !b) return false;
  const [longer, shorter] = a.length >= b.length ? [a, b] : [b, a];
  return longer.endsWith(`/${shorter}`);
}
