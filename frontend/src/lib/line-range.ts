/**
 * Line-anchor ranges for file previews, carried in the URL as
 * ``?lines=L12`` (single line) or ``?lines=L12-L20`` (inclusive range).
 * The parser is deliberately lenient (accepts ``12``, ``12-20``,
 * ``L12-20``); the formatter always emits the canonical ``L`` form so
 * shared links look the same everywhere.
 */
export interface LineRange {
  /** 1-indexed first line of the range (inclusive). */
  start: number;
  /** 1-indexed last line of the range (inclusive, >= start). */
  end: number;
}

const LINE_RANGE_PATTERN = /^L?(\d+)(?:-L?(\d+))?$/i;

export function parseLineRange(
  raw: string | null | undefined
): LineRange | null {
  if (!raw) return null;
  const match = LINE_RANGE_PATTERN.exec(raw.trim());
  if (!match) return null;
  const first = Number.parseInt(match[1], 10);
  const second = match[2] ? Number.parseInt(match[2], 10) : first;
  if (!Number.isFinite(first) || first < 1) return null;
  if (!Number.isFinite(second) || second < 1) return null;
  return first <= second
    ? { start: first, end: second }
    : { start: second, end: first };
}

export function formatLineRange(range: LineRange): string {
  return range.start === range.end
    ? `L${range.start}`
    : `L${range.start}-L${range.end}`;
}

export function lineRangesEqual(
  a: LineRange | null,
  b: LineRange | null
): boolean {
  if (a === null || b === null) return a === b;
  return a.start === b.start && a.end === b.end;
}

/**
 * Shift-click semantics: extend an existing selection to ``line``, anchored
 * at the existing start (or select the single line when nothing is
 * selected yet).
 */
export function extendLineRange(
  range: LineRange | null,
  line: number
): LineRange {
  if (!range) return { start: line, end: line };
  return line < range.start
    ? { start: line, end: range.start }
    : { start: range.start, end: line };
}

export function isLineInRange(range: LineRange | null, line: number): boolean {
  return range !== null && line >= range.start && line <= range.end;
}

export function lineRangesIntersect(
  a: LineRange | null,
  b: LineRange | null
): boolean {
  if (a === null || b === null) return false;
  return a.start <= b.end && b.start <= a.end;
}
