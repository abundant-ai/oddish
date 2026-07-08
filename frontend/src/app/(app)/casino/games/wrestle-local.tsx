"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import type { WrestleResult } from "@/lib/types";
import {
  CinemaShell,
  WagerPanel,
  errorMessage,
  formatDollars,
  parseWager,
  useCasino,
  type SfxName,
} from "../casino-kit";

const VB_W = 1000;
const VB_H = 600;
const FLOOR = 560;
const CEIL = 24;
const WALL_L = 30;
const WALL_R = 970;
const CX = 500;
const HEAD_R = 26;
const HIP_R = 30;
const SPINE = 90;
const SPREAD = 118;
const CLASP_Y = 372;
const GROUND = FLOOR - HIP_R;
const HIP_X_B = CX - SPREAD;
const HIP_X_R = CX + SPREAD;
const ARM = Math.hypot(SPREAD, GROUND - CLASP_Y);

const G = 0.17;
const DAMP = 0.99;
const MAX_V = 46;
const JUMP = 8.4;
const NUDGE = 2.6;
const JUMP_CD = 26;
const RECOVER = 0.16;
const VEL_BLEED = 0.25;
const BOUNCE = 0.42;
const STEP_MS = 1000 / 120;
const WIN_TARGET = 3;

type Pt = { x: number; y: number; px: number; py: number };
type Key = "Pb" | "Hb" | "Pr" | "Hr" | "C";
type Pts = Record<Key, Pt>;
type Mode = "idle" | "countdown" | "fight" | "roundover" | "matchover";
type Side = "b" | "r";

type Engine = {
  pts: Pts;
  cd: { b: number; r: number };
  jumpReq: { b: boolean; r: boolean };
  mode: Mode;
  countT: number;
  countLabel: string;
  freezeT: number;
  roundWinner: Side | null;
  koKey: Key | null;
  scoreB: number;
  scoreR: number;
  round: number;
  flashSeq: number;
  shakeSeq: number;
};

type View = {
  pts: Pts;
  mode: Mode;
  countLabel: string;
  scoreB: number;
  scoreR: number;
  round: number;
  roundWinner: Side | null;
  koKey: Key | null;
  flashSeq: number;
  shakeSeq: number;
};

const clamp = (v: number, a: number, b: number) => (v < a ? a : v > b ? b : v);
const mk = (x: number, y: number): Pt => ({ x, y, px: x, py: y });
const cp = (p: Pt): Pt => ({ x: p.x, y: p.y, px: p.px, py: p.py });

function restPts(): Pts {
  return {
    Pb: mk(HIP_X_B, GROUND),
    Hb: mk(HIP_X_B, GROUND - SPINE),
    Pr: mk(HIP_X_R, GROUND),
    Hr: mk(HIP_X_R, GROUND - SPINE),
    C: mk(CX, CLASP_Y),
  };
}

function freshEngine(): Engine {
  return {
    pts: restPts(),
    cd: { b: 0, r: 0 },
    jumpReq: { b: false, r: false },
    mode: "idle",
    countT: 0,
    countLabel: "",
    freezeT: 0,
    roundWinner: null,
    koKey: null,
    scoreB: 0,
    scoreR: 0,
    round: 1,
    flashSeq: 0,
    shakeSeq: 0,
  };
}

function snapshot(e: Engine): View {
  return {
    pts: {
      Pb: cp(e.pts.Pb),
      Hb: cp(e.pts.Hb),
      Pr: cp(e.pts.Pr),
      Hr: cp(e.pts.Hr),
      C: cp(e.pts.C),
    },
    mode: e.mode,
    countLabel: e.countLabel,
    scoreB: e.scoreB,
    scoreR: e.scoreR,
    round: e.round,
    roundWinner: e.roundWinner,
    koKey: e.koKey,
    flashSeq: e.flashSeq,
    shakeSeq: e.shakeSeq,
  };
}

