"use client";

import { useState } from "react";
import type { QuotaGambleResult } from "@/lib/types";
import {
  CinemaShell,
  WagerPanel,
  errorMessage,
  formatDollars,
  parseWager,
  useCasino,
} from "../casino-kit";

type Risk = "low" | "medium" | "high";

const PLINKO_ROWS = 12;
const PLINKO_TABLES: Record<Risk, string[]> = {
  low: ["10", "3", "1.6", "1.4", "1.1", "1", "0.5", "1", "1.1", "1.4", "1.6", "3", "10"],
  medium: ["33", "11", "4", "2", "1.1", "0.6", "0.3", "0.6", "1.1", "2", "4", "11", "33"],
  high: ["170", "24", "8.1", "2", "0.7", "0.2", "0.2", "0.2", "0.7", "2", "8.1", "24", "170"],
};
const RISKS: Risk[] = ["low", "medium", "high"];
const RISK_MAX: Record<Risk, string> = { low: "10×", medium: "33×", high: "170×" };

const VB_W = 1000;
const VB_H = 700;
const U = VB_W / 13;
const Y0 = 70;
const ROW_H = 44;
const BUCKET_TOP = 600;
const BUCKET_H = 86;
const APEX = { x: 500, y: Y0 - 40 };

const HOP_MS = 165;
const LAND_MS = 230;
const MIN_CHARGE_MS = 700;

type Pt = { x: number; y: number };
type Phase = "idle" | "charging" | "dropping" | "landed";
type LogEntry = { id: number; risk: Risk; mult: number; net: number; won: boolean };

const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));

function bucketColor(i: number): string {
  const t = Math.pow(Math.abs(i - 6) / 6, 1.5);
  const cold = [34, 211, 238];
  const hot = [232, 121, 249];
  let rgb = cold.map((c, k) => Math.round(c + (hot[k] - c) * t));
  if (Math.abs(i - 6) === 6) rgb = rgb.map((c) => Math.round(c + (255 - c) * 0.35));
  return `rgb(${rgb[0]},${rgb[1]},${rgb[2]})`;
}

function rightsPrefix(path: string): number[] {
  const rp = [0];
  let r = 0;
  for (const c of path) rp.push((r += c === "R" ? 1 : 0));
  return rp;
}

function buildPts(path: string, bucket: number): Pt[] {
  const pts: Pt[] = [{ ...APEX }];
  let rights = 0;
  for (let k = 0; k <= 10; k++) {
    if (path[k] === "R") rights++;
    pts.push({
      x: 500 + (rights - (k + 1) / 2) * U + (Math.random() - 0.5) * U * 0.34,
      y: Y0 + k * ROW_H + ROW_H * 0.55,
    });
  }
  pts.push({ x: U * (bucket + 0.5), y: BUCKET_TOP + BUCKET_H * 0.38 });
  return pts;
}

