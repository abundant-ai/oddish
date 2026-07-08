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

const MIN_SPIN_MS = 1400;

type Side = "heads" | "tails";

export default function CoinflipGame() {
  const ctx = useCasino();
  const [side, setSide] = useState<Side>("heads");
  const [wager, setWager] = useState("5.00");
  const [spinning, setSpinning] = useState(false);
  const [last, setLast] = useState<QuotaGambleResult | null>(null);
  const [error, setError] = useState<string | null>(null);

  const amount = parseWager(wager, ctx.remaining);
  const shownFace: Side = spinning ? side : ((last?.result ?? "heads") as Side);

  const flip = async () => {
    if (spinning || amount === null) return;
    setSpinning(true);
    setLast(null);
    setError(null);
    ctx.audio.sfx("flip");
    const started = Date.now();
    try {
      const result = await ctx.play({
        game: "coinflip",
        wager_usd: amount.toFixed(2),
        side,
      });
      await new Promise((r) => setTimeout(r, Math.max(0, MIN_SPIN_MS - (Date.now() - started))));
      setLast(result);
      ctx.audio.sfx(result.won ? (result.net_usd >= 25 ? "bigwin" : "win") : "lose");
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setSpinning(false);
    }
  };

  return (
    <CinemaShell
      theme="coinflip"
      title="DOUBLE OR NOTHING"
      tagline="One coin. Two futures. Your 24h budget rides."
    >
      <style>{`
        .cf-stage { perspective: 900px; }
        .cf-coin { position: relative; width: 11rem; height: 11rem; transform-style: preserve-3d; transition: transform 700ms cubic-bezier(0.2, 0.7, 0.3, 1.1); }
        .cf-coin.spinning { animation: cf-spin 420ms linear infinite; }
        @keyframes cf-spin { from { transform: rotateY(0); } to { transform: rotateY(360deg); } }
        .cf-face { position: absolute; inset: 0; border-radius: 9999px; display: flex; align-items: center; justify-content: center; backface-visibility: hidden; font-size: 3.5rem; font-weight: 900; font-family: var(--cz-font-display); }
        .cf-heads { background: radial-gradient(circle at 35% 30%, #fde68a, #d97706 75%); border: 6px solid #fbbf24; color: #78350f; box-shadow: 0 0 45px rgba(251,191,36,0.5); }
        .cf-tails { transform: rotateY(180deg); background: radial-gradient(circle at 35% 30%, #e2e8f0, #64748b 75%); border: 6px solid #cbd5e1; color: #1e293b; box-shadow: 0 0 45px rgba(148,163,184,0.4); }
      `}</style>
      <div className="mx-auto flex min-h-full max-w-2xl flex-col items-center justify-center gap-8 px-4 py-10">
        <div className="cf-stage">
          <div
            className={`cf-coin ${spinning ? "spinning" : ""}`}
            style={{ transform: spinning ? undefined : `rotateY(${shownFace === "tails" ? 180 : 0}deg)` }}
          >
            <div className="cf-face cf-heads">H</div>
            <div className="cf-face cf-tails">T</div>
          </div>
        </div>

        {last ? (
          <div
            className={`cz-pop cz-display text-center text-2xl font-bold ${last.won ? "cz-glow" : "cz-shake"}`}
            style={{ color: last.won ? "var(--cz-accent)" : "#f87171" }}
          >
            {last.result?.toUpperCase()} —{" "}
            {last.won
              ? `+${formatDollars(last.net_usd)} for 24 hours`
              : `${formatDollars(last.net_usd)}. The house nods.`}
          </div>
        ) : (
          <div className="cz-display text-center text-lg" style={{ color: "var(--cz-dim)" }}>
            {spinning ? "The coin is in the air…" : "Call it."}
          </div>
        )}
        {error ? <div className="text-center text-sm text-red-400">{error}</div> : null}

        <div className="flex gap-3">
          {(["heads", "tails"] as const).map((s) => (
            <button
              key={s}
              className={`cz-btn ${side === s ? "cz-btn-primary" : ""}`}
              disabled={spinning}
              onClick={() => {
                ctx.audio.sfx("click");
                setSide(s);
              }}
            >
              {s.toUpperCase()}
            </button>
          ))}
        </div>

        <WagerPanel value={wager} onChange={setWager} disabled={spinning} />

        <button className="cz-btn cz-btn-primary" disabled={spinning || amount === null} onClick={flip}>
          {spinning ? "FLIPPING…" : amount === null ? "ENTER A WAGER" : `FLIP FOR ${formatDollars(amount)}`}
        </button>
      </div>
    </CinemaShell>
  );
}
