"use client";

import { useCallback, useMemo, useState } from "react";
import useSWR from "swr";
import { Dices, History } from "lucide-react";
import { fetcher } from "@/lib/api";
import type {
  BlackjackState,
  QuotaGambleList,
  QuotaGambleResult,
  QuotaUsage,
} from "@/lib/types";
import { Badge } from "@/components/ui/badge";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import {
  CasinoProvider,
  formatDollars,
  useCasinoAudio,
  type CasinoCtx,
  type CasinoGameId,
} from "./casino-kit";
import BaccaratGame from "./games/baccarat";
import BlackjackGame from "./games/blackjack";
import CoinflipGame from "./games/coinflip";
import PlinkoGame from "./games/plinko";
import RocketGame from "./games/rocket";
import SlingGame from "./games/sling";
import SlotsGame from "./games/slots";

const GAMES: {
  id: CasinoGameId;
  name: string;
  blurb: string;
  glyph: string;
  gradient: string;
  Component: () => React.JSX.Element;
}[] = [
  { id: "coinflip", name: "Double or Nothing", blurb: "One coin, two futures", glyph: "🪙", gradient: "linear-gradient(135deg, #78350f, #f59e0b)", Component: CoinflipGame },
  { id: "plinko", name: "Plinko Peak", blurb: "12 rows of fate", glyph: "🔻", gradient: "linear-gradient(135deg, #581c87, #d946ef)", Component: PlinkoGame },
  { id: "slots", name: "Quota 7s", blurb: "Three reels, one dream", glyph: "🎰", gradient: "linear-gradient(135deg, #7f1d1d, #f59e0b)", Component: SlotsGame },
  { id: "blackjack", name: "Midnight 21", blurb: "Beat the dealer", glyph: "🃏", gradient: "linear-gradient(135deg, #064e3b, #34d399)", Component: BlackjackGame },
  { id: "baccarat", name: "Salon Privé", blurb: "Player, banker, destiny", glyph: "🥂", gradient: "linear-gradient(135deg, #831843, #f472b6)", Component: BaccaratGame },
  { id: "rocket", name: "Escape Velocity", blurb: "Cash out before the crash", glyph: "🚀", gradient: "linear-gradient(135deg, #1e1b4b, #fb923c)", Component: RocketGame },
  { id: "sling", name: "Sling King", blurb: "Climb the jungle wall", glyph: "🐒", gradient: "linear-gradient(135deg, #365314, #a3e635)", Component: SlingGame },
];

const GAME_NAMES: Record<string, string> = Object.fromEntries(
  GAMES.map((g) => [g.id, g.name]),
);

