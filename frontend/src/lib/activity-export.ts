import {
  STEP_BAND_TITLE,
  TIMELINE_CAPTION,
  TOKEN_BAND_TITLE,
  type ActivityViewModel,
} from "@/lib/activity-view-model";

export type ActivityImageFormat = "png" | "jpeg";

/**
 * Geometry mirroring the card's Tailwind classes, in CSS pixels. Verified
 * against the mounted card's measured boxes, so the numbers below are the DOM's
 * numbers: CardHeader p-3 pb-2 around a h-7 title row, CardContent p-3 pt-0,
 * space-y-4 sections, w-36 / w-16 columns, h-2 / h-10 / h-3 bands.
 */
const PAD = 12;
const CARD_RADIUS = 8;
/** The h-7 export button, not the text, sets the header's title row height. */
const TITLE_ROW_H = 28;
const TITLE_LINE_H = 20;
const TITLE_FS = 14;
/** CardTitle keeps `tracking-tight` (-0.025em) under the text-sm override. */
const TITLE_TRACKING = -0.025;
const TITLE_GAP = 8;
const ICON = 16;
const ICON_GAP = 8;
const SECTION_GAP = 16;
const ROW_H = 16;
const ROW_GAP = 4;
const SECTION_HEAD_GAP = 6;
const LABEL_W = 144;
const VALUE_W = 64;
const COL_GAP = 8;
const SWATCH_W = 4;
const SWATCH_H = 12;
const SWATCH_GAP = 6;
const TRACK_H = 8;
const BAR_GAP = 2;
const BAR_MIN_W = 3;
const RADIUS_SM = 4;
/** `min-w-[560px]` on the column the three bands share. */
const BAND_MIN_W = 560;
const BAND_H = 40;
const BAND_GAP = 2;
const STEP_MIN_W = 1;
const HEAT_GAP = 4;
const HEAT_H = 12;
const HEAT_MIN_W = 1;
const TOKEN_BAND_GAP = 12;
const CAPTION_GAP = 8;
const CAPTION_FS = 10.5;
const CAPTION_LINE_H = 15.75;
const STAR_SIZE = 12;
const STAR_INSET = 2;
/** Blink resolves box sizes in 1/64 px before painting them. */
const LAYOUT_UNIT = 1 / 64;

/** lucide `activity` and `star`, verbatim from the icons the card renders. */
const ACTIVITY_PATH =
  "M22 12h-2.48a2 2 0 0 0-1.93 1.46l-2.35 8.36a.25.25 0 0 1-.48 0L9.24 2.18a.25.25 0 0 0-.48 0l-2.35 8.36A2 2 0 0 1 4.49 12H2";
const STAR_PATH =
  "M11.525 2.295a.53.53 0 0 1 .95 0l2.31 4.679a2.123 2.123 0 0 0 1.595 1.16l5.166.756a.53.53 0 0 1 .294.904l-3.736 3.638a2.123 2.123 0 0 0-.611 1.878l.882 5.14a.53.53 0 0 1-.771.56l-4.618-2.428a2.122 2.122 0 0 0-1.973 0L6.396 21.01a.53.53 0 0 1-.77-.56l.881-5.139a2.122 2.122 0 0 0-.611-1.879L2.16 9.795a.53.53 0 0 1 .294-.906l5.165-.755a2.122 2.122 0 0 0 1.597-1.16z";
const ICON_VIEWBOX = 24;
const ICON_STROKE = 2;

/**
 * Concrete values for the theme's CSS expressions. Everything the card paints
 * is a `var()` / `color-mix()` / `hsl(var())` string, which canvas cannot parse;
 * a hidden probe inside the card resolves each one through the cascade so the
 * export inherits the live theme (light/dark) with no palette duplicated here.
 * Utility classes are resolved by wearing them, so a `bg-muted/40` track can
 * never drift from the one the card paints.
 */
