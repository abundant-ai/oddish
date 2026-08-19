/**
 * Shared configuration for pierre components — @pierre/diffs (code + diff
 * rendering) and @pierre/trees (file trees).
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
    --diffs-light-bg: var(--color-card);
    --diffs-dark-bg: var(--color-card);
    --diffs-fg-number-override: hsl(var(--muted-foreground) / 0.55);
    --diffs-bg-separator-override: var(--color-border);
  }
`;

/**
 * Theme bridge for @pierre/trees, passed as the tree's `unsafeCSS`.
 *
 * Two things are going on:
 *
 * 1. `color-scheme`. The tree declares `color-scheme: light dark` and resolves
 *    ~27 defaults (notably the file-type icon palette, which has no per-hue
 *    override) through `light-dark()`. Left alone that follows the *OS*
 *    preference, not oddish's `.dark` class, so a light-theme user on a dark
 *    machine would get dark icons. `--app-color-scheme` (globals.css) inherits
 *    across the shadow boundary and pins it to the app's theme instead.
 * 2. The `--trees-*-override` variables, which are the library's supported
 *    theming surface — same pattern as `--diffs-*-override` above. Row colors
 *    mirror what the hand-rolled trees used: `bg-primary/20` + `text-primary`
 *    for the selected row, `bg-muted` on hover, mono type at `text-xs`.
 *
 * The background stays transparent so the pane's own `bg-muted/30` shows
 * through; the tree still derives its scrollbar thumb and indent guides from
 * the foreground colors set here.
 */
export const TREES_UNSAFE_CSS = `
  :host {
    color-scheme: var(--app-color-scheme, light dark);
    --trees-font-family-override: var(--font-geist-mono), ui-monospace, SFMono-Regular, monospace;
    --trees-font-size-override: 0.75rem;
    --trees-bg-override: transparent;
    --trees-fg-override: var(--color-foreground);
    --trees-fg-muted-override: var(--color-muted-foreground);
    --trees-accent-override: var(--color-primary);
    --trees-bg-muted-override: var(--color-muted);
    --trees-selected-bg-override: hsl(var(--primary) / 0.2);
    --trees-selected-fg-override: var(--color-primary);
    --trees-border-color-override: var(--color-border);
    --trees-focus-ring-color-override: var(--color-ring);
    --trees-input-bg-override: var(--color-background);
    --trees-search-fg-override: var(--color-foreground);
  }
`;
