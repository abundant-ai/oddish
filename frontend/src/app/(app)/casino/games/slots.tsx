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

type SlotSymbol = "7" | "BAR" | "BELL" | "CHERRY" | "LEMON";

const VISUAL: SlotSymbol[] = ["7", "BAR", "BELL", "CHERRY", "LEMON"];
const IDLE: SlotSymbol[] = ["BAR", "7", "BAR"];
const PLURAL: Record<SlotSymbol, string> = {
  "7": "SEVENS",
  BAR: "BARS",
  BELL: "BELLS",
  CHERRY: "CHERRIES",
  LEMON: "LEMONS",
};

// Mirrors backend SLOT_TRIPLES + pair rules in casino_games.slot_multiplier.
const PAYTABLE: { id: string; combo: (SlotSymbol | null)[]; pays: string; note?: string }[] = [
  { id: "triple-7", combo: ["7", "7", "7"], pays: "100×" },
  { id: "triple-BAR", combo: ["BAR", "BAR", "BAR"], pays: "25×" },
  { id: "triple-BELL", combo: ["BELL", "BELL", "BELL"], pays: "12×" },
  { id: "triple-CHERRY", combo: ["CHERRY", "CHERRY", "CHERRY"], pays: "8×" },
  { id: "triple-LEMON", combo: ["LEMON", "LEMON", "LEMON"], pays: "4×" },
  { id: "pair-7", combo: ["7", "7", null], pays: "2×" },
  { id: "pair-CHERRY", combo: ["CHERRY", "CHERRY", null], pays: "1.5×" },
  { id: "pair-BELL", combo: ["BELL", "BELL", null], pays: "1×", note: "push" },
  { id: "one-7", combo: ["7", null, null], pays: "1×", note: "push" },
];

function lineId(reels: SlotSymbol[]): string | null {
  const count = (s: SlotSymbol) => reels.filter((x) => x === s).length;
  if (reels[0] === reels[1] && reels[1] === reels[2]) return `triple-${reels[0]}`;
  if (count("7") === 2) return "pair-7";
  if (count("CHERRY") === 2) return "pair-CHERRY";
  if (count("BELL") === 2) return "pair-BELL";
  if (count("7") === 1) return "one-7";
  return null;
}

function lineLabel(id: string | null, reels: SlotSymbol[]): string {
  if (id?.startsWith("triple-")) return `TRIPLE ${PLURAL[reels[0]]}`;
  if (id === "pair-7") return "TWO SEVENS";
  if (id === "pair-CHERRY") return "CHERRY PAIR";
  if (id === "pair-BELL") return "BELL PAIR";
  if (id === "one-7") return "LONE SEVEN";
  return "NO LINE";
}

function Art({ s }: { s: SlotSymbol }) {
  if (s === "7") return <span className="sl-seven">7</span>;
  if (s === "BAR") return <span className="sl-barmark">BAR</span>;
  return <span className="sl-fruit">{s === "BELL" ? "🔔" : s === "CHERRY" ? "🍒" : "🍋"}</span>;
}

