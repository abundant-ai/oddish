"use client";

import { useEffect, useRef, useState } from "react";
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
const MIN_CHARGE_MS = 420;
const LANDED_HOLD_MS = 560;
const FADE_MS = 340;
const MAX_BALLS = 40;

type Pt = { x: number; y: number };
type BallPhase = "charging" | "dropping" | "landed" | "gone";
type Ball = {
  id: number;
  phase: BallPhase;
  pts: Pt[] | null;
  rp: number[] | null;
  bucket: number | null;
  step: number;
  result: QuotaGambleResult | null;
};
type LogEntry = { id: number; risk: Risk; mult: number; net: number; won: boolean };
type Notice = { text: string; tone: "bad" | "dim" };

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

const isBrutal = (r: QuotaGambleResult) =>
  !r.won && Math.abs(r.net_usd) >= 0.005 && r.multiplier <= 0.3;

export default function PlinkoGame() {
  const ctx = useCasino();
  const [risk, setRisk] = useState<Risk>("medium");
  const [wager, setWager] = useState("5.00");
  const [balls, setBalls] = useState<Ball[]>([]);
  const [log, setLog] = useState<LogEntry[]>([]);
  const [lastResult, setLastResult] = useState<QuotaGambleResult | null>(null);
  const [settleSeq, setSettleSeq] = useState(0);
  const [shaking, setShaking] = useState(false);
  const [notice, setNotice] = useState<Notice | null>(null);

  const alive = useRef(true);
  const timers = useRef<Set<ReturnType<typeof setTimeout>>>(new Set());
  const noticeTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const inFlightRef = useRef(0);
  const nextId = useRef(1);

  useEffect(() => {
    alive.current = true;
    const pool = timers.current;
    return () => {
      alive.current = false;
      pool.forEach(clearTimeout);
      pool.clear();
      if (noticeTimer.current) clearTimeout(noticeTimer.current);
    };
  }, []);

  const wait = (ms: number) =>
    new Promise<void>((resolve) => {
      const id = setTimeout(() => {
        timers.current.delete(id);
        resolve();
      }, ms);
      timers.current.add(id);
    });

  const flashNotice = (text: string, tone: Notice["tone"]) => {
    if (!alive.current) return;
    setNotice({ text, tone });
    if (noticeTimer.current) clearTimeout(noticeTimer.current);
    noticeTimer.current = setTimeout(() => {
      if (alive.current) setNotice(null);
    }, 1600);
  };

  const triggerShake = () => {
    if (!alive.current) return;
    setShaking(true);
    const id = setTimeout(() => {
      timers.current.delete(id);
      if (alive.current) setShaking(false);
    }, 430);
    timers.current.add(id);
  };

  const patch = (id: number, next: Partial<Ball>) =>
    setBalls((prev) => prev.map((b) => (b.id === id ? { ...b, ...next } : b)));

  const runBall = async (id: number, amount: number, ballRisk: Risk) => {
    const started = Date.now();
    let result: QuotaGambleResult;
    try {
      result = await ctx.play({ game: "plinko", wager_usd: amount.toFixed(2), risk: ballRisk });
    } catch (err) {
      inFlightRef.current = Math.max(0, inFlightRef.current - 1);
      if (!alive.current) return;
      setBalls((prev) => prev.filter((b) => b.id !== id));
      flashNotice(errorMessage(err), "bad");
      return;
    }
    if (!alive.current) return;

    const d = result.detail;
    const rawBucket = typeof d.bucket === "number" ? d.bucket : 6;
    const bucket = Math.max(0, Math.min(PLINKO_ROWS, Math.round(rawBucket)));
    let path = typeof d.path === "string" ? d.path : "";
    if (path.length !== PLINKO_ROWS) path = "R".repeat(bucket) + "L".repeat(PLINKO_ROWS - bucket);

    await wait(Math.max(0, MIN_CHARGE_MS - (Date.now() - started)));
    if (!alive.current) return;
    patch(id, {
      phase: "dropping",
      pts: buildPts(path, bucket),
      rp: rightsPrefix(path),
      bucket,
      step: 0,
      result,
    });

    for (let s = 1; s <= 12; s++) {
      patch(id, { step: s });
      ctx.audio.sfx(s === 12 ? "snap" : "bounce");
      await wait(s === 12 ? LAND_MS : HOP_MS);
      if (!alive.current) return;
    }

    inFlightRef.current = Math.max(0, inFlightRef.current - 1);
    await wait(120);
    if (!alive.current) return;
    patch(id, { phase: "landed" });

    const net = result.net_usd;
    ctx.audio.sfx(Math.abs(net) < 0.005 ? "push" : result.won ? (net >= 25 ? "bigwin" : "win") : "lose");
    setLastResult(result);
    setSettleSeq((n) => n + 1);
    setLog((l) =>
      [{ id, risk: ballRisk, mult: result.multiplier, net, won: result.won }, ...l].slice(0, 10),
    );
    if (isBrutal(result)) triggerShake();

    await wait(LANDED_HOLD_MS);
    if (!alive.current) return;
    patch(id, { phase: "gone" });
    await wait(FADE_MS);
    if (!alive.current) return;
    setBalls((prev) => prev.filter((b) => b.id !== id));
  };

  const dropBall = () => {
    ctx.audio.unlock();
    const amount = parseWager(wager, ctx.remaining);
    if (amount === null) return;
    if (inFlightRef.current >= MAX_BALLS) {
      ctx.audio.sfx("click");
      flashNotice("Let some land first…", "dim");
      return;
    }
    const id = nextId.current++;
    inFlightRef.current += 1;
    ctx.audio.sfx("whoosh");
    setBalls((prev) => [
      ...prev,
      { id, phase: "charging", pts: null, rp: null, bucket: null, step: 0, result: null },
    ]);
    void runBall(id, amount, risk);
  };

  const pickRisk = (r: Risk) => {
    ctx.audio.sfx("click");
    setRisk(r);
  };

  const amount = parseWager(wager, ctx.remaining);
  const buckets = PLINKO_TABLES[risk];
  const inFlight = balls.reduce(
    (n, b) => n + (b.phase === "charging" || b.phase === "dropping" ? 1 : 0),
    0,
  );

  const activePegs = new Set<string>();
  const activeBuckets = new Set<number>();
  for (const b of balls) {
    if (b.phase === "dropping" && b.rp && b.step >= 1 && b.step <= 11) {
      activePegs.add(`${b.step - 1}:${b.rp[b.step - 1]}`);
    }
    if (b.bucket !== null && b.step === 12 && (b.phase === "dropping" || b.phase === "landed")) {
      activeBuckets.add(b.bucket);
    }
  }

  const last = lastResult;
  const lastPush = last !== null && Math.abs(last.net_usd) < 0.005;

  return (
    <CinemaShell
      theme="plinko"
      title="PLINKO PEAK"
      tagline="Twelve rows of neon fate. Drop as many as you dare."
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
          <div className={`plk-panel w-full max-w-[560px] ${shaking ? "cz-shake" : ""}`}>
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
                  const hit = activePegs.has(`${r}:${j}`);
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
                const landed = activeBuckets.has(i);
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

              {balls.map((b) => {
                const pos = b.phase === "charging" || !b.pts ? APEX : b.pts[b.step];
                const dur = b.phase === "dropping" ? (b.step === 12 ? LAND_MS : HOP_MS) : 0;
                return (
                  <g
                    key={b.id}
                    className="plk-ballwrap"
                    style={{
                      transform: `translate(${pos.x}px, ${pos.y}px)`,
                      transitionDuration: `${dur}ms`,
                    }}
                  >
                    <g
                      style={{
                        opacity: b.phase === "gone" ? 0 : 1,
                        transition: `opacity ${FADE_MS}ms ease-out`,
                      }}
                    >
                      <g className={b.phase === "charging" ? "plk-pulse" : undefined}>
                        <circle r={27} fill="url(#plkHalo)" />
                        <circle r={13} fill="url(#plkBall)" />
                        <circle cx={-4} cy={-5} r={3.4} fill="rgba(255,255,255,0.85)" />
                      </g>
                    </g>
                  </g>
                );
              })}
            </svg>

            {last?.won ? (
              <div
                key={settleSeq}
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

        <div className="flex min-h-[3.25rem] flex-col items-center justify-center gap-1 text-center">
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
              {inFlight > 0 ? "The board rains fate…" : "Pick your risk. Feed the board."}
            </div>
          )}
          {inFlight > 0 ? (
            <div className="cz-display text-xs" style={{ color: "var(--cz-accent2)" }}>
              {inFlight} in flight
            </div>
          ) : null}
        </div>
        {notice ? (
          <div
            className="text-center text-sm"
            style={{ color: notice.tone === "bad" ? "#f87171" : "var(--cz-dim)" }}
          >
            {notice.text}
          </div>
        ) : null}

        <div className="flex gap-3">
          {RISKS.map((r) => (
            <button
              key={r}
              className={`cz-btn ${risk === r ? "cz-btn-primary" : ""}`}
              onClick={() => pickRisk(r)}
            >
              {r.toUpperCase()} · {RISK_MAX[r]}
            </button>
          ))}
        </div>

        <WagerPanel value={wager} onChange={setWager} />

        <button className="cz-btn cz-btn-primary" disabled={amount === null} onClick={dropBall}>
          {amount === null ? "ENTER A WAGER" : `DROP FOR ${formatDollars(amount)}`}
        </button>

        <div className="text-center text-[0.65rem] tracking-widest" style={{ color: "var(--cz-dim)" }}>
          HOUSE ENGINE PAYTABLE · 12 ROWS · CENTER HITS MOST · EVERY DROP SETTLED SERVER-SIDE
        </div>
      </div>
    </CinemaShell>
  );
}
