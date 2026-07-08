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

type BacBet = "player" | "banker" | "tie";
type Phase = "idle" | "dealing" | "reveal" | "shoe" | "settled";
type SideKey = "p" | "b";

type BacDetail = {
  bet: BacBet;
  player: string[];
  banker: string[];
  player_total: number;
  banker_total: number;
  outcome: BacBet;
};

const PLAQUES: { bet: BacBet; label: string; sub: string; pays: string }[] = [
  { bet: "player", label: "PLAYER", sub: "PONTE", pays: "PAYS 1:1" },
  { bet: "banker", label: "BANKER", sub: "BANCO", pays: "PAYS 0.95:1" },
  { bet: "tie", label: "ÉGALITÉ", sub: "TIE", pays: "PAYS 8:1" },
];

const SUITS: Record<string, { glyph: string; color: string }> = {
  S: { glyph: "♠", color: "#26161c" },
  H: { glyph: "♥", color: "#9f1239" },
  D: { glyph: "♦", color: "#9f1239" },
  C: { glyph: "♣", color: "#26161c" },
};

const MIN_DRAW_MS = 900;

const cardValue = (code: string) => {
  const rank = code.slice(0, -1);
  if (rank === "A") return 1;
  if (rank === "10" || rank === "J" || rank === "Q" || rank === "K") return 0;
  return Number(rank);
};

const shownTotal = (cards: string[], upTo: number) =>
  cards.slice(0, upTo).reduce((s, c) => s + cardValue(c), 0) % 10;

const sleep = (ms: number) => new Promise<void>((r) => setTimeout(r, ms));

function BacCard({
  code,
  faceUp,
  third,
  dealDelay,
}: {
  code: string | null;
  faceUp: boolean;
  third?: boolean;
  dealDelay?: number;
}) {
  const rank = code ? code.slice(0, -1) : "";
  const suit = code ? SUITS[code.slice(-1)] : undefined;
  return (
    <div
      className={`bp-cardbox bp-deal-in ${third ? "bp-third" : ""}`}
      style={{ animationDelay: `${dealDelay ?? 0}ms` }}
    >
      <div className={`bp-card ${faceUp ? "bp-up" : ""}`}>
        <div className="bp-face bp-front" style={{ color: suit?.color }}>
          <span className="bp-rank">{rank}</span>
          <span className="bp-pip">{suit?.glyph}</span>
          <span className="bp-rank bp-rank-b">{rank}</span>
        </div>
        <div className="bp-face bp-back">
          <div className="bp-back-inner" />
        </div>
      </div>
    </div>
  );
}