export default function PlinkoGame() {
  const ctx = useCasino();
  const [risk, setRisk] = useState<Risk>("medium");
  const [wager, setWager] = useState("5.00");
  const [phase, setPhase] = useState<Phase>("idle");
  const [drop, setDrop] = useState<{ pts: Pt[]; rp: number[]; bucket: number; result: QuotaGambleResult } | null>(null);
  const [step, setStep] = useState(0);
  const [round, setRound] = useState(0);
  const [error, setError] = useState<string | null>(null);
  const [log, setLog] = useState<LogEntry[]>([]);

  const amount = parseWager(wager, ctx.remaining);
  const live = phase === "charging" || phase === "dropping";
  const buckets = PLINKO_TABLES[risk];
  const last = phase === "landed" && drop ? drop.result : null;
  const lastPush = last !== null && Math.abs(last.net_usd) < 0.005;
  const brutal = last !== null && !last.won && !lastPush && last.multiplier <= 0.3;

  const pickRisk = (r: Risk) => {
    ctx.audio.sfx("click");
    setRisk(r);
    if (phase === "landed") {
      setPhase("idle");
      setDrop(null);
    }
  };

  const dropBall = async () => {
    if (live || amount === null) return;
    setPhase("charging");
    setError(null);
    setDrop(null);
    setStep(0);
    setRound((n) => n + 1);
    ctx.audio.sfx("whoosh");
    const started = Date.now();
    try {
      const result = await ctx.play({ game: "plinko", wager_usd: amount.toFixed(2), risk });
      const d = result.detail as Record<string, unknown>;
      const rawBucket = typeof d.bucket === "number" ? d.bucket : 6;
      const bucket = Math.max(0, Math.min(PLINKO_ROWS, Math.round(rawBucket)));
      let path = typeof d.path === "string" ? d.path : "";
      if (path.length !== PLINKO_ROWS) path = "R".repeat(bucket) + "L".repeat(PLINKO_ROWS - bucket);
      await sleep(Math.max(0, MIN_CHARGE_MS - (Date.now() - started)));
      setDrop({ pts: buildPts(path, bucket), rp: rightsPrefix(path), bucket, result });
      setPhase("dropping");
      for (let s = 1; s <= 12; s++) {
        setStep(s);
        ctx.audio.sfx(s === 12 ? "snap" : "bounce");
        await sleep(s === 12 ? LAND_MS : HOP_MS);
      }
      await sleep(150);
      setPhase("landed");
      const net = result.net_usd;
      ctx.audio.sfx(Math.abs(net) < 0.005 ? "push" : result.won ? (net >= 25 ? "bigwin" : "win") : "lose");
      setLog((l) =>
        [{ id: Date.now(), risk, mult: result.multiplier, net, won: result.won }, ...l].slice(0, 7),
      );
    } catch (err) {
      setError(errorMessage(err));
      setPhase("idle");
    }
  };

  const ballPos: Pt | null =
    phase === "charging" ? APEX : drop && phase !== "idle" ? drop.pts[step] : null;
  const hitPeg =
    drop && phase === "dropping" && step >= 1 && step <= 11
      ? { row: step - 1, j: drop.rp[step - 1] }
      : null;

  return (
    <CinemaShell
      theme="plinko"
      title="PLINKO PEAK"
      tagline="Twelve rows of neon fate. Gravity settles all debts."
    >
      <style>{`
        .plk-panel {
          position: relative; border-radius: 20px; padding: 10px 12px 6px;
          background: rgba(8,4,18,0.55); overflow: hidden;
          border: 1px solid color-mix(in srgb, var(--cz-accent) 30%, transparent);
          box-shadow: inset 0 0 60px rgba(217,70,239,0.10), 0 0 30px rgba(34,211,238,0.08);
        }
        .plk-ballwrap { transition-property: transform; transition-timing-function: cubic-bezier(0.5, 0.05, 0.65, 1); will-change: transform; }
        @keyframes plk-hover { from { transform: translateY(-4px); } to { transform: translateY(5px); } }
        .plk-pulse { animation: plk-hover 650ms ease-in-out infinite alternate; }
        @keyframes plk-hit { from { fill-opacity: 0.75; } to { fill-opacity: 0.12; } }
        .plk-hit { animation: plk-hit 850ms ease-out infinite alternate; }
        @keyframes plk-rail { to { stroke-dashoffset: -48; } }
        .plk-rail { stroke-dasharray: 7 9; animation: plk-rail 2.4s linear infinite; }
        .plk-log { min-width: 10.5rem; max-width: 12rem; border-radius: 16px; padding: 0.75rem 0.9rem;
          background: rgba(8,4,18,0.45); border: 1px solid color-mix(in srgb, var(--cz-accent2) 28%, transparent); }
      `}</style>

      <div className="mx-auto flex min-h-full max-w-5xl flex-col items-center justify-center gap-4 px-4 py-6">
        <div className="flex w-full flex-wrap items-stretch justify-center gap-5">
          <div className={`plk-panel w-full max-w-[560px] ${brutal ? "cz-shake" : ""}`}>
            <svg viewBox={`0 0 ${VB_W} ${VB_H}`} className="block h-auto w-full" role="img" aria-label="Plinko board">
              <defs>
                <radialGradient id="plkBall" cx="35%" cy="30%">
                  <stop offset="0%" stopColor="#ffffff" />
                  <stop offset="45%" stopColor="#e879f9" />
                  <stop offset="100%" stopColor="#86198f" />
                </radialGradient>
                <radialGradient id="plkHalo">
                  <stop offset="0%" stopColor="rgba(232,121,249,0.75)" />
                  <stop offset="100%" stopColor="rgba(232,121,249,0)" />
                </radialGradient>
              </defs>

              <polyline
                className="plk-rail"
                points={`${500 - U},${Y0 - 14} 4,${BUCKET_TOP - 10}`}
                fill="none" stroke="rgba(34,211,238,0.4)" strokeWidth={2.5}
              />
              <polyline
                className="plk-rail"
                points={`${500 + U},${Y0 - 14} ${VB_W - 4},${BUCKET_TOP - 10}`}
                fill="none" stroke="rgba(34,211,238,0.4)" strokeWidth={2.5}
              />

              {Array.from({ length: PLINKO_ROWS }, (_, r) =>
                Array.from({ length: r + 1 }, (_, j) => {
                  const hit = hitPeg !== null && hitPeg.row === r && hitPeg.j === j;
                  return (
                    <circle
                      key={`${r}-${j}`}
                      cx={500 + (j - r / 2) * U}
                      cy={Y0 + r * ROW_H}
                      r={hit ? 9 : 5}
                      fill={hit ? "#22d3ee" : "rgba(253,244,255,0.5)"}
                      opacity={hit ? 1 : 0.85}
                    />
                  );
                }),
              )}

              {buckets.map((m, i) => {
                const c = bucketColor(i);
                const landed = drop !== null && step === 12 && drop.bucket === i && phase !== "idle";
                return (
                  <g key={`${risk}-${i}`}>
                    <rect
                      x={i * U + 2.5} y={BUCKET_TOP} width={U - 5} height={BUCKET_H} rx={9}
                      fill={c} fillOpacity={0.1} stroke={c} strokeOpacity={0.55} strokeWidth={1.5}
                    />
                    {landed ? (
                      <rect className="plk-hit" x={i * U + 2.5} y={BUCKET_TOP} width={U - 5} height={BUCKET_H} rx={9} fill={c} />
                    ) : null}
                    <text
                      x={i * U + U / 2} y={BUCKET_TOP + 58} textAnchor="middle"
                      fill={c} fontSize={20} fontWeight={700}
                      style={{ fontFamily: "var(--cz-font-display)" }}
                    >
                      {m}×
                    </text>
                  </g>
                );
              })}

              {ballPos ? (
                <g
                  className="plk-ballwrap"
                  style={{
                    transform: `translate(${ballPos.x}px, ${ballPos.y}px)`,
                    transitionDuration: phase === "dropping" ? (step === 12 ? `${LAND_MS}ms` : `${HOP_MS}ms`) : "0ms",
                  }}
                >
                  <g className={phase === "charging" ? "plk-pulse" : undefined}>
                    <circle r={27} fill="url(#plkHalo)" />
                    <circle r={13} fill="url(#plkBall)" />
                    <circle cx={-4} cy={-5} r={3.4} fill="rgba(255,255,255,0.85)" />
                  </g>
                </g>
              ) : null}
            </svg>

            {last?.won ? (
              <div
                key={round}
                className="cz-flash"
                style={{
                  background:
                    last.net_usd >= 25
                      ? "radial-gradient(circle, rgba(255,255,255,0.85), rgba(232,121,249,0.5) 60%, transparent)"
                      : "radial-gradient(circle, rgba(232,121,249,0.45), transparent 70%)",
                }}
              />
            ) : null}
          </div>

          <aside className="plk-log hidden flex-col gap-1.5 sm:flex">
            <div className="cz-display text-xs font-bold" style={{ color: "var(--cz-accent2)" }}>
              LAST DROPS
            </div>
            {log.length === 0 ? (
              <div className="text-xs" style={{ color: "var(--cz-dim)" }}>
                The board remembers nothing. Yet.
              </div>
            ) : (
              log.map((e) => (
                <div key={e.id} className="flex items-center justify-between gap-2 text-xs">
                  <span style={{ color: "var(--cz-dim)" }}>{e.risk.toUpperCase()}</span>
                  <span className="cz-display" style={{ color: "var(--cz-text)" }}>{e.mult}×</span>
                  <span
                    className="cz-display font-bold"
                    style={{ color: e.net > 0 ? "var(--cz-accent)" : e.net < 0 ? "#f87171" : "var(--cz-dim)" }}
                  >
                    {e.net >= 0 ? "+" : ""}{formatDollars(e.net)}
                  </span>
                </div>
              ))
            )}
          </aside>
        </div>

        <div className="flex min-h-[3.25rem] items-center justify-center text-center">
          {last ? (
            <div
              className={`cz-pop cz-display text-xl font-bold ${last.won ? "cz-glow" : ""}`}
              style={{ color: last.won ? "var(--cz-accent)" : lastPush ? "var(--cz-dim)" : "#f87171" }}
            >
              {lastPush
                ? "1× DEAD EVEN — the board blinks, unimpressed."
                : last.won
                  ? last.net_usd >= 25
                    ? `${last.multiplier}× — THE PEAK ERUPTS. +${formatDollars(last.net_usd)}!`
                    : `${last.multiplier}× — +${formatDollars(last.net_usd)} rides your 24h line.`
                  : `${last.multiplier}× — ${formatDollars(last.net_usd)} swallowed by the cold center.`}
            </div>
          ) : (
            <div className="cz-display text-lg" style={{ color: "var(--cz-dim)" }}>
              {phase === "charging"
                ? "The ball hangs at the peak…"
                : phase === "dropping"
                  ? "Twelve rows of fate…"
                  : "Pick your risk. Feed the board."}
            </div>
          )}
        </div>
        {error ? <div className="text-center text-sm text-red-400">{error}</div> : null}

        <div className="flex gap-3">
          {RISKS.map((r) => (
            <button
              key={r}
              className={`cz-btn ${risk === r ? "cz-btn-primary" : ""}`}
              disabled={live}
              onClick={() => pickRisk(r)}
            >
              {r.toUpperCase()} · {RISK_MAX[r]}
            </button>
          ))}
        </div>

        <WagerPanel value={wager} onChange={setWager} disabled={live} />

        <button className="cz-btn cz-btn-primary" disabled={live || amount === null} onClick={dropBall}>
          {phase === "charging"
            ? "SUMMONING…"
            : phase === "dropping"
              ? "FALLING…"
              : amount === null
                ? "ENTER A WAGER"
                : `DROP FOR ${formatDollars(amount)}`}
        </button>

        <div className="text-center text-[0.65rem] tracking-widest" style={{ color: "var(--cz-dim)" }}>
          HOUSE ENGINE PAYTABLE · 12 ROWS · CENTER HITS MOST · EVERY DROP SETTLED SERVER-SIDE
        </div>
      </div>
    </CinemaShell>
  );
}