function integrate(p: Pt, g: number) {
  const vx = clamp((p.x - p.px) * DAMP, -MAX_V, MAX_V);
  const vy = clamp((p.y - p.py) * DAMP, -MAX_V, MAX_V);
  p.px = p.x;
  p.py = p.y;
  p.x += vx;
  p.y += vy + g;
}

function solve(a: Pt, b: Pt, rest: number) {
  const dx = b.x - a.x;
  const dy = b.y - a.y;
  const d = Math.hypot(dx, dy) || 1e-4;
  const err = clamp(d - rest, -rest * 0.8, rest * 0.8);
  const k = (err / d) * 0.5;
  const ox = dx * k;
  const oy = dy * k;
  a.x += ox;
  a.y += oy;
  b.x -= ox;
  b.y -= oy;
}

function collide(p: Pt, r: number) {
  const minX = WALL_L + r;
  const maxX = WALL_R - r;
  const minY = CEIL + r;
  const maxY = FLOOR - r;
  if (p.x < minX) {
    p.x = minX;
    p.px = minX + (minX - p.px) * BOUNCE;
  } else if (p.x > maxX) {
    p.x = maxX;
    p.px = maxX + (maxX - p.px) * BOUNCE;
  }
  if (p.y < minY) {
    p.y = minY;
    p.py = minY + (minY - p.py) * BOUNCE;
  } else if (p.y > maxY) {
    p.y = maxY;
    p.py = maxY + (maxY - p.py) * BOUNCE;
  }
}

function recover(hip: Pt, head: Pt) {
  if (hip.y < GROUND - 10) return;
  head.x += (hip.x - head.x) * RECOVER;
  head.y += (hip.y - SPINE - head.y) * RECOVER;
  head.px += (head.x - head.px) * VEL_BLEED;
  head.py += (head.y - head.py) * VEL_BLEED;
}

async function postWrestle(wager: number, won: boolean): Promise<WrestleResult> {
  const res = await fetch("/api/quotas/gamble/wrestle", {
    method: "POST",
    credentials: "include",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ wager_usd: wager.toFixed(2), won }),
  });
  let data: Record<string, unknown> = {};
  try {
    data = (await res.json()) as Record<string, unknown>;
  } catch {
    data = {};
  }
  if (!res.ok) {
    const detail = typeof data.detail === "string" ? data.detail : undefined;
    const error = typeof data.error === "string" ? data.error : undefined;
    throw new Error(detail ?? error ?? "Bet failed");
  }
  return data as unknown as WrestleResult;
}

function Grappler({
  hip,
  head,
  clasp,
  fill,
  edge,
  dark,
  face,
  dead,
}: {
  hip: Pt;
  head: Pt;
  clasp: Pt;
  fill: string;
  edge: string;
  dark: string;
  face: "blue" | "red";
  dead: boolean;
}) {
  const legY = Math.min(hip.y + 30, FLOOR - 2);
  const look = face === "blue" ? 7 : -7;
  return (
    <g style={{ filter: "drop-shadow(0 8px 8px rgba(0,0,0,0.45))" }}>
      <line
        x1={hip.x}
        y1={hip.y}
        x2={clasp.x}
        y2={clasp.y}
        stroke={edge}
        strokeWidth={11}
        strokeLinecap="round"
        opacity={0.92}
      />
      <line x1={hip.x - 8} y1={hip.y} x2={hip.x - 8} y2={legY} stroke={dark} strokeWidth={11} strokeLinecap="round" />
      <line x1={hip.x + 8} y1={hip.y} x2={hip.x + 8} y2={legY} stroke={dark} strokeWidth={11} strokeLinecap="round" />
      <line x1={hip.x} y1={hip.y} x2={head.x} y2={head.y} stroke={edge} strokeWidth={13} strokeLinecap="round" />
      <circle cx={hip.x} cy={hip.y} r={HIP_R} fill={fill} stroke={edge} strokeWidth={3} />
      <circle cx={head.x} cy={head.y} r={HEAD_R} fill={fill} stroke={edge} strokeWidth={3} />
      {dead ? (
        <text
          x={head.x}
          y={head.y + 6}
          textAnchor="middle"
          fontSize={20}
          fontWeight={900}
          fill="#0f172a"
          style={{ fontFamily: "var(--cz-font-display)" }}
        >
          ✕ ✕
        </text>
      ) : (
        <>
          <circle cx={head.x + look - 6} cy={head.y - 4} r={3.6} fill="#0f172a" />
          <circle cx={head.x + look + 6} cy={head.y - 4} r={3.6} fill="#0f172a" />
          <path
            d={`M ${head.x + look - 7} ${head.y + 9} Q ${head.x + look} ${head.y + 14} ${head.x + look + 7} ${head.y + 9}`}
            stroke="#0f172a"
            strokeWidth={2.4}
            fill="none"
            strokeLinecap="round"
          />
        </>
      )}
    </g>
  );
}