export default function BaccaratGame() {
  const ctx = useCasino();
  const [bet, setBet] = useState<BacBet>("player");
  const [wager, setWager] = useState("5.00");
  const [phase, setPhase] = useState<Phase>("idle");
  const [detail, setDetail] = useState<BacDetail | null>(null);
  const [last, setLast] = useState<QuotaGambleResult | null>(null);
  const [revealed, setRevealed] = useState<{ p: number; b: number }>({ p: 0, b: 0 });
  const [thirdOut, setThirdOut] = useState<{ p: boolean; b: boolean }>({ p: false, b: false });
  const [error, setError] = useState<string | null>(null);
  const [round, setRound] = useState(0);
  const alive = useRef(true);

  useEffect(() => {
    alive.current = true;
    return () => {
      alive.current = false;
    };
  }, []);

  const amount = parseWager(wager, ctx.remaining);
  const live = phase === "dealing" || phase === "reveal" || phase === "shoe";
  const push = last !== null && !last.won && last.multiplier === 1;
  const lost = last !== null && !last.won && last.multiplier !== 1;

  const deal = async () => {
    if (live || amount === null) return;
    setPhase("dealing");
    setDetail(null);
    setLast(null);
    setError(null);
    setRevealed({ p: 0, b: 0 });
    setThirdOut({ p: false, b: false });
    setRound((r) => r + 1);
    ctx.audio.sfx("chip");
    const started = Date.now();
    try {
      const result = await ctx.play({
        game: "baccarat",
        wager_usd: amount.toFixed(2),
        bet,
      });
      const d = result.detail as unknown as BacDetail;
      await sleep(Math.max(0, MIN_DRAW_MS - (Date.now() - started)));
      if (!alive.current) return;
      setDetail(d);
      setPhase("reveal");
      const firstFour: [SideKey, number][] = [
        ["p", 1],
        ["b", 1],
        ["p", 2],
        ["b", 2],
      ];
      for (const [side, n] of firstFour) {
        await sleep(450);
        if (!alive.current) return;
        ctx.audio.sfx("card");
        setRevealed((r) => ({ ...r, [side]: n }));
      }
      const hasP3 = d.player.length === 3;
      const hasB3 = d.banker.length === 3;
      if (hasP3 || hasB3) {
        await sleep(500);
        if (!alive.current) return;
        setPhase("shoe");
        ctx.audio.sfx("tick");
        setThirdOut({ p: hasP3, b: hasB3 });
        await sleep(700);
        if (!alive.current) return;
        if (hasP3) {
          ctx.audio.sfx("card");
          setRevealed((r) => ({ ...r, p: 3 }));
          await sleep(450);
          if (!alive.current) return;
        }
        if (hasB3) {
          if (hasP3) {
            ctx.audio.sfx("tick");
            await sleep(300);
            if (!alive.current) return;
          }
          ctx.audio.sfx("card");
          setRevealed((r) => ({ ...r, b: 3 }));
          await sleep(450);
          if (!alive.current) return;
        }
      }
      await sleep(600);
      if (!alive.current) return;
      setLast(result);
      setPhase("settled");
      if (result.won) ctx.audio.sfx(result.net_usd >= 25 ? "bigwin" : "win");
      else if (result.multiplier === 1) ctx.audio.sfx("push");
      else ctx.audio.sfx("lose");
    } catch (err) {
      if (!alive.current) return;
      setError(errorMessage(err));
      setPhase("idle");
    }
  };

  const banner = () => {
    if (!detail || !last) return null;
    const d = detail;
    let head: string;
    if (d.outcome === "tie") {
      const naturalTie =
        d.player.length === 2 && d.banker.length === 2 && d.player_total >= 8;
      head = `${naturalTie ? "NATURAL " : ""}ÉGALITÉ — ${d.player_total} À ${d.banker_total}`;
    } else {
      const winCards = d.outcome === "player" ? d.player : d.banker;
      const winTotal = d.outcome === "player" ? d.player_total : d.banker_total;
      const loseTotal = d.outcome === "player" ? d.banker_total : d.player_total;
      const natural =
        winCards.length === 2 && winTotal >= 8
          ? `NATURAL ${winTotal === 9 ? "NINE" : "EIGHT"} — `
          : "";
      head = `${natural}${d.outcome.toUpperCase()} WINS ${winTotal}–${loseTotal}`;
    }
    const sub = last.won
      ? `+${formatDollars(last.net_usd)} onto your 24-hour line. The salon inclines its head.`
      : push
        ? "Égalité. Your stake slides back across the felt, untouched."
        : `${formatDollars(last.net_usd)}. The shoe is indifferent to allegiance.`;
    return (
      <div className="cz-pop text-center">
        <div
          className={`cz-display text-2xl font-bold ${last.won ? "cz-glow" : ""}`}
          style={{
            color: last.won ? "var(--cz-accent2)" : push ? "var(--cz-dim)" : "#fda4af",
            letterSpacing: "0.12em",
          }}
        >
          {head}
        </div>
        <div className="mt-1 text-sm italic" style={{ color: "var(--cz-dim)" }}>
          {sub}
        </div>
      </div>
    );
  };

  const statusLine = () => {
    if (phase === "settled") return banner();
    const line =
      phase === "dealing"
        ? "The croupier draws from the shoe…"
        : phase === "shoe"
          ? "The shoe decides…"
          : phase === "reveal"
            ? "A slow squeeze at the corner of the card…"
            : "Choose a side. The shoe holds eight decks and no opinions.";
    return (
      <div
        className={`cz-display text-center text-lg italic ${phase === "shoe" ? "bp-breathe" : ""}`}
        style={{ color: phase === "shoe" ? "var(--cz-accent2)" : "var(--cz-dim)" }}
      >
        {line}
      </div>
    );
  };

  const renderHand = (side: SideKey) => {
    const cards = detail ? (side === "p" ? detail.player : detail.banker) : null;
    if (!cards && phase === "idle") {
      return [0, 1].map((i) => <div key={`${side}-slot-${i}`} className="bp-slot" />);
    }
    const rev = side === "p" ? revealed.p : revealed.b;
    const showThird = !!cards && cards.length === 3 && (side === "p" ? thirdOut.p : thirdOut.b);
    const count = showThird ? 3 : 2;
    return Array.from({ length: count }, (_, i) => (
      <BacCard
        key={`${side}${i}`}
        code={cards?.[i] ?? null}
        faceUp={i < rev}
        third={i === 2}
        dealDelay={i < 2 ? (side === "p" ? i * 2 : i * 2 + 1) * 140 : 0}
      />
    ));
  };

  const totalMedallion = (side: SideKey) => {
    const cards = detail ? (side === "p" ? detail.player : detail.banker) : null;
    const rev = side === "p" ? revealed.p : revealed.b;
    const value = cards && rev > 0 ? shownTotal(cards, rev) : null;
    const isWinner =
      phase === "settled" &&
      detail !== null &&
      (detail.outcome === "tie" || detail.outcome === (side === "p" ? "player" : "banker"));
    return (
      <div
        key={`${side}-${rev}-${round}`}
        className={`bp-total cz-pop ${isWinner ? "bp-total-win" : ""}`}
      >
        {value === null ? "·" : value}
      </div>
    );
  };

  const sideLabel = (side: SideKey) => {
    const isWinner =
      phase === "settled" &&
      detail !== null &&
      (detail.outcome === "tie" || detail.outcome === (side === "p" ? "player" : "banker"));
    return (
      <div
        className="cz-display text-sm font-bold"
        style={{
          letterSpacing: "0.35em",
          color: isWinner ? "var(--cz-accent2)" : "var(--cz-dim)",
          textShadow: isWinner ? "0 0 14px color-mix(in srgb, var(--cz-accent2) 55%, transparent)" : undefined,
        }}
      >
        {side === "p" ? "PLAYER" : "BANKER"}
      </div>
    );
  };

  return (
    <CinemaShell
      theme="baccarat"
      title="SALON PRIVÉ"
      tagline="Eight decks, two hands, and a room that never raises its voice."
    >
      <style>{`
        .bp-table {
          position: relative;
          border-radius: 2.25rem;
          background:
            radial-gradient(130% 150% at 50% -10%, rgba(190,24,93,0.30), transparent 60%),
            radial-gradient(120% 140% at 50% 110%, rgba(76,5,45,0.9), #1c0713 70%);
          border: 1px solid color-mix(in srgb, var(--cz-accent2) 35%, transparent);
          box-shadow:
            inset 0 0 0 6px rgba(0,0,0,0.35),
            inset 0 0 0 7px color-mix(in srgb, var(--cz-accent2) 22%, transparent),
            0 24px 60px rgba(0,0,0,0.55);
          padding: 1.75rem 1.5rem 1.5rem;
        }
        .bp-hands { display: grid; grid-template-columns: 1fr auto 1fr; align-items: start; gap: 1rem; }
        .bp-hand { display: flex; flex-direction: column; align-items: center; gap: 0.75rem; }
        .bp-cards { display: flex; justify-content: center; gap: 0.5rem; perspective: 900px; min-height: 6.9rem; }
        .bp-divider { width: 1px; align-self: stretch; background: linear-gradient(180deg, transparent, color-mix(in srgb, var(--cz-accent2) 40%, transparent), transparent); }
        .bp-cardbox { width: 4.6rem; height: 6.6rem; }
        .bp-third { transform: rotate(7deg) translateY(4px); }
        @keyframes bp-deal { from { opacity: 0; transform: translateY(-14px); } to { opacity: 1; transform: translateY(0); } }
        .bp-deal-in { animation: bp-deal 380ms cubic-bezier(0.2, 0.8, 0.2, 1) both; }
        .bp-card {
          position: relative; width: 100%; height: 100%;
          transform-style: preserve-3d; transform: rotateY(180deg);
          transition: transform 520ms cubic-bezier(0.3, 0.7, 0.2, 1);
        }
        .bp-card.bp-up { transform: rotateY(0deg); }
        .bp-face {
          position: absolute; inset: 0; border-radius: 8px;
          backface-visibility: hidden; -webkit-backface-visibility: hidden;
        }
        .bp-front {
          background: linear-gradient(160deg, #fdf6e3, #efe3c8);
          border: 1px solid rgba(0,0,0,0.35);
          box-shadow: 0 6px 18px rgba(0,0,0,0.5);
          display: flex; flex-direction: column; align-items: center; justify-content: center;
          font-family: var(--cz-font-display);
        }
        .bp-rank { position: absolute; top: 0.25rem; left: 0.4rem; font-size: 1rem; font-weight: 700; }
        .bp-rank-b { top: auto; left: auto; bottom: 0.25rem; right: 0.4rem; transform: rotate(180deg); }
        .bp-pip { font-size: 2.4rem; line-height: 1; }
        .bp-back {
          transform: rotateY(180deg);
          background: linear-gradient(160deg, #4a0d2a, #2a0618);
          border: 1px solid color-mix(in srgb, var(--cz-accent2) 45%, transparent);
          box-shadow: 0 6px 18px rgba(0,0,0,0.5);
          padding: 0.4rem;
        }
        .bp-back-inner {
          width: 100%; height: 100%; border-radius: 4px;
          border: 1px solid color-mix(in srgb, var(--cz-accent2) 40%, transparent);
          background:
            repeating-linear-gradient(45deg, transparent 0 5px, color-mix(in srgb, var(--cz-accent2) 14%, transparent) 5px 6px),
            repeating-linear-gradient(-45deg, transparent 0 5px, color-mix(in srgb, var(--cz-accent2) 10%, transparent) 5px 6px);
        }
        .bp-slot {
          width: 4.6rem; height: 6.6rem; border-radius: 8px;
          border: 1px dashed color-mix(in srgb, var(--cz-accent2) 28%, transparent);
          background: rgba(0,0,0,0.18);
        }
        .bp-total {
          width: 2.7rem; height: 2.7rem; border-radius: 999px;
          border: 1px solid color-mix(in srgb, var(--cz-accent2) 50%, transparent);
          background: rgba(0,0,0,0.45);
          display: flex; align-items: center; justify-content: center;
          font-family: var(--cz-font-display); font-size: 1.3rem; color: var(--cz-text);
        }
        .bp-total-win {
          border-color: var(--cz-accent2); color: var(--cz-accent2);
          box-shadow: 0 0 16px color-mix(in srgb, var(--cz-accent2) 45%, transparent);
        }
        .bp-plaque {
          position: relative; border-radius: 12px; padding: 0.6rem 1.35rem;
          background: linear-gradient(180deg, color-mix(in srgb, var(--cz-accent2) 10%, transparent), rgba(0,0,0,0.4));
          border: 1px solid color-mix(in srgb, var(--cz-accent2) 35%, transparent);
          box-shadow: inset 0 0 0 3px rgba(0,0,0,0.35), inset 0 0 0 4px color-mix(in srgb, var(--cz-accent2) 22%, transparent);
          font-family: var(--cz-font-display); letter-spacing: 0.14em; color: var(--cz-text);
          cursor: pointer; transition: transform 150ms, box-shadow 150ms, border-color 150ms;
          text-align: center;
        }
        .bp-plaque:hover:not(:disabled) { transform: translateY(-2px); }
        .bp-plaque:disabled { opacity: 0.45; cursor: not-allowed; }
        .bp-sel {
          border-color: var(--cz-accent2); transform: translateY(-2px);
          box-shadow: inset 0 0 0 3px rgba(0,0,0,0.35), inset 0 0 0 4px color-mix(in srgb, var(--cz-accent2) 45%, transparent),
            0 0 18px color-mix(in srgb, var(--cz-accent2) 25%, transparent);
        }
        @keyframes bp-crown {
          0%, 100% { box-shadow: inset 0 0 0 3px rgba(0,0,0,0.35), inset 0 0 0 4px color-mix(in srgb, var(--cz-accent2) 55%, transparent), 0 0 12px color-mix(in srgb, var(--cz-accent2) 35%, transparent); }
          50% { box-shadow: inset 0 0 0 3px rgba(0,0,0,0.35), inset 0 0 0 4px var(--cz-accent2), 0 0 30px color-mix(in srgb, var(--cz-accent2) 60%, transparent); }
        }
        .bp-winner { border-color: var(--cz-accent2); animation: bp-crown 1.6s ease-in-out infinite; }
        @keyframes bp-breathe-kf { 0%, 100% { opacity: 0.65; } 50% { opacity: 1; } }
        .bp-breathe { animation: bp-breathe-kf 1.1s ease-in-out infinite; }
      `}</style>

      <div className="relative mx-auto flex min-h-full max-w-3xl flex-col items-center justify-center gap-6 px-4 py-8">
        {phase === "settled" && last?.won ? (
          <div
            key={round}
            className="cz-flash"
            style={{
              background:
                last.net_usd >= 25
                  ? "radial-gradient(ellipse at 50% 35%, rgba(251,191,36,0.6), rgba(190,24,93,0.25) 55%, transparent 75%)"
                  : "radial-gradient(ellipse at 50% 35%, rgba(251,191,36,0.4), transparent 65%)",
            }}
          />
        ) : null}

        <div className={`bp-table w-full ${phase === "settled" && lost ? "cz-shake" : ""}`}>
          <div className="bp-hands">
            <div className="bp-hand">
              {sideLabel("p")}
              <div className="bp-cards">{renderHand("p")}</div>
              {totalMedallion("p")}
            </div>
            <div className="bp-divider" />
            <div className="bp-hand">
              {sideLabel("b")}
              <div className="bp-cards">{renderHand("b")}</div>
              {totalMedallion("b")}
            </div>
          </div>
        </div>

        <div className="flex min-h-[4.25rem] w-full items-center justify-center px-2">
          {statusLine()}
        </div>
        {error ? <div className="text-center text-sm text-red-400">{error}</div> : null}

        <div className="flex flex-wrap items-stretch justify-center gap-3">
          {PLAQUES.map((p) => {
            const isWinner = phase === "settled" && detail?.outcome === p.bet;
            return (
              <button
                key={p.bet}
                className={`bp-plaque ${bet === p.bet ? "bp-sel" : ""} ${isWinner ? "bp-winner" : ""}`}
                disabled={live}
                onClick={() => {
                  ctx.audio.sfx("click");
                  setBet(p.bet);
                }}
              >
                <div className="text-base font-bold">{p.label}</div>
                <div className="text-[0.6rem]" style={{ color: "var(--cz-dim)" }}>
                  {p.sub} · {p.pays}
                </div>
              </button>
            );
          })}
        </div>
        <div className="text-center text-[0.65rem] italic" style={{ color: "var(--cz-dim)" }}>
          Banker keeps a 5% commission. Égalité returns Player and Banker stakes.
        </div>

        <WagerPanel value={wager} onChange={setWager} disabled={live} />

        <button
          className="cz-btn cz-btn-primary"
          disabled={live || amount === null}
          onClick={deal}
        >
          {live
            ? "THE CARDS ARE OUT…"
            : amount === null
              ? "ENTER A WAGER"
              : `DEAL — ${formatDollars(amount)} ON ${bet === "tie" ? "ÉGALITÉ" : bet.toUpperCase()}`}
        </button>
      </div>
    </CinemaShell>
  );
}
