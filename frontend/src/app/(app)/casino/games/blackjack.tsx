"use client";

import { useEffect, useRef, useState, type CSSProperties } from "react";
import type { BlackjackState } from "@/lib/types";
import {
  CinemaShell,
  WagerPanel,
  errorMessage,
  formatDollars,
  parseWager,
  useCasino,
} from "../casino-kit";

const SUITS: Record<string, { glyph: string; red: boolean }> = {
  S: { glyph: "♠", red: false },
  H: { glyph: "♥", red: true },
  D: { glyph: "♦", red: true },
  C: { glyph: "♣", red: false },
};

function handTotal(cards: string[]): { total: number; soft: boolean } {
  let total = 0;
  let aces = 0;
  for (const card of cards) {
    const rank = card.slice(0, -1);
    if (rank === "A") {
      aces += 1;
      total += 11;
    } else if (rank === "10" || rank === "J" || rank === "Q" || rank === "K") {
      total += 10;
    } else {
      total += Number(rank) || 0;
    }
  }
  while (total > 21 && aces > 0) {
    total -= 10;
    aces -= 1;
  }
  return { total, soft: aces > 0 };
}

const tiltFor = (i: number, n: number) =>
  Math.max(-9, Math.min(9, (i - (n - 1) / 2) * 3));

const tiltStyle = (deg: number) => ({ "--bj-tilt": `${deg}deg` }) as CSSProperties;

function CardFace({ code }: { code: string }) {
  const rank = code.slice(0, -1);
  const suit = SUITS[code.slice(-1)] ?? { glyph: "♠", red: false };
  return (
    <div className={`bj-card bj-front ${suit.red ? "bj-red" : "bj-black"}`}>
      <div className="bj-corner">
        {rank}
        <span>{suit.glyph}</span>
      </div>
      <div className="bj-pip">{suit.glyph}</div>
      <div className="bj-corner bj-corner-b">
        {rank}
        <span>{suit.glyph}</span>
      </div>
    </div>
  );
}

function CardBack() {
  return (
    <div className="bj-card bj-back">
      <span>21</span>
    </div>
  );
}

type Phase = "idle" | "dealing" | "player" | "settling" | "over";

type Banner = { title: string; sub: string; color: string; glow: boolean };

const sleep = (ms: number) => new Promise<void>((r) => setTimeout(r, ms));

