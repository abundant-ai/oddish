/**
 * Parses a task search string into free text plus tag-filter buckets.
 *
 * Grammar (whitespace-separated tokens):
 *  - `tag:<name>` is a tag token; `-tag:<name>` is a negated tag token. The
 *    `tag:` prefix is case-insensitive; the name keeps its original casing.
 *  - Standalone `AND` / `OR` / `NOT` (case-insensitive) are operators ONLY when
 *    the next token is a tag token. `AND` is a no-op (the default). `NOT`
 *    negates the following tag token (→ `none`). `OR` between two tag tokens
 *    puts both operands into `any`, re-homing the left operand out of `all` if
 *    it was just placed there. A negated tag never participates in OR re-homing.
 *  - `"quoted text"` (optionally `-` prefixed) is kept together as a single
 *    token, quotes included, so phrases survive into the free text. A quoted
 *    token is never a tag token or an operator.
 *  - Any other token is ordinary search text (joined with single spaces) and
 *    resets the pending operator state.
 *
 * The free text is forwarded verbatim to the backend, which parses the
 * include/exclude/phrase grammar (see `parse_search_query` in
 * `oddish/core/helpers.py`): terms are AND'd, `"quoted text"` matches
 * contiguously, a leading `-` excludes.
 *
 * Examples:
 *  - `parseTaskSearch("flaky tag:smoke-test")`
 *      → { text: "flaky", all: ["smoke-test"], any: [], none: [] }
 *  - `parseTaskSearch("tag:a OR tag:b NOT tag:c foo")`
 *      → { text: "foo", all: [], any: ["a", "b"], none: ["c"] }
 *  - `parseTaskSearch("not really tag related")`
 *      → { text: "not really tag related", all: [], any: [], none: [] }
 *  - `parseTaskSearch('"exact phrase" -noisy tag:smoke')`
 *      → { text: '"exact phrase" -noisy', all: ["smoke"], any: [], none: [] }
 */

export interface ParsedTaskSearch {
  text: string;
  all: string[];
  any: string[];
  none: string[];
}

type TagToken = { name: string; negated: boolean };

function asTagToken(token: string): TagToken | null {
  let negated = false;
  let body = token;
  if (body.startsWith("-")) {
    negated = true;
    body = body.slice(1);
  }
  if (!/^tag:/i.test(body)) return null;
  const name = body.slice(4); // "tag:".length === 4
  if (name.length === 0) return null;
  return { name, negated };
}

function asOperator(token: string): "AND" | "OR" | "NOT" | null {
  const upper = token.toUpperCase();
  if (upper === "AND" || upper === "OR" || upper === "NOT") return upper;
  return null;
}

/**
 * Whitespace tokenizer that keeps `"quoted text"` (with an optional leading
 * `-`) together as one token, quotes included. An unterminated quote runs to
 * the end of the string, mirroring the backend scanner. Quotes inside a bare
 * word (`foo"bar`) stay part of that word.
 */
function tokenize(raw: string): string[] {
  return raw.match(/-?"[^"]*("|$)[^\s]*|\S+/g) ?? [];
}

export function parseTaskSearch(raw: string): ParsedTaskSearch {
  const tokens = tokenize(raw);
  const textParts: string[] = [];
  const all: string[] = [];
  const any: string[] = [];
  const none: string[] = [];

  // Operator queued up for the next tag token.
  let pendingOp: "OR" | "NOT" | null = null;
  // The previous tag token, used as the left operand for OR re-homing. Cleared
  // by any plain-text token so OR only re-homes an immediately adjacent tag.
  let prevTag: { name: string; negated: boolean; inAll: boolean } | null = null;

  for (let i = 0; i < tokens.length; i += 1) {
    const token = tokens[i];
    const tag = asTagToken(token);

    if (tag) {
      if (tag.negated || pendingOp === "NOT") {
        none.push(tag.name);
        prevTag = { name: tag.name, negated: true, inAll: false };
      } else if (pendingOp === "OR") {
        if (prevTag && !prevTag.negated && prevTag.inAll) {
          // prevTag is always the most recent `all` entry at this point.
          all.pop();
          any.push(prevTag.name);
        }
        any.push(tag.name);
        prevTag = { name: tag.name, negated: false, inAll: false };
      } else {
        all.push(tag.name);
        prevTag = { name: tag.name, negated: false, inAll: true };
      }
      pendingOp = null;
      continue;
    }

    const op = asOperator(token);
    // Operators apply only when the next token is itself a tag token.
    if (op && i + 1 < tokens.length && asTagToken(tokens[i + 1])) {
      pendingOp = op === "AND" ? null : op;
      continue;
    }

    textParts.push(token);
    pendingOp = null;
    prevTag = null;
  }

  return { text: textParts.join(" "), all, any, none };
}

/**
 * Inverse of `parseTaskSearch`: renders a parsed filter back into search-bar
 * text. Round-trips through the parser: ALL terms emit as bare `tag:` tokens,
 * ANY terms as an OR chain (the parser re-homes the chain head), NONE terms
 * as `NOT tag:` tokens. A single-element ANY parses back into ALL, which is
 * semantically identical for filtering.
 */
export function serializeTaskSearch(parsed: ParsedTaskSearch): string {
  const parts: string[] = [];
  if (parsed.text) parts.push(parsed.text);
  for (const name of parsed.all) parts.push(`tag:${name}`);
  if (parsed.any.length > 0) {
    parts.push(parsed.any.map((name) => `tag:${name}`).join(" OR "));
  }
  for (const name of parsed.none) parts.push(`NOT tag:${name}`);
  return parts.join(" ");
}