function styleProbe(host: HTMLElement) {
  const probe = document.createElement("span");
  probe.style.display = "none";
  host.appendChild(probe);
  // A laid-out (not `display:none`) twin, used to ask the browser where it puts
  // a baseline: canvas font metrics and Chrome's line-box metrics round
  // differently, and guessing costs a pixel of drift on small text.
  const ruler = document.createElement("div");
  ruler.style.cssText =
    "position:absolute;left:-9999px;top:0;visibility:hidden;white-space:nowrap";
  const strut = document.createElement("span");
  strut.style.cssText = "display:inline-block;width:0;height:0";
  ruler.appendChild(strut);
  host.appendChild(ruler);
  const cache = new Map<string, string>();
  const read = (key: string, compute: () => string) => {
    const hit = cache.get(key);
    if (hit !== undefined) return hit;
    const value = compute();
    cache.set(key, value);
    return value;
  };
  const reset = () => {
    probe.className = "";
    probe.style.color = "";
  };
  return {
    /** A CSS color expression, resolved to a concrete color. */
    color(value: string): string {
      return read(`c:${value}`, () => {
        reset();
        probe.style.color = value;
        return getComputedStyle(probe).color;
      });
    },
    /** `color` / `background-color` as a Tailwind utility class paints it. */
    classColor(className: string, prop: "color" | "backgroundColor"): string {
      return read(`k:${className}:${prop}`, () => {
        reset();
        probe.className = className;
        return getComputedStyle(probe)[prop];
      });
    },
    classFont(className: string): string {
      return read(`f:${className}`, () => {
        reset();
        probe.className = className;
        return getComputedStyle(probe).fontFamily;
      });
    },
    /** Baseline offset from the top of a `lineH` line box, per CSS layout. */
    baseline(font: string, lineH: number): number {
      return Number(
        read(`b:${font}:${lineH}`, () => {
          ruler.style.font = font;
          ruler.style.lineHeight = `${lineH}px`;
          const top = ruler.getBoundingClientRect().top;
          return String(strut.getBoundingClientRect().bottom - top);
        })
      );
    },
    dispose() {
      probe.remove();
      ruler.remove();
    },
  };
}

interface Slot {
  x: number;
  w: number;
}

/**
 * CSS `flex: <grow> 1 0` with a `min-width` floor across a gapped row,
 * resolved the way the browser resolves flexible lengths (CSS Flexbox §9.7):
 * freeze what cannot grow, hand out the free space, freeze the items whose
 * share falls under the floor, repeat. When the floors no longer fit, every
 * item ends up frozen and the row is simply wider than `avail` — which is what
 * the DOM does too, so the caller clips it against the same box the
 * `overflow-hidden` run does.
 */
function layoutFlex(
  flexes: number[],
  avail: number,
  gap: number,
  minW: number
): Slot[] {
  const n = flexes.length;
  if (n === 0) return [];
  const space = avail - (n - 1) * gap;
  const widths = new Array<number>(n).fill(0);
  const frozen = new Array<boolean>(n).fill(false);
  // An item that cannot grow is frozen up front at its hypothetical main size,
  // which for a zero flex basis is whatever the floor lifts it to.
  let taken = 0;
  for (let i = 0; i < n; i++) {
    if (flexes[i] > 0) continue;
    widths[i] = minW;
    frozen[i] = true;
    taken += minW;
  }
  const initialFree = space - taken;
  for (;;) {
    let free = space - taken;
    let grow = 0;
    for (let i = 0; i < n; i++) if (!frozen[i]) grow += flexes[i];
    if (grow <= 0) break;
    // Flex factors summing to under 1 share out only that fraction of the free
    // space and leave the rest of the box empty — which is how a token run
    // holding one lightly-weighted step ends up narrower than the run itself.
    if (grow < 1) {
      const scaled = initialFree * grow;
      if (Math.abs(scaled) < Math.abs(free)) free = scaled;
    }
    let violation = false;
    for (let i = 0; i < n; i++) {
      if (frozen[i]) continue;
      widths[i] = (flexes[i] / grow) * free;
      if (widths[i] < minW) violation = true;
    }
    if (!violation) break;
    for (let i = 0; i < n; i++) {
      if (!frozen[i] && widths[i] < minW) {
        widths[i] = minW;
        frozen[i] = true;
        taken += minW;
      }
    }
  }

  // Blink quantises the running edge, not each width, so a row of equal items
  // comes out a 64th wider here and narrower there. Rounding widths instead
  // accumulates that 64th into whole pixels of drift by the end of a long row.
  const slots: Slot[] = [];
  let x = 0;
  for (const w of widths) {
    const x0 = Math.round(x / LAYOUT_UNIT) * LAYOUT_UNIT;
    const x1 = Math.round((x + w) / LAYOUT_UNIT) * LAYOUT_UNIT;
    slots.push({ x: x0, w: x1 - x0 });
    x += w + gap;
  }
  return slots;
}