export default function CasinoPage() {
  const {
    data: quota,
    error: quotaError,
    mutate: mutateQuota,
  } = useSWR<QuotaUsage>("/api/quotas/me", fetcher);
  const { data: history, error: historyError, mutate: mutateHistory } =
    useSWR<QuotaGambleList>("/api/quotas/gambles", fetcher);

  const audio = useCasinoAudio();
  const [active, setActive] = useState<CasinoGameId | null>(null);
  const [sessionNet, setSessionNet] = useState(0);

  const remaining = Math.max(
    0,
    (quota?.limit_usd ?? 0) - (quota?.used_usd ?? 0) - (quota?.reserved_usd ?? 0),
  );
  const casinoClosed = (historyError as { status?: number } | undefined)?.status === 503;

  const refresh = useCallback(() => {
    void mutateQuota();
    void mutateHistory();
  }, [mutateQuota, mutateHistory]);

  const post = useCallback(async (url: string, body: Record<string, unknown>) => {
    const res = await fetch(url, {
      method: "POST",
      credentials: "include",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    const data = await res.json();
    if (!res.ok) {
      throw new Error(
        (data as { detail?: string; error?: string }).detail ||
          (data as { error?: string }).error ||
          `Bet failed (${res.status})`,
      );
    }
    return data;
  }, []);

  const play = useCallback(
    async (body: Record<string, unknown>): Promise<QuotaGambleResult> => {
      const data = (await post("/api/quotas/gamble", body)) as QuotaGambleResult;
      setSessionNet((n) => n + data.net_usd);
      refresh();
      return data;
    },
    [post, refresh],
  );

  const playBlackjack = useCallback(
    async (body: Record<string, unknown>): Promise<BlackjackState> => {
      const data = (await post("/api/quotas/gamble/blackjack", body)) as BlackjackState;
      if (data.status === "settled" && data.net_usd !== null) {
        // The deal escrowed -wager server-side; the settled net already
        // includes getting that stake back, so session delta is just net.
        setSessionNet((n) => n + data.net_usd!);
      }
      refresh();
      return data;
    },
    [post, refresh],
  );

  const exit = useCallback(() => {
    setActive(null);
    refresh();
  }, [refresh]);

  const ctx: CasinoCtx = useMemo(
    () => ({
      quota,
      history,
      remaining,
      sessionNet,
      play,
      playBlackjack,
      refresh,
      audio,
      exit,
    }),
    [quota, history, remaining, sessionNet, play, playBlackjack, refresh, audio, exit],
  );

  const activeGame = GAMES.find((g) => g.id === active);

  return (
    <CasinoProvider ctx={ctx}>
      <div className="mx-auto max-w-4xl space-y-4">
        <style>{`
          @keyframes floor-shimmer { 0%, 100% { opacity: 0.65; } 50% { opacity: 1; } }
          .floor-card { position: relative; overflow: hidden; border-radius: 14px; padding: 1.25rem 1rem; text-align: left; color: #fff; transition: transform 150ms, box-shadow 150ms; cursor: pointer; border: 1px solid rgba(255,255,255,0.14); width: 100%; }
          .floor-card:hover { transform: translateY(-3px) scale(1.015); box-shadow: 0 12px 40px rgba(0,0,0,0.45); }
          .floor-card:hover .floor-glyph { transform: scale(1.18) rotate(-6deg); }
          .floor-glyph { font-size: 2.4rem; transition: transform 200ms; display: inline-block; filter: drop-shadow(0 4px 10px rgba(0,0,0,0.4)); }
          .floor-marquee { animation: floor-shimmer 2.8s ease-in-out infinite; }
        `}</style>

        <div>
          <h1 className="flex items-center gap-2 text-2xl font-bold">
            <Dices className="h-6 w-6" />
            Quota Casino
          </h1>
          <p className="text-muted-foreground text-sm">
            Seven ways to bend your 24-hour budget. Wins raise your limit,
            losses lower it, everything ages out after 24 hours. You found this
            page; that&rsquo;s on you.
          </p>
        </div>

        <Card>
          <CardContent className="pt-6">
            {quotaError ? (
              <p className="text-destructive text-sm">Could not load your quota.</p>
            ) : casinoClosed ? (
              <p className="text-muted-foreground text-sm">
                The casino is still being built (schema migrating). Come back shortly.
              </p>
            ) : (
              <div className="space-y-4">
                <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
                  <div>
                    <p className="text-muted-foreground text-xs">Bankroll</p>
                    <p className="text-lg font-semibold">{quota ? formatDollars(remaining) : "…"}</p>
                  </div>
                  <div>
                    <p className="text-muted-foreground text-xs">24h limit</p>
                    <p className="text-lg font-semibold">{quota ? formatDollars(quota.limit_usd) : "…"}</p>
                  </div>
                  <div>
                    <p className="text-muted-foreground text-xs">Used</p>
                    <p className="text-lg font-semibold">{quota ? formatDollars(quota.used_usd) : "…"}</p>
                  </div>
                  <div>
                    <p className="text-muted-foreground text-xs">Gamble net (24h)</p>
                    <p
                      className={`text-lg font-semibold ${
                        (history?.net_usd ?? 0) < 0 ? "text-red-400" : (history?.net_usd ?? 0) > 0 ? "text-green-400" : ""
                      }`}
                    >
                      {formatDollars(history?.net_usd ?? 0)}
                    </p>
                  </div>
                </div>

                <div className="grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-4">
                  {GAMES.map((game) => (
                    <button
                      key={game.id}
                      className="floor-card"
                      style={{ background: game.gradient }}
                      onClick={() => {
                        audio.unlock();
                        audio.sfx("click");
                        setSessionNet(0);
                        setActive(game.id);
                      }}
                    >
                      <span className="floor-glyph">{game.glyph}</span>
                      <span className="floor-marquee mt-2 block text-sm font-bold tracking-wide uppercase">
                        {game.name}
                      </span>
                      <span className="block text-xs opacity-80">{game.blurb}</span>
                    </button>
                  ))}
                </div>

                {quota && quota.enforced === false ? (
                  <p className="text-muted-foreground text-xs">
                    Quota isn&rsquo;t enforced right now, so you&rsquo;re gambling for honor.
                  </p>
                ) : null}
              </div>
            )}
          </CardContent>
        </Card>

        <Card>
          <CardHeader>
            <CardTitle className="flex items-center gap-2 text-base">
              <History className="h-4 w-4" />
              Last 24 hours
            </CardTitle>
          </CardHeader>
          <CardContent>
            {casinoClosed ? (
              <p className="text-muted-foreground text-sm">No table, no tab.</p>
            ) : historyError ? (
              <p className="text-muted-foreground text-sm">Could not load your bets.</p>
            ) : !history ? (
              <p className="text-muted-foreground text-sm">Loading bets…</p>
            ) : history.items.length === 0 ? (
              <p className="text-muted-foreground text-sm">No bets yet. The floor is open.</p>
            ) : (
              <ul className="divide-border divide-y">
                {history.items.map((item) => (
                  <li key={item.id} className="flex items-center justify-between py-2 text-sm">
                    <span className="flex items-center gap-2">
                      <Badge variant={item.net_usd > 0 ? "success" : item.net_usd === 0 ? "secondary" : "failed"}>
                        {item.net_usd > 0 ? "WIN" : item.net_usd === 0 ? "PUSH" : "LOSS"}
                      </Badge>
                      <span className="text-muted-foreground">
                        {GAME_NAMES[item.game] ?? item.game} · {formatDollars(item.wager_usd)}
                        {item.multiplier !== null ? ` @ ${item.multiplier}x` : ""}
                      </span>
                    </span>
                    <span className="flex items-center gap-3">
                      <span className={item.net_usd > 0 ? "font-medium text-green-400" : item.net_usd < 0 ? "font-medium text-red-400" : "font-medium"}>
                        {item.net_usd > 0 ? "+" : ""}
                        {formatDollars(item.net_usd)}
                      </span>
                      <span className="text-muted-foreground text-xs">
                        {new Date(item.created_at).toLocaleTimeString()}
                      </span>
                    </span>
                  </li>
                ))}
              </ul>
            )}
          </CardContent>
        </Card>
      </div>

      {activeGame ? <activeGame.Component /> : null}
    </CasinoProvider>
  );
}
