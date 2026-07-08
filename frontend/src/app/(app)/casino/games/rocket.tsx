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

const TARGET_CHIPS = [1.5, 2, 3, 5, 10];
const MIN_TARGET = 1.01;
const MAX_TARGET = 100;
const IGNITION_MS = 1300;
const GREEN = "#4ade80";
const RED = "#f87171";

type Phase = "idle" | "ignition" | "flight" | "won" | "lost";

type RoundSpec = { target: number; crash: number; won: boolean; net: number };

type Debris = { id: number; dx: number; dy: number; delay: number; color: string };

const clamp01 = (x: number) => Math.min(1, Math.max(0, x));
const flightMs = (crash: number) =>
  2500 + 1500 * clamp01(Math.log(Math.max(crash, 1.001)) / Math.log(40));
const apexVh = (crash: number) =>
  6 + 28 * clamp01(Math.log(Math.max(crash, 1.001)) / Math.log(15));

const parseTarget = (s: string): number | null => {
  const t = Number.parseFloat(s);
  if (!Number.isFinite(t)) return null;
  const r = Math.round(t * 100) / 100;
  return r < MIN_TARGET || r > MAX_TARGET ? null : r;
};

const wait = (ms: number) => new Promise((r) => setTimeout(r, ms));

export default function RocketGame() {
  const ctx = useCasino();
  const [wager, setWager] = useState("5.00");
  const [targetStr, setTargetStr] = useState("2.00");
  const [phase, setPhase] = useState<Phase>("idle");
  const [round, setRound] = useState<RoundSpec | null>(null);
  const [value, setValue] = useState(1);
  const [progress, setProgress] = useState(0);
  const [stamped, setStamped] = useState(false);
  const [debris, setDebris] = useState<Debris[]>([]);
  const [flash, setFlash] = useState<{ id: number; bg: string } | null>(null);
  const [history, setHistory] = useState<number[]>([]);
  const [error, setError] = useState<string | null>(null);

  const aliveRef = useRef(true);
  const flightTimerRef = useRef<number | null>(null);
  const loseTimerRef = useRef<number | null>(null);
  const stampedRef = useRef(false);
  const lastTickRef = useRef(1);
  const flashIdRef = useRef(0);

  useEffect(() => {
    aliveRef.current = true;
    return () => {
      aliveRef.current = false;
      if (flightTimerRef.current !== null) window.clearInterval(flightTimerRef.current);
      if (loseTimerRef.current !== null) window.clearTimeout(loseTimerRef.current);
    };
  }, []);

  const amount = parseWager(wager, ctx.remaining);
  const target = parseTarget(targetStr);
  const live = phase === "ignition" || phase === "flight";

  const pulseFlash = (bg: string) => {
    flashIdRef.current += 1;
    setFlash({ id: flashIdRef.current, bg });
  };

  const settle = (spec: RoundSpec) => {
    setHistory((h) => [spec.crash, ...h].slice(0, 32));
    if (spec.won) {
      if (!stampedRef.current) {
        stampedRef.current = true;
        setStamped(true);
        ctx.audio.sfx("cashout");
      }
      setPhase("won");
      pulseFlash("radial-gradient(circle at 50% 45%, rgba(74,222,128,0.45), transparent 65%)");
      ctx.audio.sfx(spec.net >= 25 ? "bigwin" : "win");
    } else {
      setPhase("lost");
      setDebris(
        Array.from({ length: 14 }, (_, i) => ({
          id: i,
          dx: (Math.random() * 2 - 1) * 150,
          dy: (Math.random() * 2 - 1) * 120 - 40,
          delay: Math.random() * 90,
          color: i % 3 === 0 ? "var(--cz-accent2)" : "var(--cz-accent)",
        })),
      );
      pulseFlash("radial-gradient(circle at 50% 55%, rgba(249,115,22,0.65), transparent 70%)");
      ctx.audio.sfx("explode");
      loseTimerRef.current = window.setTimeout(() => ctx.audio.sfx("lose"), 650);
    }
  };

  const fly = (spec: RoundSpec) => {
    if (!aliveRef.current) return;
    setRound(spec);
    setPhase("flight");
    lastTickRef.current = 1;
    ctx.audio.sfx("launch");
    const dur = flightMs(spec.crash);
    const t0 = performance.now();
    flightTimerRef.current = window.setInterval(() => {
      const p = Math.min(1, (performance.now() - t0) / dur);
      const v = Math.min(spec.crash, Math.pow(spec.crash, p));
      const step = v < 3 ? 0.25 : v < 10 ? 1 : 10;
      if (v - lastTickRef.current >= step) {
        lastTickRef.current = v;
        ctx.audio.sfx("tick");
      }
      if (spec.won && !stampedRef.current && v >= spec.target) {
        stampedRef.current = true;
        setStamped(true);
        ctx.audio.sfx("cashout");
        pulseFlash("radial-gradient(circle at 50% 40%, rgba(74,222,128,0.3), transparent 60%)");
      }
      setProgress(p);
      setValue(v);
      if (p >= 1) {
        if (flightTimerRef.current !== null) window.clearInterval(flightTimerRef.current);
        flightTimerRef.current = null;
        settle(spec);
      }
    }, 90);
  };

  const launch = async () => {
    if (live || amount === null || target === null) return;
    setPhase("ignition");
    setError(null);
    setRound(null);
    setValue(1);
    setProgress(0);
    setStamped(false);
    setDebris([]);
    stampedRef.current = false;
    ctx.audio.sfx("snap");
    const started = Date.now();
    let result: QuotaGambleResult;
    try {
      result = await ctx.play({
        game: "rocket",
        wager_usd: amount.toFixed(2),
        target: target.toFixed(2),
      });
    } catch (err) {
      setError(errorMessage(err));
      setPhase("idle");
      return;
    }
    const d = result.detail as { target?: unknown; crash?: unknown };
    const tgt = Number.parseFloat(String(d.target ?? target.toFixed(2)));
    const crashRaw = Number.parseFloat(String(d.crash ?? "1"));
    const spec: RoundSpec = {
      target: Number.isFinite(tgt) ? tgt : target,
      crash: Number.isFinite(crashRaw) ? crashRaw : 1,
      won: result.won,
      net: result.net_usd,
    };
    await wait(Math.max(0, IGNITION_MS - (Date.now() - started)));
    fly(spec);
  };

  const apex = round ? apexVh(round.crash) : 0;
  const rocketY = -(progress * apex);

  return (
    <CinemaShell
      theme="rocket"
      title="ESCAPE VELOCITY"
      tagline="Set your exit before ignition. The void decides everything after."
    >
      <style>{`
        .rk-stars, .rk-streaks { position: absolute; left: 0; right: 0; top: 0; pointer-events: none; will-change: transform; }
        .rk-stars {
          height: 1560px;
          background-image:
            radial-gradient(1px 1px at 22px 34px, rgba(249,250,251,0.9), transparent 55%),
            radial-gradient(1px 1px at 118px 156px, rgba(129,140,248,0.85), transparent 55%),
            radial-gradient(1.5px 1.5px at 201px 90px, rgba(249,250,251,0.7), transparent 55%),
            radial-gradient(1px 1px at 64px 214px, rgba(251,146,60,0.7), transparent 55%),
            radial-gradient(1px 1px at 175px 245px, rgba(249,250,251,0.5), transparent 55%),
            radial-gradient(1px 1px at 240px 180px, rgba(129,140,248,0.5), transparent 55%);
          background-size: 260px 260px;
          animation: rk-fall 80s linear infinite;
          opacity: 0.8;
        }
        @keyframes rk-fall { from { transform: translateY(-780px); } to { transform: translateY(0); } }
        .rk-streaks {
          height: 1440px;
          background-image:
            radial-gradient(1px 26px at 40px 60px, rgba(129,140,248,0.7), transparent 60%),
            radial-gradient(1px 32px at 150px 140px, rgba(249,250,251,0.5), transparent 60%),
            radial-gradient(1px 22px at 220px 40px, rgba(251,146,60,0.6), transparent 60%),
            radial-gradient(1px 30px at 90px 200px, rgba(129,140,248,0.5), transparent 60%);
          background-size: 240px 240px;
          animation: rk-fall-fast 0.9s linear infinite;
          opacity: 0;
          transition: opacity 500ms;
        }
        .rk-streaks.on { opacity: 1; }
        @keyframes rk-fall-fast { from { transform: translateY(-720px); } to { transform: translateY(0); } }
        .rk-readout { font-family: var(--cz-font-display); font-weight: 700; font-variant-numeric: tabular-nums; line-height: 1; letter-spacing: 0.02em; }
        .rk-rocket { position: absolute; bottom: 11%; left: 50%; z-index: 10; font-size: 2.6rem; transition: transform 110ms linear; will-change: transform; }
        .rk-rocket.gone { opacity: 0.25; transition: opacity 700ms, transform 110ms linear; }
        .rk-ship { display: inline-block; transform: rotate(-45deg); }
        .rk-ship.rumble { animation: rk-rumble 90ms linear infinite; }
        @keyframes rk-rumble {
          0%, 100% { transform: translate(-1px, 0) rotate(-45deg); }
          50% { transform: translate(1px, 1px) rotate(-45deg); }
        }
        .rk-flame {
          position: absolute; left: 50%; top: 96%; width: 10px; height: 28px;
          border-radius: 50% 50% 50% 50% / 30% 30% 70% 70%;
          background: linear-gradient(#fef3c7, #fb923c 40%, rgba(251,146,60,0));
          filter: blur(1px); transform-origin: top;
          animation: rk-flick 110ms linear infinite alternate;
        }
        @keyframes rk-flick {
          from { transform: translateX(-50%) scaleY(0.7); opacity: 0.8; }
          to { transform: translateX(-50%) scaleY(1.3); opacity: 1; }
        }
        .rk-boom { position: absolute; bottom: 11%; left: 50%; z-index: 10; }
        .rk-boom-core { display: inline-block; font-size: 4rem; transform: translateX(-50%); }
        .rk-debris { position: absolute; left: 0; top: 40%; font-size: 0.7rem; animation: rk-debris 900ms ease-out forwards; }
        @keyframes rk-debris {
          from { transform: translate(0, 0); opacity: 1; }
          to { transform: translate(var(--dx), var(--dy)); opacity: 0; }
        }
        .rk-stamp {
          display: inline-block; border: 2px solid ${GREEN}; color: ${GREEN};
          padding: 0.25rem 0.9rem; border-radius: 6px; transform: rotate(-6deg);
          font-family: var(--cz-font-display); letter-spacing: 0.14em;
          text-shadow: 0 0 14px rgba(74,222,128,0.7); background: rgba(74,222,128,0.08);
        }
        .rk-tape {
          overflow: hidden; white-space: nowrap; padding: 0.35rem 0;
          border-bottom: 1px solid color-mix(in srgb, var(--cz-accent2) 25%, transparent);
          font-family: var(--cz-font-display); font-size: 0.72rem; letter-spacing: 0.08em;
        }
        .rk-tape-track { display: inline-flex; animation: rk-tape 28s linear infinite; will-change: transform; }
        .rk-tape-copy { display: inline-flex; gap: 1.6rem; padding-right: 1.6rem; }
        @keyframes rk-tape { from { transform: translateX(0); } to { transform: translateX(-50%); } }
      `}</style>

      <div className="flex min-h-full flex-col">
        <div className="rk-tape">
          {history.length > 0 ? (
            <div className="rk-tape-track">
              {[0, 1].map((copy) => (
                <span key={copy} className="rk-tape-copy" aria-hidden={copy === 1}>
                  {history.map((c, i) => (
                    <span
                      key={`${copy}-${i}`}
                      style={{ color: c >= 10 ? "var(--cz-accent)" : c >= 2 ? GREEN : RED }}
                    >
                      {c >= 10 ? "▲▲" : c >= 2 ? "▲" : "▼"} {c.toFixed(2)}x
                    </span>
                  ))}
                </span>
              ))}
            </div>
          ) : (
            <div className="px-4 text-center" style={{ color: "var(--cz-dim)" }}>
              CRASH TELEMETRY — NO PRIOR FLIGHTS THIS SESSION
            </div>
          )}
        </div>

        <div
          className={`relative min-h-[300px] flex-1 overflow-hidden ${phase === "lost" ? "cz-shake" : ""}`}
        >
          <div className="rk-stars" />
          <div className={`rk-streaks ${phase === "flight" ? "on" : ""}`} />

          <div className="absolute inset-x-0 top-[9%] z-10 flex flex-col items-center gap-3 px-4 text-center">
            {stamped && round ? (
              <>
                <div
                  className="rk-readout cz-glow text-[clamp(3rem,9vw,5.2rem)]"
                  style={{ color: GREEN }}
                >
                  {round.target.toFixed(2)}x
                </div>
                <div className="rk-stamp cz-pop text-sm">
                  AUTO-CASHOUT @ {round.target.toFixed(2)}x
                </div>
                <div className="text-xs" style={{ color: "var(--cz-dim)" }}>
                  {phase === "flight"
                    ? `hull still climbing… ${value.toFixed(2)}x`
                    : `SIGNAL LOST @ ${round.crash.toFixed(2)}x — you were already gone.`}
                </div>
              </>
            ) : (
              <div
                className={`rk-readout text-[clamp(3rem,9vw,5.2rem)] ${phase === "flight" ? "cz-glow" : ""}`}
                style={{
                  color:
                    phase === "lost" ? RED : phase === "flight" ? "var(--cz-accent)" : "var(--cz-text)",
                }}
              >
                {value.toFixed(2)}x
              </div>
            )}

            {phase === "idle" && !round ? (
              <div className="cz-display text-sm" style={{ color: "var(--cz-dim)" }}>
                AWAITING LAUNCH ORDER — set target, set stake, commit.
              </div>
            ) : null}
            {phase === "ignition" ? (
              <div className="cz-display animate-pulse text-sm" style={{ color: "var(--cz-accent2)" }}>
                IGNITION SEQUENCE ENGAGED — TELEMETRY LOCKED
              </div>
            ) : null}
            {phase === "flight" && round && !stamped ? (
              <div className="cz-display text-sm" style={{ color: "var(--cz-accent2)" }}>
                AUTO-CASHOUT ARMED @ {round.target.toFixed(2)}x
              </div>
            ) : null}
            {phase === "won" && round ? (
              <div className="cz-pop cz-display text-2xl font-bold" style={{ color: GREEN }}>
                PAYLOAD SECURED +{formatDollars(round.net)}
              </div>
            ) : null}
            {phase === "lost" && round ? (
              <>
                <div className="cz-pop cz-display text-2xl font-bold" style={{ color: RED }}>
                  TOTAL HULL LOSS — {formatDollars(round.net)}
                </div>
                <div className="text-xs" style={{ color: "var(--cz-dim)" }}>
                  Exit was set at {round.target.toFixed(2)}x. The hull gave out at{" "}
                  {round.crash.toFixed(2)}x. The void keeps the difference.
                </div>
              </>
            ) : null}
          </div>

          {phase !== "lost" ? (
            <div
              className={`rk-rocket ${phase === "won" ? "gone" : ""}`}
              style={{ transform: `translate(-50%, ${rocketY.toFixed(2)}vh)` }}
            >
              <span className={`rk-ship ${phase === "ignition" ? "rumble" : ""}`}>🚀</span>
              {phase === "ignition" || phase === "flight" ? <span className="rk-flame" /> : null}
            </div>
          ) : (
            <div className="rk-boom" style={{ transform: `translate(0, ${-apex.toFixed(2)}vh)` }}>
              <span className="rk-boom-core cz-pop">💥</span>
              {debris.map((d) => (
                <span
                  key={d.id}
                  className="rk-debris"
                  style={
                    {
                      "--dx": `${d.dx.toFixed(0)}px`,
                      "--dy": `${d.dy.toFixed(0)}px`,
                      animationDelay: `${d.delay.toFixed(0)}ms`,
                      color: d.color,
                    } as React.CSSProperties
                  }
                >
                  ▪
                </span>
              ))}
            </div>
          )}

          <div className="absolute inset-x-0 bottom-[7%] z-0 flex flex-col items-center gap-1">
            <div
              style={{
                width: "min(320px, 60%)",
                height: "2px",
                background: "linear-gradient(90deg, transparent, var(--cz-accent), transparent)",
                boxShadow: "0 0 18px var(--cz-accent)",
              }}
            />
            <div className="text-[10px] tracking-[0.3em]" style={{ color: "var(--cz-dim)" }}>
              LC-39A · ODDISH RANGE
            </div>
          </div>

          {flash ? (
            <div key={flash.id} className="cz-flash z-20" style={{ background: flash.bg }} />
          ) : null}
        </div>

        <div
          className="relative z-10 mx-auto flex w-full max-w-3xl flex-col items-center gap-3 px-4 pb-6 pt-4"
          style={{ borderTop: "1px solid color-mix(in srgb, var(--cz-accent2) 20%, transparent)" }}
        >
          <div className="flex flex-wrap items-center justify-center gap-2">
            <span className="cz-display text-xs" style={{ color: "var(--cz-dim)" }}>
              TARGET
            </span>
            {TARGET_CHIPS.map((c) => (
              <button
                key={c}
                className={`cz-btn ${target === c ? "cz-btn-primary" : ""}`}
                disabled={live}
                onClick={() => {
                  ctx.audio.sfx("click");
                  setTargetStr(c.toFixed(2));
                }}
              >
                {c}x
              </button>
            ))}
            <input
              className="cz-input"
              value={targetStr}
              inputMode="decimal"
              aria-label="Target multiplier"
              disabled={live}
              onChange={(e) => setTargetStr(e.target.value)}
            />
          </div>

          <div className="text-xs" style={{ color: "var(--cz-dim)" }}>
            {target !== null
              ? `P(reach ${target.toFixed(2)}x) = 99% / ${target.toFixed(2)} = ${(99 / target).toFixed(2)}% · pays ${target.toFixed(2)}x${
                  amount !== null ? ` · profit +${formatDollars(amount * (target - 1))}` : ""
                }`
              : `TARGET MUST BE ${MIN_TARGET}x – ${MAX_TARGET}x`}
          </div>

          <WagerPanel value={wager} onChange={setWager} disabled={live} />

          <button
            className="cz-btn cz-btn-primary"
            disabled={live || amount === null || target === null}
            onClick={launch}
          >
            {live
              ? phase === "ignition"
                ? "IGNITION…"
                : "IN FLIGHT…"
              : amount === null
                ? "ENTER A WAGER"
                : target === null
                  ? "SET A VALID TARGET"
                  : `LAUNCH — ${formatDollars(amount)} @ ${target.toFixed(2)}x`}
          </button>

          {error ? <div className="text-center text-sm text-red-400">{error}</div> : null}
        </div>
      </div>
    </CinemaShell>
  );
}