/**
 * The run row. `overflow-hidden` zeroes a flex item's automatic minimum size,
 * so the runs themselves shrink to nothing and the 2px gaps are the row's only
 * fixed width — a band with more runs than the card has pixels overflows into
 * `overflow-x-auto` and leaves nothing but gap on screen. An image can neither
 * scroll nor show that, so past the halfway point the export shrinks the gap
 * and keeps the runs proportional. That is the one deliberate divergence from
 * the screen — everything else about the bands matches.
 */
function layoutRuns(
  weights: number[],
  avail: number,
  device: number
): Slot[] {
  const n = weights.length;
  let gap = BAND_GAP;
  if (n > 1 && (n - 1) * gap > avail / 2) {
    gap = avail / 2 / (n - 1);
    if (gap < device) gap = 0;
  }
  return layoutFlex(weights, avail, gap, 0);
}

/** Snap a coordinate to a paint grid (1 CSS px, or one device pixel). */
function pixelSnapper(unit: number) {
  return (v: number) => Math.round(v / unit) * unit;
}

function roundRect(
  ctx: CanvasRenderingContext2D,
  x: number,
  y: number,
  w: number,
  h: number,
  r: number
) {
  ctx.beginPath();
  ctx.roundRect(x, y, w, h, Math.max(0, Math.min(r, w / 2, h / 2)));
}

function fillRounded(
  ctx: CanvasRenderingContext2D,
  x: number,
  y: number,
  w: number,
  h: number,
  r: number,
  color: string
) {
  if (w <= 0 || h <= 0) return;
  ctx.fillStyle = color;
  roundRect(ctx, x, y, w, h, r);
  ctx.fill();
}

/** Text in a CSS line box of height `lineH`, on the browser's own baseline. */
function drawText(
  ctx: CanvasRenderingContext2D,
  probe: ReturnType<typeof styleProbe>,
  text: string,
  x: number,
  boxTop: number,
  lineH: number,
  align: CanvasTextAlign
) {
  ctx.textAlign = align;
  ctx.textBaseline = "alphabetic";
  ctx.fillText(text, x, boxTop + probe.baseline(ctx.font, lineH));
}

function ellipsize(
  ctx: CanvasRenderingContext2D,
  text: string,
  maxW: number
): string {
  if (ctx.measureText(text).width <= maxW) return text;
  let out = text;
  while (out.length > 1 && ctx.measureText(`${out}…`).width > maxW) {
    out = out.slice(0, -1);
  }
  return `${out}…`;
}

/** Greedy word wrap, as CSS wraps a paragraph of normal text. */
function wrapText(
  ctx: CanvasRenderingContext2D,
  text: string,
  maxW: number
): string[] {
  const lines: string[] = [];
  let line = "";
  for (const word of text.split(" ")) {
    const next = line ? `${line} ${word}` : word;
    if (line && ctx.measureText(next).width > maxW) {
      lines.push(line);
      line = word;
    } else {
      line = next;
    }
  }
  if (line) lines.push(line);
  return lines;
}

