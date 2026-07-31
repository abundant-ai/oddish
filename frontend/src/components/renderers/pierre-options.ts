/**
 * Shared configuration for @pierre/diffs components (code + diff rendering).
 *
 * Pierre renders inside a Shadow DOM, so app styles don't reach it; theming
 * goes through its CSS custom properties instead. The font variable is
 * inherited from the document root (custom properties cross shadow
 * boundaries), so the app's Geist Mono applies inside pierre too.
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
  }
`;

/** Blends pierre's background with the app's panel background. */
export const PIERRE_STYLE_OVERRIDES = (isDark: boolean) =>
  ({
    "--diffs-bg-buffer-override": "transparent",
    "--diffs-bg-context-override": "transparent",
    "--diffs-bg-separator-override": isDark ? "#262626" : "#f5f5f5",
    "--diffs-fg-number-override": isDark
      ? "rgba(161,161,161,0.5)"
      : "rgba(113,113,113,0.5)",
  }) as React.CSSProperties;
