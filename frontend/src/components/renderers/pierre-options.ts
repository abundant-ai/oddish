/**
 * Theming for pierre components — @pierre/diffs (code + diffs) and
 * @pierre/trees (file trees). Pierre renders inside a Shadow DOM, which app
 * styles can't reach, but CSS custom properties inherit across the boundary,
 * so everything here maps pierre variables to the app's theme variables.
 * Pierre derives its secondary shades (gutters, headers, separators, diff
 * tints) from `--diffs-*-bg` via color-mix, so setting the base background
 * re-themes the whole component.
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
 * @pierre/trees theming (its `unsafeCSS` option); `--trees-*-override` is the
 * library's theming surface. `color-scheme` must be pinned: the tree resolves
 * defaults it has no override for (e.g. file-type icon colors) via
 * `light-dark()`, which follows the OS preference, not `.dark` —
 * `--app-color-scheme` (globals.css) carries the app theme across the shadow
 * boundary. The transparent background lets the pane's own `bg-muted/30`
 * show through.
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