/** A lucide icon, drawn from its own path data at `size` CSS px. */
function drawIcon(
  ctx: CanvasRenderingContext2D,
  path: Path2D,
  x: number,
  y: number,
  size: number,
  stroke: string,
  fill?: string,
  shadow?: { color: string; blur: number; offsetY: number }
) {
  ctx.save();
  ctx.translate(x, y);
  ctx.scale(size / ICON_VIEWBOX, size / ICON_VIEWBOX);
  ctx.lineWidth = ICON_STROKE;
  ctx.lineJoin = "round";
  ctx.lineCap = "round";
  ctx.strokeStyle = stroke;
  if (shadow) {
    // One pass for the shadow only: shadowing the fill and the stroke
    // separately would print the second shadow across the shape's face.
    ctx.shadowColor = shadow.color;
    ctx.shadowBlur = shadow.blur;
    ctx.shadowOffsetY = shadow.offsetY;
    ctx.stroke(path);
    ctx.shadowColor = "transparent";
    ctx.shadowBlur = 0;
    ctx.shadowOffsetY = 0;
  }
  if (fill) {
    ctx.fillStyle = fill;
    ctx.fill(path);
  }
  ctx.stroke(path);
  ctx.restore();
}

function measureHeight(
  model: ActivityViewModel,
  border: number,
  noteLines: number
): number {
  let h = border + PAD + TITLE_ROW_H + TITLE_GAP;
  model.sections.forEach((section, i) => {
    if (i > 0) h += SECTION_GAP;
    const rows = section.rows.length;
    h +=
      ROW_H + SECTION_HEAD_GAP + rows * ROW_H + Math.max(0, rows - 1) * ROW_GAP;
  });
  h += SECTION_GAP + ROW_H + SECTION_HEAD_GAP + BAND_H;
  if (model.hasTokens) {
    h += HEAT_GAP + HEAT_H;
    h += TOKEN_BAND_GAP + ROW_H + SECTION_HEAD_GAP + BAND_H;
    h += CAPTION_GAP + CAPTION_LINE_H;
  }
  if (noteLines > 0) h += SECTION_GAP + noteLines * CAPTION_LINE_H;
  return h + PAD + border;
}

/**
 * Draw the Activity card onto a fresh canvas at `scale`× device pixels.
 * `host` is the live card element: its computed styles supply the surface,
 * border, text colors and loaded font stacks.
 */
function renderActivityCanvas(
  host: HTMLElement,
  model: ActivityViewModel,
  format: ActivityImageFormat,
  scale: number
): HTMLCanvasElement {
  const probe = styleProbe(host);
  // Canvas text is antialiased per the canvas element's own computed style, so
  // it has to sit inside the card (off-screen) to inherit `antialiased` and
  // land the same glyph weight the card shows. Attached here, not in the paint
  // pass, so `finally` still detaches it when painting throws.
  const canvas = document.createElement("canvas");
  canvas.style.cssText = "position:absolute;left:-9999px;top:0";
  host.appendChild(canvas);
  try {
    paintActivityCard(canvas, host, probe, model, format, scale);
    return canvas;
  } finally {
    canvas.remove();
    probe.dispose();
  }
}