function Pips({ score, color }: { score: number; color: string }) {
  return (
    <span className="flex gap-1">
      {[0, 1, 2].map((i) => (
        <span
          key={i}
          className="wl-pip"
          style={{ borderColor: color, background: i < score ? color : "transparent" }}
        />
      ))}
    </span>
  );
}

export default function WrestleLocal({ onSwitchToDuel }: { onSwitchToDuel: () => void }) {
  const ctx = useCasino();
  const eng = useRef<Engine>(freshEngine());
  const [view, setView] = useState<View>(() => snapshot(eng.current));
  const [wager, setWager] = useState("5.00");
  const [result, setResult] = useState<WrestleResult | null>(null);
  const [error, setError] = useState<string | null>(null);

  const alive = useRef(true);
  const settledRef = useRef(false);
  const matchWagerRef = useRef<number | null>(null);
  const sfxRef = useRef(ctx.audio.sfx);
  sfxRef.current = ctx.audio.sfx;

  const amount = parseWager(wager, ctx.remaining);

  const settleMatch = useCallback(
    async (blueWon: boolean) => {
      const w = matchWagerRef.current;
      if (w === null) return;
      try {
        const res = await postWrestle(w, blueWon);
        if (!alive.current) return;
        ctx.recordNet(res.net_usd);
        ctx.refresh();
        setResult(res);
        ctx.audio.sfx(blueWon ? (res.net_usd >= 25 ? "bigwin" : "win") : "lose");
      } catch (err) {
        if (!alive.current) return;
        setError(errorMessage(err));
        ctx.audio.sfx(blueWon ? "win" : "lose");
      }
    },
    [ctx],
  );

  useEffect(() => {
    alive.current = true;
    let raf = 0;
    let last = performance.now();
    let acc = 0;

    const sfx = (n: SfxName) => sfxRef.current(n);

    const jump = (e: Engine, side: Side) => {
      const hip = side === "b" ? e.pts.Pb : e.pts.Pr;
      if (e.cd[side] > 0 || hip.y < GROUND - 10) return;
      hip.py = hip.y + JUMP;
      const dir = side === "b" ? 1 : -1;
      hip.px = hip.x - dir * NUDGE;
      e.cd[side] = JUMP_CD;
      sfx("whoosh");
    };

    const endRound = (e: Engine, winner: Side) => {
      e.mode = "roundover";
      e.freezeT = 1150;
      e.roundWinner = winner;
      e.koKey = winner === "b" ? "Hr" : "Hb";
      e.shakeSeq += 1;
      e.flashSeq += 1;
      sfx("snap");
      sfx("explode");
    };

    const substep = (e: Engine) => {
      const { Pb, Hb, Pr, Hr, C } = e.pts;
      if (e.mode === "fight") {
        if (e.jumpReq.b) {
          jump(e, "b");
          e.jumpReq.b = false;
        }
        if (e.jumpReq.r) {
          jump(e, "r");
          e.jumpReq.r = false;
        }
      } else {
        e.jumpReq.b = false;
        e.jumpReq.r = false;
      }
      e.cd.b = Math.max(0, e.cd.b - 1);
      e.cd.r = Math.max(0, e.cd.r - 1);
      integrate(Pb, G);
      integrate(Hb, G);
      integrate(Pr, G);
      integrate(Hr, G);
      integrate(C, 0);
      for (let i = 0; i < 6; i++) {
        solve(Pb, Hb, SPINE);
        solve(Pr, Hr, SPINE);
        solve(Pb, C, ARM);
        solve(Pr, C, ARM);
        collide(Pb, HIP_R);
        collide(Pr, HIP_R);
        collide(Hb, HEAD_R);
        collide(Hr, HEAD_R);
        collide(C, 12);
      }
      recover(Pb, Hb);
      recover(Pr, Hr);
      if (e.mode === "fight") {
        if (Hb.y >= FLOOR - HEAD_R - 0.6 && Hb.y > Pb.y) endRound(e, "r");
        else if (Hr.y >= FLOOR - HEAD_R - 0.6 && Hr.y > Pr.y) endRound(e, "b");
      }
    };

    const advance = (e: Engine, dt: number) => {
      if (e.mode === "countdown") {
        e.countT -= dt;
        const label = e.countT > 2600 ? "3" : e.countT > 1600 ? "2" : e.countT > 600 ? "1" : "CLASH!";
        if (label !== e.countLabel) {
          e.countLabel = label;
          sfx(label === "CLASH!" ? "bounce" : "tick");
        }
        if (e.countT <= 0) {
          e.mode = "fight";
          e.countLabel = "";
        }
      } else if (e.mode === "roundover") {
        e.freezeT -= dt;
        if (e.freezeT <= 0) {
          if (e.roundWinner === "b") e.scoreB += 1;
          else e.scoreR += 1;
          if (e.scoreB >= WIN_TARGET || e.scoreR >= WIN_TARGET) {
            e.mode = "matchover";
          } else {
            e.round += 1;
            e.pts = restPts();
            e.cd.b = 0;
            e.cd.r = 0;
            e.roundWinner = null;
            e.koKey = null;
            e.mode = "countdown";
            e.countT = 3600;
            e.countLabel = "";
          }
        }
      }
    };

    const frame = (now: number) => {
      if (!alive.current) return;
      const e = eng.current;
      const dt = Math.min(now - last, 50);
      last = now;
      acc += dt;
      let steps = 0;
      while (acc >= STEP_MS && steps < 6) {
        if (e.mode === "idle" || e.mode === "countdown" || e.mode === "fight") substep(e);
        acc -= STEP_MS;
        steps += 1;
      }
      advance(e, dt);
      setView(snapshot(e));
      raf = requestAnimationFrame(frame);
    };
    raf = requestAnimationFrame(frame);

    const onDown = (ev: KeyboardEvent) => {
      if (ev.repeat) return;
      const e = eng.current;
      if (e.mode !== "fight") return;
      if (ev.key.toLowerCase() === "w") {
        ev.preventDefault();
        e.jumpReq.b = true;
      } else if (ev.key === ",") {
        ev.preventDefault();
        e.jumpReq.r = true;
      }
    };
    window.addEventListener("keydown", onDown);

    return () => {
      alive.current = false;
      cancelAnimationFrame(raf);
      window.removeEventListener("keydown", onDown);
    };
  }, []);

  useEffect(() => {
    if (view.mode === "matchover" && !settledRef.current) {
      settledRef.current = true;
      void settleMatch(view.scoreB >= WIN_TARGET);
    }
  }, [view.mode, view.scoreB, settleMatch]);

  const startMatch = () => {
    if (amount === null) return;
    ctx.audio.unlock();
    ctx.audio.sfx("cashout");
    setResult(null);
    setError(null);
    settledRef.current = false;
    matchWagerRef.current = amount;
    const e = eng.current;
    e.pts = restPts();
    e.cd.b = 0;
    e.cd.r = 0;
    e.jumpReq.b = false;
    e.jumpReq.r = false;
    e.roundWinner = null;
    e.koKey = null;
    e.scoreB = 0;
    e.scoreR = 0;
    e.round = 1;
    e.freezeT = 0;
    e.mode = "countdown";
    e.countT = 3600;
    e.countLabel = "";
  };

  const runItBack = () => {
    ctx.audio.sfx("click");
    setResult(null);
    setError(null);
    settledRef.current = false;
    const e = eng.current;
    e.pts = restPts();
    e.roundWinner = null;
    e.koKey = null;
    e.scoreB = 0;
    e.scoreR = 0;
    e.round = 1;
    e.mode = "idle";
    e.countLabel = "";
  };

  const v = view;
  const showKO = v.mode === "roundover" || v.mode === "matchover";
  const blueDead = showKO && v.koKey === "Hb";
  const redDead = showKO && v.koKey === "Hr";
  const blueWon = v.scoreB >= WIN_TARGET;
  const matchWager = matchWagerRef.current ?? 0;
  const shakeRing = v.mode === "roundover";

  return (
    <CinemaShell
      theme="wrestle"
      title="SPRING SLAM — LIVE"
      tagline="Hot-seat, one keyboard, two spines. Best of five for your own budget."
      hudExtra={
        <button
          className="cz-btn wl-hud-btn"
          onClick={() => {
            ctx.audio.sfx("click");
            onSwitchToDuel();
          }}
        >
          CHALLENGE A COWORKER
        </button>
      }
    >
      <style>{`
        .wl-stage {
          position: relative; border-radius: 18px; overflow: hidden;
          width: min(760px, 94vw);
          border: 1px solid color-mix(in srgb, var(--cz-accent2) 35%, transparent);
          box-shadow: inset 0 0 70px rgba(0,0,0,0.6), 0 10px 40px rgba(0,0,0,0.4);
          background:
            radial-gradient(600px 260px at 50% 0%, rgba(250,204,21,0.10), transparent 70%),
            linear-gradient(180deg, #0a0e24, #141b40);
        }
        .wl-overlay {
          position: absolute; inset: 0; z-index: 20; display: flex; flex-direction: column;
          align-items: center; justify-content: center; gap: 0.6rem; text-align: center; padding: 1rem;
        }
        .wl-overlay-solid { background: rgba(4,6,22,0.66); }
        .wl-count {
          font-family: var(--cz-font-display); font-weight: 900; line-height: 1;
          font-size: clamp(4rem, 16vw, 8rem); color: var(--cz-text);
          text-shadow: 0 0 30px var(--cz-accent2), 0 6px 0 rgba(0,0,0,0.5);
        }
        .wl-clash { color: #facc15; text-shadow: 0 0 34px rgba(250,204,21,0.9), 0 6px 0 #92400e; }
        .wl-ko {
          font-family: var(--cz-font-display); font-weight: 900; line-height: 1;
          font-size: clamp(3.5rem, 13vw, 6.5rem); color: #facc15;
          text-shadow: 0 0 26px rgba(250,204,21,0.9), 0 6px 0 #92400e, 0 10px 30px rgba(0,0,0,0.8);
        }
        .wl-koside { font-family: var(--cz-font-display); font-size: 0.95rem; letter-spacing: 0.12em; }
        .wl-legend { font-family: var(--cz-font-display); letter-spacing: 0.14em; font-size: 0.82rem; color: var(--cz-dim); text-align: center; }
        .wl-corner { font-family: var(--cz-font-display); font-size: 0.72rem; letter-spacing: 0.08em; }
        .wl-pip { display: inline-block; width: 12px; height: 12px; border-radius: 50%; border: 2px solid; }
        .wl-link { font-size: 0.72rem; padding: 0.3rem 0.7rem; }
        .wl-hud-btn { font-size: 0.72rem; padding: 0.35rem 0.7rem; }
      `}</style>

      <div className="mx-auto flex min-h-full max-w-4xl flex-col items-center justify-center gap-4 px-4 py-6">
        <div className="flex w-full max-w-[760px] items-center justify-between px-2">
          <div className="flex items-center gap-2">
            <span className="wl-corner" style={{ color: "#93c5fd" }}>
              YOU (W)
            </span>
            <Pips score={v.scoreB} color="#60a5fa" />
          </div>
          <span className="cz-display text-xs" style={{ color: "var(--cz-dim)" }}>
            ROUND {v.round} · BEST OF 5
          </span>
          <div className="flex items-center gap-2">
            <Pips score={v.scoreR} color="#f87171" />
            <span className="wl-corner" style={{ color: "#fca5a5" }}>
              RIVAL (,)
            </span>
          </div>
        </div>

        <div className={`wl-stage ${shakeRing ? "cz-shake" : ""}`}>
          <svg viewBox={`0 0 ${VB_W} ${VB_H}`} className="block h-auto w-full" role="img" aria-label="Wrestling ring">
            <defs>
              <radialGradient id="wlBlue" cx="38%" cy="30%">
                <stop offset="0%" stopColor="#dbeafe" />
                <stop offset="70%" stopColor="#2563eb" />
                <stop offset="100%" stopColor="#1e3a8a" />
              </radialGradient>
              <radialGradient id="wlRed" cx="62%" cy="30%">
                <stop offset="0%" stopColor="#fee2e2" />
                <stop offset="70%" stopColor="#dc2626" />
                <stop offset="100%" stopColor="#7f1d1d" />
              </radialGradient>
              <linearGradient id="wlMat" x1="0" y1="0" x2="0" y2="1">
                <stop offset="0%" stopColor="#2b3470" />
                <stop offset="100%" stopColor="#141b40" />
              </linearGradient>
            </defs>

            {[0, 1, 2].map((i) => (
              <line
                key={i}
                x1={WALL_L}
                y1={150 + i * 60}
                x2={WALL_R}
                y2={150 + i * 60}
                stroke="url(#wlMat)"
                strokeWidth={4}
                opacity={0.55}
              />
            ))}
            {[0, 1, 2].map((i) => (
              <line
                key={`r${i}`}
                x1={WALL_L}
                y1={150 + i * 60}
                x2={WALL_R}
                y2={150 + i * 60}
                stroke={i === 1 ? "#facc15" : "#93c5fd"}
                strokeWidth={2}
                opacity={0.5}
              />
            ))}

            <rect x={20} y={FLOOR} width={960} height={VB_H - FLOOR} fill="url(#wlMat)" />
            <line x1={20} y1={FLOOR} x2={980} y2={FLOOR} stroke="#cbd5e1" strokeWidth={4} />
            {[...Array(13)].map((_, i) => (
              <line
                key={`c${i}`}
                x1={40 + i * 76}
                y1={FLOOR + 4}
                x2={40 + i * 76}
                y2={VB_H}
                stroke="#0b1233"
                strokeWidth={2}
                opacity={0.5}
              />
            ))}

            <rect x={WALL_L - 9} y={126} width={18} height={FLOOR - 126} rx={9} fill="#60a5fa" opacity={0.9} />
            <rect x={WALL_R - 9} y={126} width={18} height={FLOOR - 126} rx={9} fill="#f87171" opacity={0.9} />
            <circle cx={WALL_L} cy={124} r={12} fill="#bfdbfe" />
            <circle cx={WALL_R} cy={124} r={12} fill="#fecaca" />

            <Grappler
              hip={v.pts.Pb}
              head={v.pts.Hb}
              clasp={v.pts.C}
              fill="url(#wlBlue)"
              edge="#60a5fa"
              dark="#1d4ed8"
              face="blue"
              dead={blueDead}
            />
            <Grappler
              hip={v.pts.Pr}
              head={v.pts.Hr}
              clasp={v.pts.C}
              fill="url(#wlRed)"
              edge="#f87171"
              dark="#b91c1c"
              face="red"
              dead={redDead}
            />
            <circle cx={v.pts.C.x} cy={v.pts.C.y} r={11} fill="#facc15" stroke="#fff7cd" strokeWidth={2.5} />
            <circle cx={v.pts.C.x - 3} cy={v.pts.C.y - 3} r={3} fill="#fffbeb" />
          </svg>

          {v.mode === "countdown" && v.countLabel ? (
            <div className="wl-overlay">
              <div key={v.countLabel} className={`wl-count cz-pop ${v.countLabel === "CLASH!" ? "wl-clash" : ""}`}>
                {v.countLabel}
              </div>
            </div>
          ) : null}

          {v.mode === "roundover" ? (
            <>
              <div key={v.flashSeq} className="cz-flash" style={{ background: "rgba(250,204,21,0.55)" }} />
              <div className="wl-overlay">
                <div className="wl-ko cz-pop">SLAM!</div>
                <div
                  className="wl-koside"
                  style={{ color: v.roundWinner === "b" ? "#93c5fd" : "#fca5a5" }}
                >
                  {v.roundWinner === "b" ? "BLUE PINS A FALL" : "RED PINS A FALL"}
                </div>
              </div>
            </>
          ) : null}

          {v.mode === "idle" && !result ? (
            <div className="wl-overlay wl-overlay-solid">
              <div className="cz-display cz-glow text-2xl font-bold" style={{ color: "var(--cz-accent)" }}>
                SPRING SLAM — LIVE
              </div>
              <div className="text-xs" style={{ color: "var(--cz-dim)" }}>
                Local hot-seat — whoever&rsquo;s at the keyboard decides the winner.
              </div>
              <div className="cz-display text-sm" style={{ color: "var(--cz-accent2)" }}>
                BEST OF FIVE · FIRST TO 3 FALLS · GET THEIR HEAD ON THE MAT
              </div>
              <WagerPanel value={wager} onChange={setWager} />
              <button className="cz-btn cz-btn-primary" disabled={amount === null} onClick={startMatch}>
                {amount === null ? "ENTER A WAGER" : `STAKE ${formatDollars(amount)} · START MATCH`}
              </button>
              <div className="wl-legend">W = BLUE jumps · , = RED jumps</div>
              <button
                className="cz-btn wl-link"
                onClick={() => {
                  ctx.audio.sfx("click");
                  onSwitchToDuel();
                }}
              >
                challenge a coworker instead →
              </button>
              {error ? <div className="text-sm text-red-400">{error}</div> : null}
            </div>
          ) : null}

          {v.mode === "matchover" ? (
            <div className="wl-overlay wl-overlay-solid">
              {blueWon ? (
                <div key={v.flashSeq} className="cz-flash" style={{ background: "rgba(96,165,250,0.5)" }} />
              ) : null}
              <div
                className={`cz-display text-center text-2xl font-bold ${blueWon ? "cz-glow cz-pop" : "cz-shake"}`}
                style={{ color: blueWon ? "var(--cz-accent2)" : "#f87171" }}
              >
                {blueWon
                  ? `+${formatDollars(result?.net_usd ?? matchWager)} — RIPPED FROM THE HOUSE`
                  : `${formatDollars(result?.net_usd ?? -matchWager)} — THE MAT CLAIMS YOU`}
              </div>
              <div className="cz-display text-sm" style={{ color: "var(--cz-dim)" }}>
                FINAL {v.scoreB}–{v.scoreR}
              </div>
              {error ? <div className="text-sm text-red-400">Bet not recorded: {error}</div> : null}
              <div className="mt-1 flex gap-3">
                <button className="cz-btn cz-btn-primary" onClick={runItBack}>
                  RUN IT BACK
                </button>
                <button
                  className="cz-btn"
                  onClick={() => {
                    ctx.audio.sfx("click");
                    ctx.exit();
                  }}
                >
                  BACK
                </button>
              </div>
            </div>
          ) : null}
        </div>

        <div className="wl-legend">W = BLUE jumps · , = RED jumps — spring together, slam their head to the canvas</div>
        {v.mode === "fight" ? (
          <div className="cz-display text-sm" style={{ color: "var(--cz-dim)" }}>
            {formatDollars(matchWager)} ON THE LINE — FIRST HEAD DOWN LOSES THE FALL
          </div>
        ) : null}
      </div>
    </CinemaShell>
  );
}
