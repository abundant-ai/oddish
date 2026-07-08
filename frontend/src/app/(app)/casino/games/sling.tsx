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

// Mirror of backend/api/casino_games.py: 0.98 / 0.75^k, 2dp, rungs 1..10.
const SLING_LADDER = [1.31, 1.74, 2.32, 3.1, 4.13, 5.51, 7.34, 9.79, 13.05, 17.4];
const MAX_RUNG = 10;

const RUNG_GAP = 60;
const WALL_W = 300;
const WALL_H = 760;
const GROUND = { x: 0, y: 26 };
const CAM_ANCHOR = 170;
const LEAP_MS = 330;
const TUMBLE_MS = 1000;

const rungX = (k: number) => (k % 2 ? -74 : 74);
const rungY = (k: number) => 96 + (k - 1) * RUNG_GAP;
const multAt = (k: number) => SLING_LADDER[k - 1] ?? 0;
const sleep = (ms: number) => new Promise<void>((r) => setTimeout(r, ms));

type MonkeyMode =
  | "idle"
  | "crouch"
  | "windup"
  | "leap"
  | "grab"
  | "miss"
  | "tumble"
  | "splat"
  | "celebrate";

const LEAVES = [
  { x: -134, y: 60, s: 22, o: 0.5, e: "🌿" },
  { x: 130, y: 148, s: 26, o: 0.4, e: "🍃" },
  { x: -126, y: 268, s: 20, o: 0.45, e: "🍃" },
  { x: 134, y: 342, s: 22, o: 0.5, e: "🌿" },
  { x: -132, y: 452, s: 26, o: 0.4, e: "🌿" },
  { x: 128, y: 548, s: 20, o: 0.45, e: "🍃" },
  { x: -124, y: 646, s: 24, o: 0.5, e: "🍃" },
];