function paintActivityCard(
  canvas: HTMLCanvasElement,
  host: HTMLElement,
  probe: ReturnType<typeof styleProbe>,
  model: ActivityViewModel,
  format: ActivityImageFormat,
  scale: number
): void {
  const hostStyle = getComputedStyle(host);
  const surface = hostStyle.backgroundColor;
  const borderColor = hostStyle.borderTopColor;
  const foreground = hostStyle.color;
  const border = parseFloat(hostStyle.borderTopWidth) || 0;
  const muted = probe.classColor("text-muted-foreground", "color");
  const track = probe.classColor("bg-muted/40", "backgroundColor");
  const sans = hostStyle.fontFamily;
  const mono = probe.classFont("font-mono");

  const width = Math.round(host.getBoundingClientRect().width);
  const left = border + PAD;
  const right = width - border - PAD;
  const contentW = right - left;

  const ctx = canvas.getContext("2d");
  if (!ctx) throw new Error("Canvas 2D is unavailable");

  // The excluded-steps note is the one line of prose on the card, so how many
  // lines it wraps to has to be measured before the canvas can be sized.
  ctx.font = `${CAPTION_FS}px ${mono}`;
  const noteLines = model.emptyNote
    ? wrapText(ctx, model.emptyNote, contentW)
    : [];

  // Fractional line heights leave the card half a CSS pixel tall; Blink paints
  // the border box snapped to whole CSS pixels, which pushes the bottom border
  // a device row down from where the layout box ends. Snap the same way.
  const height = Math.round(measureHeight(model, border, noteLines.length));
  canvas.width = Math.round(width * scale);
  canvas.height = Math.round(height * scale);
  ctx.scale(scale, scale);

  // JPEG has no alpha channel, so the rounded corners must land on an opaque
  // surface rather than compositing to black.
  if (format === "jpeg") {
    ctx.fillStyle = surface;
    ctx.fillRect(0, 0, width, height);
  }
  fillRounded(ctx, 0, 0, width, height, CARD_RADIUS, surface);
  if (border > 0) {
    ctx.strokeStyle = borderColor;
    ctx.lineWidth = border;
    roundRect(
      ctx,
      border / 2,
      border / 2,
      width - border,
      height - border,
      CARD_RADIUS - border / 2
    );
    ctx.stroke();
  }

  let y = border + PAD;

  // header — the export button beside the title is chrome, not content, and is
  // the one thing on the card the image deliberately leaves out
  drawIcon(
    ctx,
    new Path2D(ACTIVITY_PATH),
    left,
    y + (TITLE_ROW_H - ICON) / 2,
    ICON,
    foreground
  );
  ctx.fillStyle = foreground;
  ctx.font = `500 ${TITLE_FS}px ${sans}`;
  ctx.letterSpacing = `${TITLE_TRACKING * TITLE_FS}px`;
  drawText(
    ctx,
    probe,
    "Activity",
    left + ICON + ICON_GAP,
    y + (TITLE_ROW_H - TITLE_LINE_H) / 2,
    TITLE_LINE_H,
    "left"
  );
  ctx.letterSpacing = "0px";
  y += TITLE_ROW_H + TITLE_GAP;

  const trackX = left + LABEL_W + COL_GAP;
  const trackW = contentW - LABEL_W - VALUE_W - 2 * COL_GAP;

  const heading = (name: string, total: string, totalX: number) => {
    ctx.fillStyle = foreground;
    ctx.font = `500 12px ${sans}`;
    drawText(ctx, probe, name, left, y, ROW_H, "left");
    ctx.fillStyle = muted;
    ctx.font = `12px ${mono}`;
    drawText(ctx, probe, total, totalX, y, ROW_H, "right");
    y += ROW_H + SECTION_HEAD_GAP;
  };

  model.sections.forEach((section, i) => {
    if (i > 0) y += SECTION_GAP;
    heading(section.name, `${section.totalLabel} total`, right);

    for (const row of section.rows) {
      const mid = y + ROW_H / 2;
      const color = probe.color(row.color);
      fillRounded(
        ctx,
        left,
        mid - SWATCH_H / 2,
        SWATCH_W,
        SWATCH_H,
        RADIUS_SM,
        color
      );
      ctx.fillStyle = foreground;
      ctx.font = `12px ${sans}`;
      drawText(
        ctx,
        probe,
        ellipsize(ctx, row.label, LABEL_W - SWATCH_W - SWATCH_GAP),
        left + SWATCH_W + SWATCH_GAP,
        y,
        ROW_H,
        "left"
      );

      fillRounded(
        ctx,
        trackX,
        mid - TRACK_H / 2,
        trackW,
        TRACK_H,
        RADIUS_SM,
        track
      );
      ctx.save();
      roundRect(ctx, trackX, mid - TRACK_H / 2, trackW, TRACK_H, RADIUS_SM);
      ctx.clip();
      let barX = trackX;
      for (const bar of row.bars) {
        // Blink truncates a percentage width to 1/64 px and the next bar starts
        // from that quantised edge, so carrying the exact width forward instead
        // drifts the row by half a pixel within a dozen instances.
        const w = Math.max(
          BAR_MIN_W,
          Math.floor((bar.pct / 100) * trackW * 64) / 64
        );
        const x0 = Math.round(barX);
        fillRounded(
          ctx,
          x0,
          mid - TRACK_H / 2,
          Math.round(barX + w) - x0,
          TRACK_H,
          RADIUS_SM,
          color
        );
        barX += w + BAR_GAP;
      }
      ctx.restore();

      ctx.fillStyle = muted;
      ctx.font = `12px ${mono}`;
      drawText(ctx, probe, row.valueLabel, right, y, ROW_H, "right");
      y += ROW_H + ROW_GAP;
    }
    if (section.rows.length) y -= ROW_GAP;
  });

  y += SECTION_GAP;
  // The three bands share a `min-w-[560px]` column inside an `overflow-x-auto`
  // scroller, so on a card narrower than that they are laid out at 560 and only
  // their left edge is ever on screen. Clip to the same box the scroller shows.
  const bandW = Math.max(contentW, BAND_MIN_W);
  const device = 1 / scale;
  // Blink resolves layout in 1/64 px but paints boxes on whole CSS pixels, so
  // a sub-pixel run edge lands on the same grid line for both of us.
  const snap = pixelSnapper(1);
  ctx.save();
  ctx.beginPath();
  ctx.rect(left, y, contentW, height - y);
  ctx.clip();

  heading(STEP_BAND_TITLE, model.stepTotalLabel, left + bandW);

  // Every step in a run wears the run's color and the cells sit flush, so the
  // step band is the run box itself: one rounded rect per run, whatever the
  // cells inside it resolve to.
  const stepRuns = layoutRuns(
    model.runs.map((run) => run.stepWeight),
    bandW,
    device
  );
  const bandY = y;
  model.runs.forEach((run, r) => {
    const rx = left + stepRuns[r].x;
    const x0 = snap(rx);
    fillRounded(
      ctx,
      x0,
      bandY,
      snap(rx + stepRuns[r].w) - x0,
      BAND_H,
      RADIUS_SM,
      probe.color(run.color)
    );
  });

  const star = new Path2D(STAR_PATH);
  model.runs.forEach((run, r) => {
    if (!run.steps.some((step) => step.highlighted)) return;
    const rx = left + stepRuns[r].x;
    const cells = layoutFlex(
      run.steps.map(() => 1),
      stepRuns[r].w,
      0,
      STEP_MIN_W
    );
    ctx.save();
    roundRect(
      ctx,
      snap(rx),
      bandY,
      snap(rx + stepRuns[r].w) - snap(rx),
      BAND_H,
      RADIUS_SM
    );
    ctx.clip();
    run.steps.forEach((step, i) => {
      if (!step.highlighted) return;
      drawIcon(
        ctx,
        star,
        snap(rx + cells[i].x + cells[i].w) - STAR_INSET - STAR_SIZE,
        bandY + STAR_INSET,
        STAR_SIZE,
        "#ffffff",
        "#ffffff",
        // the icon's `drop-shadow`; canvas shadows ignore the transform, so
        // these are device pixels while everything else is CSS pixels
        { color: "rgba(0, 0, 0, 0.16)", blur: 2 * scale, offsetY: 1 * scale }
      );
    });
    ctx.restore();
  });
  y += BAND_H;

  if (model.hasTokens) {
    y += HEAT_GAP;
    // The heat band re-uses the step band's run widths but paints a color per
    // cell, so unlike the bands either side of it the cells have to be drawn
    // one by one — and clipped, because a run too narrow for its floors drops
    // its tail behind `overflow-hidden` on screen as well.
    const heatY = y;
    model.runs.forEach((run, r) => {
      const rx = left + stepRuns[r].x;
      const x0 = snap(rx);
      const x1 = snap(rx + stepRuns[r].w);
      if (x1 <= x0) return;
      const cells = layoutFlex(
        run.steps.map(() => 1),
        stepRuns[r].w,
        0,
        HEAT_MIN_W
      );
      ctx.save();
      roundRect(ctx, x0, heatY, x1 - x0, HEAT_H, RADIUS_SM);
      ctx.clip();
      // Square cells: only the run box is `rounded-sm`, and it is already
      // clipping them. Rounding each cell would scallop the band instead.
      run.steps.forEach((step, i) => {
        const c0 = snap(rx + cells[i].x);
        if (c0 >= x1) return;
        ctx.fillStyle = probe.color(step.heatColor);
        ctx.fillRect(c0, heatY, snap(rx + cells[i].x + cells[i].w) - c0, HEAT_H);
      });
      ctx.restore();
    });
    y += HEAT_H + TOKEN_BAND_GAP;

    heading(TOKEN_BAND_TITLE, model.tokenTotalLabel, left + bandW);

    const tokenRuns = layoutRuns(
      model.runs.map((run) => run.tokenWeight),
      bandW,
      device
    );
    const tokenY = y;
    model.runs.forEach((run, r) => {
      const rx = left + tokenRuns[r].x;
      const x0 = snap(rx);
      const x1 = snap(rx + tokenRuns[r].w);
      if (x1 <= x0) return;
      // Unlike the two bands above, a token run is not always filled by its
      // own cells: their flex factors are token shares, and shares summing to
      // under 1 leave the tail of the run box showing the card behind it.
      const cells = layoutFlex(
        run.steps.map((step) => step.tokenFlex),
        tokenRuns[r].w,
        0,
        STEP_MIN_W
      );
      const last = cells[cells.length - 1];
      const covered = Math.min(x1, snap(rx + last.x + last.w));
      ctx.save();
      roundRect(ctx, x0, tokenY, x1 - x0, BAND_H, RADIUS_SM);
      ctx.clip();
      ctx.fillStyle = probe.color(run.color);
      ctx.fillRect(x0, tokenY, covered - x0, BAND_H);
      ctx.restore();
    });
    y += BAND_H + CAPTION_GAP;

    ctx.fillStyle = muted;
    ctx.font = `${CAPTION_FS}px ${mono}`;
    drawText(ctx, probe, TIMELINE_CAPTION, left, y, CAPTION_LINE_H, "left");
    y += CAPTION_LINE_H;
  }
  ctx.restore();

  if (noteLines.length) {
    y += SECTION_GAP;
    ctx.fillStyle = muted;
    ctx.font = `${CAPTION_FS}px ${mono}`;
    for (const line of noteLines) {
      drawText(ctx, probe, line, left, y, CAPTION_LINE_H, "left");
      y += CAPTION_LINE_H;
    }
  }
}

