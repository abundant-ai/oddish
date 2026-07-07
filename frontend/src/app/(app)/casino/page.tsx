"use client";

import { useState } from "react";
import useSWR from "swr";
import {
  Coins,
  Dices,
  History,
  TrendingDown,
  TrendingUp,
} from "lucide-react";
import { fetcher } from "@/lib/api";
import type {
  QuotaGambleList,
  QuotaGambleResult,
  QuotaUsage,
} from "@/lib/types";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Card,
  CardContent,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { Input } from "@/components/ui/input";

const formatDollars = (value: number) =>
  value.toLocaleString(undefined, {
    style: "currency",
    currency: "USD",
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  });

const MIN_SPIN_MS = 1400;
const PRESETS = [1, 5, 10, 25];

type Side = "heads" | "tails";

export default function CasinoPage() {
  const {
    data: quota,
    error: quotaError,
    mutate: mutateQuota,
  } = useSWR<QuotaUsage>("/api/quotas/me", fetcher);
  const {
    data: history,
    error: historyError,
    mutate: mutateHistory,
  } = useSWR<QuotaGambleList>("/api/quotas/gambles", fetcher);

  const [side, setSide] = useState<Side>("heads");
  const [wagerInput, setWagerInput] = useState("5");
  const [spinning, setSpinning] = useState(false);
  const [lastFlip, setLastFlip] = useState<QuotaGambleResult | null>(null);
  const [flipError, setFlipError] = useState<string | null>(null);

  const limit = quota?.limit_usd ?? 0;
  const used = quota?.used_usd ?? 0;
  const reserved = quota?.reserved_usd ?? 0;
  const remaining = Math.max(0, limit - used - reserved);
  const wager = Number.parseFloat(wagerInput);
  const wagerValid =
    Number.isFinite(wager) && wager > 0 && wager <= remaining + 1e-9;
  const casinoClosed = (historyError as { status?: number })?.status === 503;
  const tappedOut = !!quota && remaining <= 0;
  const shownFace: Side = spinning
    ? side
    : ((lastFlip?.result ?? "heads") as Side);

  const setWager = (value: number) =>
    setWagerInput((Math.floor(value * 100) / 100).toFixed(2));

  const flip = async () => {
    if (spinning || !wagerValid) return;
    setSpinning(true);
    setLastFlip(null);
    setFlipError(null);
    const started = Date.now();
    try {
      const res = await fetch("/api/quotas/gamble", {
        method: "POST",
        credentials: "include",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ wager_usd: wager.toFixed(2), side }),
      });
      const data = await res.json();
      if (!res.ok) {
        throw new Error(
          data.detail || data.error || `Flip failed (${res.status})`,
        );
      }
      const elapsed = Date.now() - started;
      await new Promise((r) => setTimeout(r, Math.max(0, MIN_SPIN_MS - elapsed)));
      setLastFlip(data as QuotaGambleResult);
      void mutateQuota();
      void mutateHistory();
    } catch (err) {
      setFlipError(err instanceof Error ? err.message : "Flip failed");
    } finally {
      setSpinning(false);
    }
  };

  return (
    <div className="mx-auto max-w-3xl space-y-4">
      <style>{`
        .qc-stage { perspective: 900px; }
        .qc-coin {
          position: relative;
          transform-style: preserve-3d;
          transition: transform 700ms cubic-bezier(0.2, 0.7, 0.3, 1.1);
        }
        .qc-coin.qc-spinning { animation: qc-spin 450ms linear infinite; }
        .qc-face {
          position: absolute;
          inset: 0;
          display: flex;
          align-items: center;
          justify-content: center;
          border-radius: 9999px;
          backface-visibility: hidden;
        }
        .qc-tails-face { transform: rotateY(180deg); }
        @keyframes qc-spin {
          from { transform: rotateY(0deg); }
          to { transform: rotateY(360deg); }
        }
      `}</style>

      <div>
        <h1 className="flex items-center gap-2 text-2xl font-bold">
          <Dices className="h-6 w-6" />
          Quota Casino
        </h1>
        <p className="text-muted-foreground text-sm">
          Double or nothing on your 24-hour budget. Wins raise your limit,
          losses lower it, and both age out of the window after 24 hours. You
          found this page; that&rsquo;s on you.
        </p>
      </div>

      <Card>
        <CardHeader>
          <CardTitle className="flex items-center gap-2 text-base">
            <Coins className="h-4 w-4" />
            Bankroll
          </CardTitle>
        </CardHeader>
        <CardContent className="space-y-6">
          {quotaError ? (
            <p className="text-destructive text-sm">
              Could not load your quota.
            </p>
          ) : !quota ? (
            <p className="text-muted-foreground text-sm">Counting chips…</p>
          ) : (
            <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
              <div>
                <p className="text-muted-foreground text-xs">To gamble</p>
                <p className="text-lg font-semibold">
                  {formatDollars(remaining)}
                </p>
              </div>
              <div>
                <p className="text-muted-foreground text-xs">24h limit</p>
                <p className="text-lg font-semibold">{formatDollars(limit)}</p>
              </div>
              <div>
                <p className="text-muted-foreground text-xs">Used</p>
                <p className="text-lg font-semibold">{formatDollars(used)}</p>
              </div>
              <div>
                <p className="text-muted-foreground text-xs">Flip net (24h)</p>
                <p
                  className={`text-lg font-semibold ${
                    (history?.net_usd ?? 0) < 0
                      ? "text-red-400"
                      : (history?.net_usd ?? 0) > 0
                        ? "text-green-400"
                        : ""
                  }`}
                >
                  {formatDollars(history?.net_usd ?? 0)}
                </p>
              </div>
            </div>
          )}

          <div className="qc-stage flex justify-center py-2">
            <div
              className={`qc-coin h-32 w-32 ${spinning ? "qc-spinning" : ""}`}
              style={{
                transform: spinning
                  ? undefined
                  : `rotateY(${shownFace === "tails" ? 180 : 0}deg)`,
              }}
            >
              <div className="qc-face border-4 border-amber-300 bg-gradient-to-br from-amber-200 to-amber-500 text-3xl font-black text-amber-900">
                H
              </div>
              <div className="qc-face qc-tails-face border-4 border-slate-300 bg-gradient-to-br from-slate-200 to-slate-500 text-3xl font-black text-slate-800">
                T
              </div>
            </div>
          </div>

          {lastFlip ? (
            <div
              className={`flex items-center justify-center gap-2 rounded-md border p-3 text-sm font-medium ${
                lastFlip.won
                  ? "border-green-500/30 bg-green-500/10 text-green-400"
                  : "border-red-500/30 bg-red-500/10 text-red-400"
              }`}
            >
              {lastFlip.won ? (
                <TrendingUp className="h-4 w-4" />
              ) : (
                <TrendingDown className="h-4 w-4" />
              )}
              {lastFlip.result} — you called {lastFlip.side}.{" "}
              {lastFlip.won
                ? `The house weeps: +${formatDollars(lastFlip.wager_usd)} of limit for 24h.`
                : `The house thanks you: ${formatDollars(lastFlip.net_usd)} of limit for 24h.`}
            </div>
          ) : null}

          {flipError ? (
            <p className="text-destructive text-center text-sm">{flipError}</p>
          ) : null}

          {casinoClosed ? (
            <p className="text-muted-foreground text-center text-sm">
              The casino is still being built (schema migrating). Come back
              shortly.
            </p>
          ) : tappedOut ? (
            <p className="text-muted-foreground text-center text-sm">
              You&rsquo;re tapped out. Come back when older spend and losses
              age out of your 24-hour window.
            </p>
          ) : (
            <div className="space-y-3">
              <div className="flex justify-center gap-2">
                {(["heads", "tails"] as const).map((s) => (
                  <Button
                    key={s}
                    variant={side === s ? "default" : "outline"}
                    size="sm"
                    disabled={spinning}
                    onClick={() => setSide(s)}
                  >
                    {s === "heads" ? "Heads" : "Tails"}
                  </Button>
                ))}
              </div>

              <div className="flex flex-wrap items-center justify-center gap-2">
                <Input
                  value={wagerInput}
                  onChange={(e) => setWagerInput(e.target.value)}
                  inputMode="decimal"
                  disabled={spinning}
                  className="w-28 text-right"
                  aria-label="Wager in dollars"
                />
                {PRESETS.map((p) => (
                  <Button
                    key={p}
                    variant="outline"
                    size="sm"
                    disabled={spinning || p > remaining}
                    onClick={() => setWager(p)}
                  >
                    ${p}
                  </Button>
                ))}
                <Button
                  variant="outline"
                  size="sm"
                  disabled={spinning || remaining <= 0}
                  onClick={() => setWager(remaining / 2)}
                >
                  Half
                </Button>
                <Button
                  variant="outline"
                  size="sm"
                  disabled={spinning || remaining <= 0}
                  onClick={() => setWager(remaining)}
                >
                  Max
                </Button>
              </div>

              <div className="flex justify-center">
                <Button
                  size="lg"
                  className="w-48"
                  disabled={spinning || !quota || !wagerValid}
                  onClick={flip}
                >
                  {spinning
                    ? "Flipping…"
                    : wagerValid
                      ? `Flip for ${formatDollars(wager)}`
                      : "Enter a valid wager"}
                </Button>
              </div>

              {quota && quota.enforced === false ? (
                <p className="text-muted-foreground text-center text-xs">
                  Quota isn&rsquo;t enforced right now, so you&rsquo;re
                  flipping for honor.
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
            <p className="text-muted-foreground text-sm">
              Could not load your flips.
            </p>
          ) : !history ? (
            <p className="text-muted-foreground text-sm">Loading flips…</p>
          ) : history.items.length === 0 ? (
            <p className="text-muted-foreground text-sm">
              No flips yet. The coin is patient.
            </p>
          ) : (
            <ul className="divide-border divide-y">
              {history.items.map((item) => (
                <li
                  key={item.id}
                  className="flex items-center justify-between py-2 text-sm"
                >
                  <span className="flex items-center gap-2">
                    <Badge variant={item.won ? "success" : "failed"}>
                      {item.won ? "WIN" : "LOSS"}
                    </Badge>
                    <span className="text-muted-foreground">
                      wagered {formatDollars(item.wager_usd)}
                    </span>
                  </span>
                  <span className="flex items-center gap-3">
                    <span
                      className={
                        item.won
                          ? "font-medium text-green-400"
                          : "font-medium text-red-400"
                      }
                    >
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
  );
}