export default function SlingGame() {
  const ctx = useCasino();
  const [rung, setRung] = useState(4);
  const [wager, setWager] = useState("5.00");
  const [busy, setBusy] = useState(false);
  const [mmode, setMmode] = useState<MonkeyMode>("idle");
  const [mpos, setMpos] = useState(GROUND);
  const [grabbed, setGrabbed] = useState(0);
  const [planted, setPlanted] = useState(false);
  const [lit, setLit] = useState(false);
  const [shaking, setShaking] = useState(false);
  const [flash, setFlash] = useState<"win" | "lose" | null>(null);
  const [fellFrom, setFellFrom] = useState<number | null>(null);
  const [last, setLast] = useState<QuotaGambleResult | null>(null);
  const [error, setError] = useState<string | null>(null);

  const alive = useRef(true);
  useEffect(
    () => () => {
      alive.current = false;
    },
    [],
  );

  const amount = parseWager(wager, ctx.remaining);
  const mult = multAt(rung);
  const reachPct = (Math.pow(0.75, rung) * 100).toFixed(rung >= 7 ? 1 : 0);
  const camY = Math.max(0, mpos.y - CAM_ANCHOR);

  const monkeyTransition =
    mmode === "leap"
      ? `transform ${LEAP_MS}ms cubic-bezier(0.3, 0.4, 0.5, 1)`
      : mmode === "tumble"
        ? `transform ${TUMBLE_MS}ms cubic-bezier(0.55, -0.2, 0.85, 0.55)`
        : "transform 400ms cubic-bezier(0.25, 0.8, 0.3, 1)";
  const camTransition = `transform ${mmode === "tumble" ? TUMBLE_MS : 420}ms cubic-bezier(0.25, 0.8, 0.3, 1)`;

  const slingIt = async () => {
    if (busy || amount === null) return;
    setBusy(true);
    setLast(null);
    setError(null);
    setFlash(null);
    setGrabbed(0);
    setPlanted(false);
    setLit(false);
    setFellFrom(null);
    setMpos(GROUND);
    setMmode("crouch");
    const target = rung;

    let result: QuotaGambleResult;
    try {
      result = await ctx.play({ game: "sling", wager_usd: amount.toFixed(2), rung: target });
    } catch (err) {
      if (!alive.current) return;
      setError(errorMessage(err));
      setMmode("idle");
      setBusy(false);
      return;
    }
    const fellAt = typeof result.detail.fell_at === "number" ? result.detail.fell_at : null;

    await sleep(620);
    const lastGood = fellAt === null ? target : fellAt - 1;
    for (let k = 1; k <= lastGood; k++) {
      if (!alive.current) return;
      setMmode("windup");
      await sleep(180);
      ctx.audio.sfx("whoosh");
      setMmode("leap");
      setMpos({ x: rungX(k), y: rungY(k) });
      await sleep(LEAP_MS);
      ctx.audio.sfx("snap");
      setMmode("grab");
      setGrabbed(k);
      await sleep(140);
    }
    if (!alive.current) return;

    if (fellAt !== null) {
      setMmode("windup");
      await sleep(200);
      ctx.audio.sfx("whoosh");
      setMmode("leap");
      setMpos({ x: rungX(fellAt), y: rungY(fellAt) + 42 });
      await sleep(LEAP_MS);
      if (!alive.current) return;
      setMmode("miss");
      await sleep(460);
      if (!alive.current) return;
      ctx.audio.sfx("lose");
      setMmode("tumble");
      setMpos(GROUND);
      await sleep(TUMBLE_MS);
      if (!alive.current) return;
      setMmode("splat");
      setFellFrom(fellAt);
      setFlash("lose");
      setShaking(true);
      setLast(result);
      setTimeout(() => alive.current && setShaking(false), 500);
    } else {
      await sleep(240);
      if (!alive.current) return;
      setMmode("celebrate");
      setPlanted(true);
      setLit(true);
      setFlash("win");
      ctx.audio.sfx(result.net_usd >= 25 || multAt(target) >= 5 ? "bigwin" : "win");
      setLast(result);
    }
    setBusy(false);
  };

  return (
    <CinemaShell
      theme="sling"
      title="SLING KING"
      tagline="Ten holds up the jungle wall. Every grab is a coin toss the monkey usually wins."
      hudExtra={
        <span className="cz-chip">
          rung {rung} · ×{mult.toFixed(2)}
        </span>
      }
    >
      <style>{`
        .sl-stage {
          position: relative; overflow: hidden; border-radius: 18px;
          width: min(340px, 92vw); height: clamp(320px, 50vh, 460px);
          border: 1px solid color-mix(in srgb, var(--cz-accent) 35%, transparent);
          background:
            linear-gradient(180deg, rgba(10,25,6,0.2), rgba(6,18,4,0.75)),
            radial-gradient(220px 160px at 20% 20%, rgba(163,230,53,0.10), transparent 70%),
            radial-gradient(260px 200px at 85% 70%, rgba(245,158,11,0.08), transparent 70%);
          box-shadow: inset 0 0 60px rgba(0,0,0,0.55);
        }
        .sl-wall { position: absolute; left: 50%; bottom: 0; width: ${WALL_W}px; height: ${WALL_H}px; will-change: transform; }
        .sl-vine {
          position: absolute; left: 50%; bottom: 0; width: 7px; height: 100%;
          transform: translateX(-50%); border-radius: 4px;
          background: repeating-linear-gradient(180deg, #3f6212 0 26px, #4d7c0f 26px 52px);
          opacity: 0.85;
        }
        .sl-rung {
          position: absolute; left: 50%; width: 92px; height: 18px;
          border: none; padding: 0; cursor: pointer; border-radius: 999px;
          background: linear-gradient(180deg, #d9a45c, #8a5a2b 80%);
          box-shadow: 0 3px 0 rgba(0,0,0,0.4), inset 0 1px 0 rgba(255,255,255,0.25);
        }
        .sl-rung:disabled { cursor: default; }
        .sl-rung.sl-target {
          outline: 2px solid var(--cz-accent);
          animation: sl-pulse 900ms ease-in-out infinite alternate;
        }
        .sl-rung.sl-grabbed {
          background: linear-gradient(180deg, #bef264, #65a30d 80%);
          box-shadow: 0 0 14px color-mix(in srgb, var(--cz-accent) 55%, transparent);
        }
        .sl-rung.sl-vinelit { animation: sl-vinelit 700ms ease-in-out 2; }
        @keyframes sl-pulse {
          from { box-shadow: 0 0 6px color-mix(in srgb, var(--cz-accent) 40%, transparent); }
          to { box-shadow: 0 0 22px color-mix(in srgb, var(--cz-accent) 80%, transparent); }
        }
        @keyframes sl-vinelit {
          0%, 100% { filter: none; }
          50% { filter: brightness(1.7) drop-shadow(0 0 10px var(--cz-accent)); }
        }
        .sl-mult {
          position: absolute; top: 50%; transform: translateY(-50%);
          font-family: var(--cz-font-display); font-size: 0.74rem; letter-spacing: 0.04em;
          color: var(--cz-dim); text-shadow: 0 1px 3px rgba(0,0,0,0.9); white-space: nowrap;
        }
        .sl-rung.sl-left .sl-mult { left: calc(100% + 9px); }
        .sl-rung.sl-right .sl-mult { right: calc(100% + 9px); }
        .sl-rung.sl-target .sl-mult { color: var(--cz-accent); font-weight: 700; font-size: 0.85rem; }
        .sl-monkey {
          position: absolute; left: 50%; bottom: 0; z-index: 12;
          font-size: 44px; line-height: 1; will-change: transform;
          filter: drop-shadow(0 8px 10px rgba(0,0,0,0.6));
        }
        .sl-minner { display: inline-block; }
        .sl-m-idle { animation: sl-bob 1.6s ease-in-out infinite alternate; }
        .sl-m-crouch { animation: sl-crouch 280ms ease-in-out infinite alternate; }
        .sl-m-windup { animation: sl-windup 180ms ease-in forwards; }
        .sl-m-leap { animation: sl-arc ${LEAP_MS}ms cubic-bezier(0.3, 0, 0.7, 1); }
        .sl-m-grab { animation: sl-grab 150ms ease-out; }
        .sl-m-miss { animation: sl-miss 460ms ease-in-out; }
        .sl-m-tumble { animation: sl-spin 480ms linear infinite; }
        .sl-m-splat { transform: rotate(96deg) scaleY(0.85); }
        .sl-m-celebrate { animation: sl-party 480ms ease-in-out infinite alternate; }
        @keyframes sl-bob { from { transform: translateY(0); } to { transform: translateY(-6px); } }
        @keyframes sl-crouch { from { transform: scaleY(0.86) scaleX(1.08); } to { transform: scaleY(0.78) scaleX(1.14) translateY(3px); } }
        @keyframes sl-windup { from { transform: none; } to { transform: scaleY(0.7) scaleX(1.2) translateY(5px); } }
        @keyframes sl-arc {
          0% { transform: translateY(0) rotate(0); }
          45% { transform: translateY(-38px) rotate(-9deg); }
          100% { transform: translateY(0) rotate(0); }
        }
        @keyframes sl-grab { from { transform: scale(1.2); } to { transform: scale(1); } }
        @keyframes sl-miss {
          0%, 100% { transform: rotate(0); }
          30% { transform: rotate(-16deg) translateY(-4px); }
          65% { transform: rotate(12deg); }
        }
        @keyframes sl-spin { from { transform: rotate(0); } to { transform: rotate(-360deg); } }
        @keyframes sl-party { from { transform: translateY(0) rotate(-6deg); } to { transform: translateY(-8px) rotate(6deg); } }
        .sl-alarm { position: absolute; left: 50%; top: -22px; transform: translateX(-50%); font-size: 20px; }
        .sl-crown { position: absolute; left: 50%; top: -24px; transform: translateX(-50%); font-size: 22px; }
        .sl-flag { position: absolute; left: 50%; z-index: 11; font-size: 30px; filter: drop-shadow(0 4px 6px rgba(0,0,0,0.6)); }
        .sl-ground {
          position: absolute; left: 50%; bottom: -4px; transform: translateX(-50%);
          font-size: 24px; white-space: nowrap; letter-spacing: -4px;
        }
        .sl-leaf { position: absolute; left: 50%; pointer-events: none; }
      `}</style>

      <div className="mx-auto flex min-h-full max-w-4xl flex-col items-center justify-center gap-6 px-4 py-8 md:flex-row md:gap-12">
        <div className={shaking ? "cz-shake" : ""}>
          <div className="sl-stage">
            <div className="sl-wall" style={{ transform: `translate3d(-50%, ${camY}px, 0)`, transition: camTransition }}>
              <div className="sl-vine" />
              {LEAVES.map((l, i) => (
                <span
                  key={i}
                  className="sl-leaf"
                  style={{
                    bottom: l.y,
                    fontSize: l.s,
                    opacity: l.o,
                    transform: `translateX(calc(-50% + ${l.x}px))`,
                  }}
                >
                  {l.e}
                </span>
              ))}
              {SLING_LADDER.map((m, i) => {
                const k = i + 1;
                return (
                  <button
                    key={k}
                    type="button"
                    aria-label={`Target rung ${k}, pays ${m.toFixed(2)}x`}
                    className={[
                      "sl-rung",
                      k % 2 ? "sl-left" : "sl-right",
                      k === rung ? "sl-target" : "",
                      grabbed >= k ? "sl-grabbed" : "",
                      lit && k <= rung ? "sl-vinelit" : "",
                    ].join(" ")}
                    style={{
                      bottom: rungY(k) - 16,
                      transform: `translateX(calc(-50% + ${rungX(k)}px))`,
                      animationDelay: lit && k <= rung ? `${k * 60}ms` : undefined,
                    }}
                    disabled={busy}
                    onClick={() => {
                      ctx.audio.sfx("tick");
                      setRung(k);
                    }}
                  >
                    <span className="sl-mult">×{m.toFixed(2)}</span>
                  </button>
                );
              })}
              {planted ? (
                <span
                  className="sl-flag cz-pop"
                  style={{ bottom: rungY(rung) + 20, transform: `translateX(calc(-50% + ${rungX(rung) + 34}px))` }}
                >
                  🚩
                </span>
              ) : null}
              <div
                className="sl-monkey"
                style={{
                  transform: `translate3d(calc(-50% + ${mpos.x}px), ${-mpos.y}px, 0)`,
                  transition: monkeyTransition,
                }}
              >
                <span className={`sl-minner sl-m-${mmode}`}>🐒</span>
                {mmode === "miss" ? <span className="sl-alarm cz-pop">❗</span> : null}
                {mmode === "splat" ? <span className="sl-alarm">💫</span> : null}
                {mmode === "celebrate" ? <span className="sl-crown cz-pop">👑</span> : null}
              </div>
              <div className="sl-ground">🌴🌿🍌🌿🪨🌿🍌🌿🌴</div>
            </div>

            {flash === "win" ? (
              <div
                className="cz-flash"
                style={{ background: "radial-gradient(circle, rgba(163,230,53,0.55), transparent 72%)" }}
              />
            ) : null}
            {flash === "lose" ? <div className="cz-flash" style={{ background: "rgba(248,113,113,0.4)" }} /> : null}

            <div className="pointer-events-none absolute inset-x-0 top-2 z-20 flex justify-center px-2">
              {last ? (
                <div
                  className={`cz-pop cz-display text-center text-lg font-bold ${last.won ? "cz-glow" : ""}`}
                  style={{ color: last.won ? "var(--cz-accent)" : "#f87171" }}
                >
                  {last.won
                    ? `KING OF RUNG ${rung} — +${formatDollars(last.net_usd)} for 24 hours`
                    : `RUNG ${fellFrom} SAID NO — ${formatDollars(last.net_usd)}. Gravity collects.`}
                </div>
              ) : (
                <div className="cz-display text-sm" style={{ color: "var(--cz-dim)" }}>
                  {busy
                    ? grabbed > 0
                      ? `GRIP ${grabbed} / ${rung} — hold on…`
                      : "Chalking up…"
                    : "Pick a rung. Fling the monkey."}
                </div>
              )}
            </div>
          </div>
        </div>

        <div className="flex w-full max-w-sm flex-col items-center gap-5 md:w-80">
          <div className="flex items-center gap-4">
            <button
              className="cz-btn"
              aria-label="Lower target rung"
              disabled={busy || rung <= 1}
              onClick={() => {
                ctx.audio.sfx("click");
                setRung((r) => Math.max(1, r - 1));
              }}
            >
              −
            </button>
            <div className="text-center">
              <div className="cz-display text-3xl font-bold" style={{ color: "var(--cz-accent)" }}>
                RUNG {rung}
              </div>
              <div className="cz-display cz-glow text-xl" style={{ color: "var(--cz-accent2)" }}>
                ×{mult.toFixed(2)}
              </div>
              <div className="text-xs" style={{ color: "var(--cz-dim)" }}>
                {reachPct}% chance the monkey makes it
              </div>
            </div>
            <button
              className="cz-btn"
              aria-label="Raise target rung"
              disabled={busy || rung >= MAX_RUNG}
              onClick={() => {
                ctx.audio.sfx("click");
                setRung((r) => Math.min(MAX_RUNG, r + 1));
              }}
            >
              +
            </button>
          </div>

          {amount !== null ? (
            <div className="cz-display text-sm" style={{ color: "var(--cz-dim)" }}>
              Lands it → <span style={{ color: "var(--cz-text)" }}>{formatDollars(amount * mult)}</span> swings back
            </div>
          ) : null}

          <WagerPanel value={wager} onChange={setWager} disabled={busy} />

          <button className="cz-btn cz-btn-primary" disabled={busy || amount === null} onClick={slingIt}>
            {busy ? "MONKEY IN FLIGHT…" : amount === null ? "ENTER A WAGER" : `SLING FOR ${formatDollars(amount)}`}
          </button>

          <div className="text-center text-xs" style={{ color: "var(--cz-dim)" }}>
            75% per grab. The wall keeps the other 2%.
          </div>

          {error ? <div className="text-center text-sm text-red-400">{error}</div> : null}
        </div>
      </div>
    </CinemaShell>
  );
}
