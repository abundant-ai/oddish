/**
 * Shared configuration for @pierre/diffs components (code + diff rendering).
 *
 * Pierre renders inside a Shadow DOM, so app styles don't reach it — but CSS
 * custom properties inherit across the shadow boundary. Everything here is
 * wired to the app's own theme variables (which flip with the `.dark` class),
 * so pierre panes take on oddish's palette instead of pierre's stock
 * white/black. Pierre derives all its secondary shades (gutters, headers,
 * separators, diff tints) from `--diffs-*-bg` via color-mix, so setting the
 * base background to the app's card color re-themes the whole component.
 */

export const PIERRE_THEME = {
  dark: "pierre-dark",
  light: "pierre-light",
} as const;

export const PIERRE_UNSAFE_CSS = `
  :host {
    --diffs-font-family: var(--font-geist-mono), ui-monospace, SFMono-Regular, monospace;
    --diffs-header-font-family: var(--font-geist-mono), ui-monospace, SFMono-Regular, monospace;
    --diffs-tab-size: 2;
    --diffs-font-size: 0.75rem;
    --diffs-line-height: 1.25rem;
    /* Pierre pads the code block by this much above line 1 (its own default
     * is the same 8px, so this changes nothing visually). Pinned because
     * line-anchored overlays add it to their offsets — see CODE_TOP_REM in
     * comments/qa-comments.tsx. */
    --diffs-gap-block: 0.5rem;
    --diffs-light-bg: var(--color-card);
    --diffs-dark-bg: var(--color-card);
    --diffs-fg-number-override: hsl(var(--muted-foreground) / 0.55);
    --diffs-bg-separator-override: var(--color-border);
  }
`;