function toBlob(canvas: HTMLCanvasElement, type: string): Promise<Blob> {
  return new Promise((resolve, reject) => {
    canvas.toBlob(
      (blob) =>
        blob ? resolve(blob) : reject(new Error(`Failed to encode ${type}`)),
      type,
      0.92
    );
  });
}

/**
 * Render the Activity card to a raster image and save it. Drawing the view
 * model onto a canvas — rather than rasterizing the DOM — keeps `oklch()`,
 * `color-mix()` and CSS variables out of the picture entirely: every color is
 * resolved to a concrete value by the browser before it reaches the canvas.
 */
export async function downloadActivityImage({
  host,
  model,
  trialId,
  format,
  scale = 2,
}: {
  host: HTMLElement;
  model: ActivityViewModel;
  trialId: string;
  format: ActivityImageFormat;
  scale?: number;
}): Promise<void> {
  await document.fonts?.ready;
  const canvas = renderActivityCanvas(host, model, format, scale);
  const blob = await toBlob(
    canvas,
    format === "png" ? "image/png" : "image/jpeg"
  );

  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = `activity-${trialId}.${format === "png" ? "png" : "jpg"}`;
  document.body.appendChild(link);
  link.click();
  link.remove();
  window.setTimeout(() => URL.revokeObjectURL(url), 0);
}
