// Analyzer reports cite trials with the canonical deep link
// `/tasks/{task_id}/probe/{trial_id}`. The analyzer prompt asks the LLM to
// embed these as `[trajectory](/tasks/.../probe/...)`, but the model sometimes
// inverts the order — `[/tasks/.../probe/...](a descriptive sentence)` — which
// CommonMark can't parse as a link (a bare destination may not contain spaces),
// so it renders as dead plain text instead of a blue link.
//
// This normalizer runs before react-markdown and repairs those cases so the
// path always lands in the href position. It's a no-op for correctly formed
// links and for markdown that contains no trajectory paths.

// A task/trial id segment: starts and ends with an alphanumeric-ish char so a
// trailing sentence period isn't swallowed into the path.
const SEG = String.raw`[A-Za-z0-9_-](?:[A-Za-z0-9._-]*[A-Za-z0-9_-])?`;
const TRAJ = String.raw`/tasks/${SEG}/probe/${SEG}`;

const VALID_HREF = /^(https?:\/\/|\/|#|mailto:|tel:)/;

// Escape brackets so a repaired label can't break the surrounding link syntax.
const escapeLabel = (s: string) => s.replace(/([[\]])/g, "\\$1");

// [<trajectory path>](<destination>) — the inverted/broken shape.
const INVERTED_LINK = new RegExp(
  String.raw`\[\s*(${TRAJ})\s*\]\(([^)]*)\)`,
  "g"
);

// A bare trajectory path. The lookbehind avoids matching mid-token or when the
// path is already the text/href of a link; the lookahead only rejects genuine
// path continuations (word char, hyphen, slash) so a trailing sentence period
// stays outside the link.
const BARE_PATH = new RegExp(
  String.raw`(?<![[(\w./-])(${TRAJ})(?![\w/-])`,
  "g"
);

// A complete markdown link, used to shield existing links from the bare-path
// pass (so a path inside another link's text isn't turned into a nested link).
const MARKDOWN_LINK = /(\[[^\]]*\]\([^)]*\))/g;

function repair(text: string): string {
  // 1. Fix inverted links: move the path into the href, keep the descriptive
  //    text (if any) as the visible label. Leave genuine links alone.
  const fixed = text.replace(
    INVERTED_LINK,
    (match, path: string, dest: string) => {
      const label = dest.trim();
      if (VALID_HREF.test(label)) return match; // real link, path just in text
      return `[${escapeLabel(label) || "trajectory"}](${path})`;
    }
  );

  // 2. Linkify bare paths, skipping anything already inside a markdown link
  //    (including the links produced by step 1, at odd split indices).
  return fixed
    .split(MARKDOWN_LINK)
    .map((seg, i) =>
      i % 2 === 1
        ? seg
        : seg.replace(BARE_PATH, (_m, path: string) => `[trajectory](${path})`)
    )
    .join("");
}

/**
 * Rewrites trajectory-path references so they always render as blue links,
 * regardless of how the LLM formatted them. Code spans and fenced code blocks
 * are left untouched.
 */
export function linkifyTrajectories(markdown: string): string {
  // Split out inline code and fenced blocks (captured, so they survive at odd
  // indices) and transform only the plain-text segments.
  const parts = markdown.split(/(```[\s\S]*?```|`[^`\n]*`)/g);
  return parts.map((part, i) => (i % 2 === 1 ? part : repair(part))).join("");
}