export default function BlackjackGame() {
  const ctx = useCasino();
  const alive = useRef(true);
  const [wager, setWager] = useState("5.00");
  const [phase, setPhase] = useState<Phase>("idle");
  const [busy, setBusy] = useState(false);
  const [hand, setHand] = useState<BlackjackState | null>(null);
  const [playerCards, setPlayerCards] = useState<string[]>([]);
  const [dealerCards, setDealerCards] = useState<string[]>([]);
  const [holeUp, setHoleUp] = useState(false);
  const [tension, setTension] = useState<string | null>(null);
  const [banner, setBanner] = useState<Banner | null>(null);
  const [flash, setFlash] = useState<{ key: number; color: string } | null>(null);
  const [shaking, setShaking] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(
    () => () => {
      alive.current = false;
    },
    [],
  );

  const amount = parseWager(wager, ctx.remaining);
  const player = handTotal(playerCards);
  const upTotal = handTotal(dealerCards.slice(0, 1)).total;
  const dealerLive = handTotal(dealerCards.filter((c) => c !== "??")).total;

  const runSettle = async (s: BlackjackState) => {
    setPhase("settling");
    const busted = handTotal(s.player).total > 21;
    setTension(
      busted
        ? "Over the line. The dealer turns the hole card anyway — house etiquette."
        : "The dealer's fingers rest on the hole card…",
    );
    await sleep(busted ? 600 : 850);
    if (!alive.current) return;
    ctx.audio.sfx("tick");
    await sleep(320);
    setDealerCards(s.dealer.slice(0, 2));
    await sleep(80);
    if (!alive.current) return;
    ctx.audio.sfx("flip");
    setHoleUp(true);
    await sleep(760);
    if (s.dealer.length > 2) {
      setTension("Seventeen or bust — the house has no choice.");
      for (let i = 3; i <= s.dealer.length; i++) {
        if (!alive.current) return;
        setDealerCards(s.dealer.slice(0, i));
        ctx.audio.sfx("card");
        await sleep(580);
      }
    }
    await sleep(420);
    if (!alive.current) return;

    const net = s.net_usd ?? 0;
    const plus = `+${formatDollars(net)}`;
    const minus = formatDollars(net);
    const dealerTotal = s.dealer_total ?? handTotal(s.dealer).total;
    let b: Banner;
    if (s.outcome === "blackjack") {
      b = {
        title: "MIDNIGHT TWENTY-ONE",
        sub: `A natural, paid 3:2. ${plus} — the pit boss pretends not to notice.`,
        color: "var(--cz-accent2)",
        glow: true,
      };
      setFlash({ key: Date.now(), color: "rgba(250,204,21,0.75)" });
      ctx.audio.sfx("bigwin");
    } else if (s.outcome === "win") {
      b =
        dealerTotal > 21
          ? {
              title: "DEALER BUSTS",
              sub: `${dealerTotal}, and over the cliff. ${plus} slides across the felt.`,
              color: "var(--cz-accent)",
              glow: true,
            }
          : {
              title: "YOU TAKE THE POT",
              sub: `${s.player_total} over ${dealerTotal}. ${plus}, quietly.`,
              color: "var(--cz-accent)",
              glow: true,
            };
      setFlash({
        key: Date.now(),
        color: "color-mix(in srgb, var(--cz-accent) 65%, transparent)",
      });
      ctx.audio.sfx(net >= 25 ? "bigwin" : "win");
    } else if (s.outcome === "push") {
      b = {
        title: "STANDOFF",
        sub: `${s.player_total} apiece. Your stake walks back to you — nobody blinks.`,
        color: "var(--cz-dim)",
        glow: false,
      };
      ctx.audio.sfx("push");
    } else {
      b = busted
        ? {
            title: "BUST",
            sub: `${minus}. ${s.player_total} — one card past midnight.`,
            color: "#f87171",
            glow: false,
          }
        : {
            title: "THE HOUSE STANDS",
            sub: `${dealerTotal} over ${s.player_total}. ${minus}. The dealer never smiles.`,
            color: "#f87171",
            glow: false,
          };
      setShaking(true);
      setTimeout(() => {
        if (alive.current) setShaking(false);
      }, 500);
      ctx.audio.sfx("lose");
    }
    setTension(null);
    setBanner(b);
    setPhase("over");
    setBusy(false);
  };

  const deal = async () => {
    if (busy || amount === null) return;
    setBusy(true);
    setError(null);
    setBanner(null);
    setTension(null);
    setHoleUp(false);
    setPlayerCards([]);
    setDealerCards([]);
    setPhase("dealing");
    ctx.audio.sfx("click");
    try {
      const s = await ctx.playBlackjack({
        action: "deal",
        wager_usd: amount.toFixed(2),
      });
      if (!alive.current) return;
      setHand(s);
      setTension("Cards whisper out of the shoe…");
      await sleep(320);
      ctx.audio.sfx("card");
      setPlayerCards(s.player.slice(0, 1));
      await sleep(250);
      ctx.audio.sfx("card");
      setDealerCards(s.dealer.slice(0, 1));
      await sleep(250);
      ctx.audio.sfx("card");
      setPlayerCards(s.player.slice(0, 2));
      await sleep(250);
      ctx.audio.sfx("card");
      setDealerCards([...s.dealer.slice(0, 1), "??"]);
      await sleep(380);
      if (!alive.current) return;
      if (s.status === "settled") {
        await runSettle(s);
      } else {
        setTension(null);
        setPhase("player");
        setBusy(false);
      }
    } catch (err) {
      if (!alive.current) return;
      setError(errorMessage(err));
      setPhase("idle");
      setBusy(false);
    }
  };

  const act = async (action: "hit" | "stand" | "double") => {
    if (busy || !hand || hand.status !== "player_turn") return;
    setBusy(true);
    setError(null);
    ctx.audio.sfx(action === "double" ? "chip" : "click");
    try {
      const s = await ctx.playBlackjack({ action, session_id: hand.session_id });
      if (!alive.current) return;
      setHand(s);
      if (s.player.length > playerCards.length) {
        await sleep(180);
        ctx.audio.sfx("card");
        setPlayerCards(s.player);
        await sleep(440);
        if (!alive.current) return;
      }
      if (s.status === "settled") {
        await runSettle(s);
      } else {
        setBusy(false);
      }
    } catch (err) {
      if (!alive.current) return;
      setError(errorMessage(err));
      setBusy(false);
    }
  };

  const handLive = phase === "dealing" || phase === "player" || phase === "settling";

  const renderHand = (cards: string[], isDealer: boolean) => {
    const slots = cards.map((c, i) => {
      const tilt = tiltFor(i, cards.length);
      if (isDealer && i === 1) {
        return (
          <div key={i} className="bj-in" style={tiltStyle(tilt)}>
            <div className="bj-flip">
              <div className={`bj-flip-inner ${holeUp ? "bj-up" : ""}`}>
                <div className="bj-flip-face">
                  <CardBack />
                </div>
                <div className="bj-flip-face bj-flip-rear">
                  {c === "??" ? <CardBack /> : <CardFace code={c} />}
                </div>
              </div>
            </div>
          </div>
        );
      }
      return (
        <div key={i} className="bj-in" style={tiltStyle(tilt)}>
          {c === "??" ? <CardBack /> : <CardFace code={c} />}
        </div>
      );
    });
    for (let i = cards.length; i < 2; i++) {
      slots.push(<div key={`ghost-${i}`} className="bj-ghost" />);
    }
    return slots;
  };

  const dealerChip =
    dealerCards.length === 0
      ? null
      : holeUp
        ? `${dealerLive}${dealerLive > 21 ? " — BUST" : ""}`
        : `showing ${upTotal} + ?`;

  const playerChip =
    playerCards.length === 0
      ? null
      : `${player.total}${player.soft ? " soft" : ""}${player.total > 21 ? " — BUST" : ""}`;

  return (
    <CinemaShell
      theme="blackjack"
      title="MIDNIGHT 21"
      tagline="Green felt, low light, and a dealer who has seen everything."
      hudExtra={
        handLive && hand ? (
          <span className="cz-chip" style={{ color: "var(--cz-accent2)" }}>
            riding {formatDollars(hand.wager_usd)}
          </span>
        ) : undefined
      }
    >
      <style>{`
        .bj-card { width: 4.9rem; height: 7rem; border-radius: 0.55rem; box-shadow: 0 8px 18px rgba(0,0,0,0.45); }
        .bj-front { position: relative; background: linear-gradient(160deg, #fdfbf3, #eee8d8); border: 1px solid #d6cfbd; display: flex; align-items: center; justify-content: center; }
        .bj-red { color: #b91c1c; }
        .bj-black { color: #1c1917; }
        .bj-corner { position: absolute; top: 0.3rem; left: 0.42rem; font-size: 0.85rem; font-weight: 700; line-height: 1.05; text-align: center; font-family: var(--cz-font-display); }
        .bj-corner span { display: block; font-size: 0.78rem; }
        .bj-corner-b { top: auto; left: auto; right: 0.42rem; bottom: 0.3rem; transform: rotate(180deg); }
        .bj-pip { font-size: 2.3rem; }
        .bj-back { background: repeating-linear-gradient(45deg, #14532d 0 7px, #0b3a1f 7px 14px); border: 1px solid rgba(250,204,21,0.5); display: flex; align-items: center; justify-content: center; }
        .bj-back span { color: rgba(250,204,21,0.75); font-family: var(--cz-font-display); font-size: 1.05rem; letter-spacing: 0.08em; border: 1px solid rgba(250,204,21,0.45); border-radius: 999px; width: 2.3rem; height: 2.3rem; display: flex; align-items: center; justify-content: center; background: rgba(4,20,12,0.4); }
        .bj-ghost { width: 4.9rem; height: 7rem; border-radius: 0.55rem; border: 1px dashed rgba(236,253,245,0.16); }
        .bj-hand { display: flex; justify-content: center; min-height: 7.2rem; }
        .bj-hand > * + * { margin-left: -1.1rem; }
        @keyframes bj-deal { from { transform: translate(30vw, -30vh) rotate(24deg); opacity: 0; } to { transform: translate(0, 0) rotate(var(--bj-tilt, 0deg)); opacity: 1; } }
        .bj-in { animation: bj-deal 380ms cubic-bezier(0.18, 0.75, 0.25, 1) both; transform: rotate(var(--bj-tilt, 0deg)); }
        .bj-flip { width: 4.9rem; height: 7rem; perspective: 900px; }
        .bj-flip-inner { position: relative; width: 100%; height: 100%; transform-style: preserve-3d; transition: transform 640ms cubic-bezier(0.35, 0.8, 0.3, 1); }
        .bj-flip-inner.bj-up { transform: rotateY(180deg); }
        .bj-flip-face { position: absolute; inset: 0; backface-visibility: hidden; }
        .bj-flip-rear { transform: rotateY(180deg); }
        .bj-felt { position: relative; width: 100%; max-width: 56rem; min-height: 26rem;
          border-radius: 1.4rem 1.4rem 50% 50% / 1.4rem 1.4rem 34% 34%;
          background: radial-gradient(120% 95% at 50% 0%, #0d4a2b 0%, #08351e 55%, #052414 100%);
          box-shadow: inset 0 0 0 2px rgba(250,204,21,0.26), inset 0 0 90px rgba(0,0,0,0.55), 0 28px 60px rgba(0,0,0,0.5);
          padding: 1.4rem 1.6rem 3rem; display: flex; flex-direction: column; justify-content: space-between; gap: 1rem; }
        .bj-shoe { position: absolute; top: 1.1rem; right: 1.4rem; width: 3.1rem; height: 4.3rem; opacity: 0.85; }
        .bj-shoe div { position: absolute; inset: 0; border-radius: 0.4rem; background: repeating-linear-gradient(45deg, #14532d 0 5px, #0b3a1f 5px 10px); border: 1px solid rgba(250,204,21,0.35); }
        .bj-shoe div:nth-child(2) { transform: translate(-3px, 3px); }
        .bj-shoe div:nth-child(3) { transform: translate(-6px, 6px); }
        .bj-center { position: absolute; left: 0; right: 0; top: 50%; transform: translateY(-50%); display: flex; flex-direction: column; align-items: center; text-align: center; pointer-events: none; padding: 0 3rem; }
        .bj-label { font-family: var(--cz-font-display); letter-spacing: 0.35em; font-size: 0.68rem; color: rgba(250,204,21,0.6); }
        .bj-stencil { color: rgba(250,204,21,0.34); font-family: var(--cz-font-display); }
      `}</style>

      <div className="relative mx-auto flex min-h-full max-w-4xl flex-col items-center justify-center gap-5 px-4 py-8">
        {flash ? (
          <div
            key={flash.key}
            className="cz-flash"
            style={{ background: flash.color, zIndex: 20 }}
          />
        ) : null}

        <div className={`bj-felt ${shaking ? "cz-shake" : ""}`}>
          <div className="bj-shoe" aria-hidden>
            <div />
            <div />
            <div />
          </div>

          <div className="flex flex-col items-center gap-2">
            <div className="flex items-center gap-2">
              <span className="bj-label">DEALER</span>
              {dealerChip ? (
                <span
                  className="cz-chip"
                  style={holeUp && dealerLive > 21 ? { color: "#f87171" } : undefined}
                >
                  {dealerChip}
                </span>
              ) : null}
            </div>
            <div className="bj-hand">{renderHand(dealerCards, true)}</div>
          </div>

          <div className="bj-center">
            {banner ? (
              <div className="cz-pop">
                <div
                  className={`cz-display font-bold ${banner.glow ? "cz-glow" : ""}`}
                  style={{ fontSize: "2rem", color: banner.color }}
                >
                  {banner.title}
                </div>
                <div style={{ color: "var(--cz-dim)", marginTop: "0.4rem", fontSize: "0.95rem" }}>
                  {banner.sub}
                </div>
              </div>
            ) : tension ? (
              <div
                className="cz-pop"
                style={{ color: "var(--cz-dim)", fontStyle: "italic", fontSize: "1rem" }}
              >
                {tension}
              </div>
            ) : (
              <div className="bj-stencil">
                <div style={{ fontSize: "1.5rem", letterSpacing: "0.4em" }}>MIDNIGHT 21</div>
                <div style={{ fontSize: "0.7rem", marginTop: "0.35rem", letterSpacing: "0.2em" }}>
                  DEALER STANDS ON 17 · BLACKJACK PAYS 3:2
                </div>
              </div>
            )}
          </div>

          <div className="flex flex-col items-center gap-2">
            <div className="bj-hand">{renderHand(playerCards, false)}</div>
            <div className="flex items-center gap-2">
              <span className="bj-label">YOU</span>
              {playerChip ? (
                <span
                  className="cz-chip"
                  style={
                    player.total > 21
                      ? { color: "#f87171" }
                      : player.total === 21
                        ? { color: "var(--cz-accent2)" }
                        : undefined
                  }
                >
                  {playerChip}
                </span>
              ) : null}
            </div>
          </div>
        </div>

        <div className="flex min-h-[8.5rem] w-full flex-col items-center gap-3">
          {phase === "player" && hand ? (
            <>
              <div className="flex flex-wrap justify-center gap-3">
                <button className="cz-btn cz-btn-primary" disabled={busy} onClick={() => act("hit")}>
                  HIT
                </button>
                <button className="cz-btn cz-btn-primary" disabled={busy} onClick={() => act("stand")}>
                  STAND
                </button>
                <button
                  className="cz-btn"
                  disabled={busy || !hand.can_double}
                  onClick={() => act("double")}
                  title={hand.can_double ? "Double the stake, take exactly one card" : "Only on your first two cards"}
                >
                  DOUBLE DOWN
                </button>
              </div>
              <div className="text-center text-xs italic" style={{ color: "var(--cz-dim)" }}>
                {formatDollars(hand.wager_usd)} riding. House rule: the stake is already in the
                cage — walk out mid-hand and it stays there.
              </div>
            </>
          ) : phase === "dealing" || phase === "settling" ? (
            <div className="cz-display text-sm" style={{ color: "var(--cz-dim)" }}>
              {phase === "dealing" ? "The shoe decides…" : "The table holds its breath…"}
            </div>
          ) : (
            <>
              <WagerPanel value={wager} onChange={setWager} disabled={busy} />
              <button
                className="cz-btn cz-btn-primary"
                disabled={busy || amount === null}
                onClick={deal}
              >
                {amount === null
                  ? "ENTER A WAGER"
                  : `${phase === "over" ? "NEXT HAND" : "DEAL"} — ${formatDollars(amount)}`}
              </button>
            </>
          )}
          {error ? <div className="text-center text-sm text-red-400">{error}</div> : null}
          <div className="text-center text-xs" style={{ color: "var(--cz-dim)", opacity: 0.8 }}>
            Dealer stands on 17 · Blackjack pays 3:2 · No splits — this is a speakeasy, not a
            spreadsheet.
          </div>
        </div>
      </div>
    </CinemaShell>
  );
}