export default function SlotsGame() {
  const ctx = useCasino();
  const [wager, setWager] = useState("5.00");
  const [phase, setPhase] = useState<"idle" | "spin" | "done">("idle");
  const [stopped, setStopped] = useState(0);
  const [finalReels, setFinalReels] = useState<SlotSymbol[] | null>(null);
  const [holding, setHolding] = useState(false);
  const [last, setLast] = useState<QuotaGambleResult | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [flashKey, setFlashKey] = useState(0);

  const amount = parseWager(wager, ctx.remaining);
  const spinning = phase === "spin";
  const lit = phase === "done" && finalReels ? lineId(finalReels) : null;
  const lost = phase === "done" && last !== null && last.multiplier === 0;

  const pull = async () => {
    if (spinning || amount === null) return;
    setPhase("spin");
    setStopped(0);
    setHolding(false);
    setLast(null);
    setError(null);
    ctx.audio.sfx("snap");
    ctx.audio.sfx("whoosh");
    const t0 = Date.now();
    const until = (ms: number) =>
      new Promise<void>((res) => setTimeout(res, Math.max(0, ms - (Date.now() - t0))));
    try {
      const r = await ctx.play({ game: "slots", wager_usd: amount.toFixed(2) });
      const symbols = ((r.detail as { reels?: unknown }).reels ?? []) as SlotSymbol[];
      if (symbols.length !== 3) throw new Error("The cabinet jammed. Try another pull.");
      const near = symbols[0] === symbols[1];
      setFinalReels(symbols);
      await until(700);
      setStopped(1);
      ctx.audio.sfx("snap");
      await until(1200);
      setStopped(2);
      ctx.audio.sfx("snap");
      if (near) {
        setHolding(true);
        for (let i = 0; i < 4; i++) {
          await until(1280 + i * 150);
          ctx.audio.sfx("tick");
        }
      }
      await until(near ? 2300 : 1700);
      setHolding(false);
      setStopped(3);
      ctx.audio.sfx("snap");
      await until(near ? 2650 : 2050);
      setLast(r);
      setPhase("done");
      const jackpot = symbols.every((s) => s === "7");
      if (jackpot) {
        setFlashKey((k) => k + 1);
        ctx.audio.sfx("bigwin");
      } else if (r.multiplier > 1) {
        ctx.audio.sfx(r.net_usd >= 25 ? "bigwin" : "win");
      } else if (r.multiplier === 1) {
        ctx.audio.sfx("push");
      } else {
        ctx.audio.sfx("lose");
      }
    } catch (err) {
      setError(errorMessage(err));
      setPhase("idle");
      setStopped(0);
      setHolding(false);
    }
  };

  const jackpot = phase === "done" && finalReels !== null && lit === "triple-7";
  const statusCopy = !spinning
    ? "Feed the machine. Pull the arm."
    : holding && finalReels
      ? finalReels[0] === "7"
        ? "TWO SEVENS… ONE MORE FOR 100×."
        : `TWO ${PLURAL[finalReels[0]]}… HOLD EVERYTHING.`
      : stopped === 0
        ? "The reels are screaming…"
        : stopped === 1
          ? "First window locks…"
          : "Last reel decides your day…";

  return (
    <CinemaShell
      theme="slots"
      title="QUOTA 7s"
      tagline="One arm. Five fruits. Triple sevens pays 100×."
    >
      <style>{`
        .sl-cab { position: relative; border-radius: 18px; border: 3px solid #b45309; padding: 1rem 1.75rem 1.6rem; background: linear-gradient(180deg, #45100a, #2a0906 45%, #1b0503); box-shadow: 0 0 55px rgba(245,158,11,0.16), inset 0 0 46px rgba(0,0,0,0.65); }
        .sl-title { font-size: 2rem; font-weight: 900; color: #fde047; text-align: center; text-shadow: 0 2px 0 #92400e, 0 0 22px rgba(245,158,11,0.85); }
        .sl-bulbs { display: flex; justify-content: center; gap: 0.55rem; padding: 0.3rem 0; }
        .sl-bulb { width: 7px; height: 7px; border-radius: 999px; background: #fbbf24; box-shadow: 0 0 8px #fbbf24; animation: sl-blink 1.1s steps(1) infinite; }
        .sl-bulb:nth-child(even) { animation-delay: 0.55s; }
        @keyframes sl-blink { 0%, 49% { opacity: 1; } 50%, 100% { opacity: 0.22; } }
        .sl-window { position: relative; width: 6.5rem; height: 7rem; overflow: hidden; border-radius: 10px; border: 3px solid #92400e; background: linear-gradient(180deg, #fcd34d, #fffbeb 28%, #fffbeb 72%, #f59e0b); box-shadow: inset 0 8px 16px rgba(0,0,0,0.35), inset 0 -8px 16px rgba(0,0,0,0.35); }
        .sl-window::after { content: ""; position: absolute; left: 4px; right: 4px; top: 50%; height: 2px; transform: translateY(-50%); background: rgba(239,68,68,0.35); pointer-events: none; }
        .sl-hot { animation: sl-hot 0.45s ease-in-out infinite alternate; }
        @keyframes sl-hot { from { box-shadow: inset 0 8px 16px rgba(0,0,0,0.35), 0 0 8px rgba(245,158,11,0.4); } to { box-shadow: inset 0 8px 16px rgba(0,0,0,0.35), 0 0 30px rgba(253,224,71,0.95); } }
        .sl-strip { will-change: transform; filter: blur(2.5px); animation: sl-roll linear infinite; }
        @keyframes sl-roll { from { transform: translateY(0); } to { transform: translateY(-50%); } }
        .sl-cell { height: 7rem; display: flex; align-items: center; justify-content: center; }
        .sl-settle { animation: sl-settle 380ms cubic-bezier(0.2, 0.9, 0.3, 1.3); }
        @keyframes sl-settle { 0% { transform: translateY(-58%); } 70% { transform: translateY(7%); } 100% { transform: translateY(0); } }
        .sl-seven { font-family: var(--cz-font-display); font-size: 3.4rem; line-height: 1; color: #ef4444; text-shadow: 2px 2px 0 #7f1d1d, 0 0 14px rgba(251,191,36,0.85); }
        .sl-barmark { font-family: var(--cz-font-display); font-size: 1.35rem; letter-spacing: 0.06em; color: #fbbf24; background: #111; border: 2px solid #fbbf24; border-radius: 5px; padding: 0.1rem 0.45rem; transform: skewX(-6deg); }
        .sl-fruit { font-size: 2.9rem; line-height: 1; filter: drop-shadow(0 3px 4px rgba(0,0,0,0.35)); }
        .sl-mini .sl-seven { font-size: 1.15rem; text-shadow: 1px 1px 0 #7f1d1d; }
        .sl-mini .sl-barmark { font-size: 0.55rem; padding: 0 0.2rem; border-width: 1px; border-radius: 3px; }
        .sl-mini .sl-fruit { font-size: 1rem; filter: none; }
        .sl-any { color: var(--cz-dim); font-size: 0.85rem; }
        .sl-lever { position: absolute; right: -2.3rem; top: 26%; width: 0.65rem; height: 6rem; border-radius: 999px; background: linear-gradient(90deg, #9ca3af, #e5e7eb 45%, #6b7280); transform-origin: bottom center; transition: transform 260ms cubic-bezier(0.5, 0, 0.4, 1.4); }
        .sl-lever::before { content: ""; position: absolute; top: -1.5rem; left: 50%; transform: translateX(-50%); width: 1.9rem; height: 1.9rem; border-radius: 999px; background: radial-gradient(circle at 35% 30%, #fca5a5, #b91c1c 72%); box-shadow: 0 0 14px rgba(239,68,68,0.7); }
        .sl-pulled { transform: scaleY(-1); }
        .sl-pay { min-width: 15rem; border: 1px solid color-mix(in srgb, var(--cz-accent) 40%, transparent); border-radius: 14px; background: rgba(0,0,0,0.4); padding: 0.85rem 0.9rem; }
        .sl-payrow { display: flex; align-items: center; justify-content: space-between; gap: 1rem; padding: 0.26rem 0.55rem; border-radius: 8px; }
        .sl-lit { background: color-mix(in srgb, var(--cz-accent) 24%, transparent); box-shadow: 0 0 16px color-mix(in srgb, var(--cz-accent) 55%, transparent); }
        .sl-pull { font-size: 1.25rem; padding: 0.85rem 2.4rem; border-radius: 14px; }
      `}</style>

      <div className="relative mx-auto flex min-h-full max-w-5xl flex-col items-center justify-center gap-6 px-4 py-8">
        {flashKey > 0 ? (
          <div
            key={flashKey}
            className="cz-flash z-30"
            style={{ background: "radial-gradient(circle, rgba(253,224,71,0.95), rgba(245,158,11,0.55) 70%)" }}
          />
        ) : null}

        <div className="flex flex-wrap items-center justify-center gap-10">
          <div className={`sl-cab ${lost ? "cz-shake" : ""}`}>
            <div className={`sl-lever hidden md:block ${spinning ? "sl-pulled" : ""}`} />
            <div className="sl-bulbs">
              {Array.from({ length: 14 }, (_, i) => (
                <span key={i} className="sl-bulb" />
              ))}
            </div>
            <div className="cz-display sl-title">QUOTA 7s</div>
            <div className="sl-bulbs">
              {Array.from({ length: 14 }, (_, i) => (
                <span key={i} className="sl-bulb" />
              ))}
            </div>
            <div className="mt-4 flex justify-center gap-3">
              {[0, 1, 2].map((i) => {
                const rolling = spinning && stopped <= i;
                const sym = finalReels && stopped > i ? finalReels[i] : IDLE[i];
                return (
                  <div key={i} className={`sl-window ${holding && i === 2 ? "sl-hot" : ""}`}>
                    {rolling ? (
                      <div key="spin" className="sl-strip" style={{ animationDuration: `${260 + i * 60}ms` }}>
                        {[...VISUAL, ...VISUAL].map((s, j) => (
                          <div key={j} className="sl-cell">
                            <Art s={s} />
                          </div>
                        ))}
                      </div>
                    ) : (
                      <div key="held" className={`sl-cell ${finalReels ? "sl-settle" : ""}`}>
                        <Art s={sym} />
                      </div>
                    )}
                  </div>
                );
              })}
            </div>
          </div>

          <aside className="sl-pay">
            <div className="cz-display mb-2 text-sm font-bold" style={{ color: "var(--cz-accent)" }}>
              PAYTABLE — HOUSE VERIFIED
            </div>
            {PAYTABLE.map((row) => (
              <div key={row.id} className={`sl-payrow ${lit === row.id ? "sl-lit cz-pop" : ""}`}>
                <span className="sl-mini flex items-center gap-1.5">
                  {row.combo.map((s, i) =>
                    s ? (
                      <Art key={i} s={s} />
                    ) : (
                      <span key={i} className="sl-any">
                        ✱
                      </span>
                    ),
                  )}
                </span>
                <span className="cz-display text-sm" style={{ color: "var(--cz-text)" }}>
                  {row.pays}
                  {row.note ? (
                    <span className="ml-1 text-xs" style={{ color: "var(--cz-dim)" }}>
                      {row.note}
                    </span>
                  ) : null}
                </span>
              </div>
            ))}
          </aside>
        </div>

        <div className="flex min-h-[4.5rem] flex-col items-center justify-center text-center">
          {phase === "done" && last && finalReels ? (
            jackpot ? (
              <div className="cz-pop">
                <div className="cz-display cz-glow font-black" style={{ fontSize: "2.6rem", color: "#fde047" }}>
                  ★ JACKPOT ★
                </div>
                <div className="cz-display text-xl font-bold" style={{ color: "var(--cz-accent)" }}>
                  TRIPLE SEVENS PAYS 100× — +{formatDollars(last.net_usd)} FOR 24 HOURS
                </div>
              </div>
            ) : last.multiplier > 1 ? (
              <div className="cz-pop cz-display cz-glow text-2xl font-bold" style={{ color: "var(--cz-accent)" }}>
                {lineLabel(lit, finalReels)} PAYS {last.multiplier}× — +{formatDollars(last.net_usd)}
              </div>
            ) : last.multiplier === 1 ? (
              <div className="cz-pop cz-display text-xl font-bold" style={{ color: "var(--cz-text)" }}>
                {lineLabel(lit, finalReels)} — PUSH. THE MACHINE BLINKS FIRST.
              </div>
            ) : (
              <div className="cz-shake cz-display text-2xl font-bold" style={{ color: "#f87171" }}>
                NO LINE — {formatDollars(last.net_usd)}. THE HOUSE HUMS ALONG.
              </div>
            )
          ) : (
            <div className="cz-display text-lg" style={{ color: holding ? "var(--cz-accent)" : "var(--cz-dim)" }}>
              {statusCopy}
            </div>
          )}
          {error ? <div className="mt-1 text-sm text-red-400">{error}</div> : null}
        </div>

        <WagerPanel value={wager} onChange={setWager} disabled={spinning} />

        <button className="cz-btn cz-btn-primary sl-pull" disabled={spinning || amount === null} onClick={pull}>
          {spinning ? "REELS ROLLING…" : amount === null ? "ENTER A WAGER" : `PULL — ${formatDollars(amount)}`}
        </button>
      </div>
    </CinemaShell>
  );
}
