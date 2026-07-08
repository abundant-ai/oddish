"use client";

import { useEffect, useRef, useState } from "react";
import useSWR from "swr";
import { fetcher } from "@/lib/api";
import type { QuotaDuel, QuotaDuelAccept, QuotaDuelList } from "@/lib/types";
import {
  CinemaShell,
  WagerPanel,
  errorMessage,
  formatDollars,
  parseWager,
  useCasino,
  type SfxName,
} from "../casino-kit";
import WrestleLocal from "./wrestle-local";

const sleep = (ms: number) => new Promise<void>((r) => setTimeout(r, ms));

const mulberry32 = (seed: number) => {
  let a = seed >>> 0;
  return () => {
    a = (a + 0x6d2b79f5) | 0;
    let t = Math.imul(a ^ (a >>> 15), 1 | a);
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
};

type Pose = { x: number; y: number; rot: number; sq: number };
type FrameExtra = { sfx?: SfxName[]; label?: string; ko?: boolean };
type Frame = { b: Pose; r: Pose; dur: number } & FrameExtra;
type Script = { frames: Frame[]; koIndex: number };

const homePose = (s: number): Pose => ({ x: s * 118, y: 0, rot: -s * 8, sq: 1 });

const SPRING_LINES = ["SPRING!", "BOING!", "FULL EXTENSION!", "AIRBORNE ENGINEER!"];
const BUMP_LINES = ["MID-AIR COLLISION!", "BELLIES CLAP LIKE THUNDER!", "NOBODY WINS THAT EXCHANGE!"];
const GRAZE_LINES = ["A HEAD GRAZES THE CANVAS…", "MILLIMETERS FROM DISASTER…"];
const RECOVER_LINES = ["…AND SOMEHOW UP!", "…THE SPRING FORGIVES!"];

// Pure replay: every beat comes off the server seed, KO forced to winner_role.
function buildScript(seed: number, blueWins: boolean): Script {
  const rand = mulberry32(seed);
  const frames: Frame[] = [];
  const F = (b: Partial<Pose>, r: Partial<Pose>, dur: number, extra: FrameExtra = {}) =>
    frames.push({ b: { ...homePose(-1), ...b }, r: { ...homePose(1), ...r }, dur, ...extra });
  const pick = (lines: string[]) => lines[Math.floor(rand() * lines.length)];

  const n = 7 + Math.floor(rand() * 5);
  for (let i = 0; i < n; i++) {
    const roll = rand();
    if (roll < 0.38) {
      const s = rand() < 0.5 ? -1 : 1;
      const solo = (p: Partial<Pose>, dur: number, extra: FrameExtra = {}) =>
        s < 0 ? F(p, {}, dur, extra) : F({}, p, dur, extra);
      solo({ sq: 0.7, rot: -s * 5 }, 220);
      solo(
        { x: s * (26 + rand() * 42), y: 95 + rand() * 70, rot: -s * (50 + rand() * 85), sq: 1.06 },
        410,
        { sfx: ["whoosh"], label: pick(SPRING_LINES) },
      );
      solo({ sq: 0.78 }, 290);
    } else if (roll < 0.68) {
      const h = 95 + rand() * 55;
      F({ sq: 0.72 }, { sq: 0.72 }, 220);
      F(
        { x: -26, y: h, rot: 32 + rand() * 24, sq: 1.05 },
        { x: 26, y: h + (rand() - 0.5) * 26, rot: -(32 + rand() * 24), sq: 1.05 },
        380,
        { sfx: ["whoosh"] },
      );
      F(
        { x: -(85 + rand() * 42), y: 34, rot: -(45 + rand() * 55) },
        { x: 85 + rand() * 42, y: 34, rot: 45 + rand() * 55 },
        330,
        { sfx: ["bounce"], label: pick(BUMP_LINES) },
      );
      F({ sq: 0.8 }, { sq: 0.8 }, 300);
    } else if (roll < 0.85) {
      const sv = rand() < 0.5 ? -1 : 1;
      const vic = (p: Partial<Pose>, dur: number, extra: FrameExtra = {}) =>
        sv < 0 ? F(p, {}, dur, extra) : F({}, p, dur, extra);
      vic({ x: sv * (95 + rand() * 25), y: 4, rot: sv * (96 + rand() * 16) }, 880, {
        sfx: ["tick"],
        label: pick(GRAZE_LINES),
      });
      vic({ x: sv * 100, y: 36, rot: -sv * 22, sq: 1.06 }, 360, {
        sfx: ["bounce"],
        label: pick(RECOVER_LINES),
      });
      vic({}, 270);
    } else {
      F({ rot: -16 + rand() * 8, sq: 0.9 }, { rot: 16 - rand() * 8, sq: 0.9 }, 270, {
        sfx: ["tick"],
        label: "JELLY LEGS.",
      });
      F({}, {}, 270);
    }
  }

  const ls = blueWins ? 1 : -1;
  const wsg = -ls;
  const KO = (w: Partial<Pose>, l: Partial<Pose>, dur: number, extra: FrameExtra = {}) =>
    wsg < 0 ? F(w, l, dur, extra) : F(l, w, dur, extra);
  const koIndex = frames.length;
  KO({ sq: 0.6, rot: -wsg * 6 }, {}, 340, { label: "THE FINISHER…" });
  KO({ x: wsg * 10, y: 175, rot: -wsg * 200, sq: 1.08 }, {}, 430, { sfx: ["whoosh"] });
  KO(
    { x: wsg * 46, y: 26, rot: -wsg * 4, sq: 0.85 },
    { x: ls * 148, y: 95, rot: ls * 120 },
    330,
    { sfx: ["bounce"], label: "CLEAN HIT!" },
  );
  KO({ x: wsg * 60, y: 0 }, { x: ls * 150, y: 125, rot: ls * 185 }, 560, {
    sfx: ["tick"],
    label: "UPSIDE DOWN…",
  });
  KO({ x: wsg * 60, y: 0 }, { x: ls * 150, y: -10, rot: ls * 180, sq: 0.6 }, 240, {
    sfx: ["explode"],
    ko: true,
  });
  KO({ x: wsg * 60, y: 70, sq: 1.06 }, { x: ls * 150, y: -10, rot: ls * 180, sq: 0.6 }, 420);
  KO({ x: wsg * 60, y: 0 }, { x: ls * 150, y: -10, rot: ls * 180, sq: 0.6 }, 360);
  return { frames, koIndex };
}

type FightState = {
  rival: string;
  wager: number;
  net: number;
  win: boolean;
  script: Script;
};

const INTRO_MS = 1600;
const EASE = "cubic-bezier(0.35, 1.25, 0.4, 1)";

function FighterSprite({
  corner,
  pose,
  dur,
  dead,
}: {
  corner: "blue" | "red";
  pose: Pose;
  dur: number;
  dead: boolean;
}) {
  return (
    <div
      className="ws-fighter"
      style={{
        transform: `translateX(calc(-50% + ${pose.x}px)) translateY(${-pose.y}px)`,
        transition: `transform ${dur}ms ${EASE}`,
      }}
    >
      <div
        className={`ws-blob ws-${corner}`}
        style={{
          transform: `rotate(${pose.rot}deg) scaleY(${pose.sq})`,
          transition: `transform ${dur}ms ${EASE}`,
        }}
      >
        <div className={`ws-face ${corner === "blue" ? "ws-face-r" : "ws-face-l"}`}>
          {dead ? (
            <span className="ws-eyes-dead">✕ ✕</span>
          ) : (
            <>
              <span className="ws-eye" />
              <span className="ws-eye" />
            </>
          )}
        </div>
        <div className="ws-mouth" />
        <div className={`ws-leg ws-leg-${corner}`} />
      </div>
    </div>
  );
}

export default function WrestleGame() {
  const ctx = useCasino();
  const { data: list, mutate: mutateDuels } = useSWR<QuotaDuelList>("/api/quotas/duels", fetcher, {
    refreshInterval: 15000,
    shouldRetryOnError: false,
  });

  const [mode, setMode] = useState<"local" | "duel">("local");
  const [fight, setFight] = useState<FightState | null>(null);
  const [phase, setPhase] = useState<"intro" | "live" | "done">("intro");
  const [poseB, setPoseB] = useState<Pose>(() => homePose(-1));
  const [poseR, setPoseR] = useState<Pose>(() => homePose(1));
  const [frameDur, setFrameDur] = useState(300);
  const [caption, setCaption] = useState<string | null>(null);
  const [koHit, setKoHit] = useState(false);

  const [opponentId, setOpponentId] = useState("");
  const [wager, setWager] = useState("5.00");
  const [creating, setCreating] = useState(false);
  const [pendingId, setPendingId] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const alive = useRef(true);
  const runId = useRef(0);
  const skipRef = useRef(false);
  useEffect(
    () => () => {
      alive.current = false;
    },
    [],
  );

  const post = async <T,>(url: string, body?: Record<string, unknown>): Promise<T> => {
    const res = await fetch(url, {
      method: "POST",
      credentials: "include",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body ?? {}),
    });
    let data: Record<string, unknown> = {};
    try {
      data = (await res.json()) as Record<string, unknown>;
    } catch {
      data = {};
    }
    if (!res.ok) {
      const msg =
        typeof data.detail === "string"
          ? data.detail
          : typeof data.error === "string"
            ? data.error
            : `Request failed (${res.status})`;
      throw new Error(msg);
    }
    return data as T;
  };

  const runFight = async (fs: FightState) => {
    const run = ++runId.current;
    const ok = () => alive.current && runId.current === run;
    skipRef.current = false;
    setKoHit(false);
    setCaption(null);
    setFrameDur(300);
    setPoseB(homePose(-1));
    setPoseR(homePose(1));
    setPhase("intro");
    await sleep(INTRO_MS);
    if (!ok()) return;
    setPhase("live");
    const { frames, koIndex } = fs.script;
    for (let i = 0; i < frames.length; i++) {
      if (!ok()) return;
      if (skipRef.current && i < koIndex) i = koIndex;
      const f = frames[i];
      setFrameDur(f.dur);
      setPoseB(f.b);
      setPoseR(f.r);
      if (f.label) setCaption(f.label);
      f.sfx?.forEach((s) => ctx.audio.sfx(s));
      if (f.ko) setKoHit(true);
      await sleep(f.dur + 60);
    }
    if (!ok()) return;
    setPhase("done");
    ctx.audio.sfx(fs.win ? (fs.net >= 25 ? "bigwin" : "win") : "lose");
  };

  const startFight = (d: QuotaDuel) => {
    if (!d.fight) return;
    const win = d.i_won ?? d.fight.winner_role === d.role;
    const fs: FightState = {
      rival: d.role === "challenger" ? d.opponent.label : d.challenger.label,
      wager: d.wager_usd,
      net: d.my_net_usd ?? (win ? d.wager_usd : -d.wager_usd),
      win,
      script: buildScript(d.fight.seed, win),
    };
    setFight(fs);
    void runFight(fs);
  };

  const acceptDuel = async (d: QuotaDuel) => {
    if (pendingId) return;
    ctx.audio.sfx("click");
    setPendingId(d.id);
    setError(null);
    try {
      const res = await post<QuotaDuelAccept>(`/api/quotas/duels/${d.id}/accept`);
      if (!alive.current) return;
      ctx.recordNet(res.duel.my_net_usd ?? 0);
      ctx.refresh();
      void mutateDuels();
      startFight(res.duel);
    } catch (err) {
      if (!alive.current) return;
      setError(errorMessage(err));
      void mutateDuels();
    } finally {
      if (alive.current) setPendingId(null);
    }
  };

  const dismissDuel = async (d: QuotaDuel) => {
    if (pendingId) return;
    ctx.audio.sfx("click");
    setPendingId(d.id);
    setError(null);
    try {
      await post<QuotaDuel>(`/api/quotas/duels/${d.id}/dismiss`);
    } catch (err) {
      if (alive.current) setError(errorMessage(err));
    } finally {
      if (alive.current) {
        setPendingId(null);
        void mutateDuels();
      }
    }
  };

  const createDuel = async () => {
    const amount = parseWager(wager, ctx.remaining);
    if (creating || !opponentId || amount === null) return;
    setCreating(true);
    setError(null);
    try {
      await post<QuotaDuel>("/api/quotas/duels", {
        opponent_user_id: opponentId,
        wager_usd: amount.toFixed(2),
      });
      if (!alive.current) return;
      ctx.audio.sfx("chip");
      void mutateDuels();
    } catch (err) {
      if (!alive.current) return;
      setError(errorMessage(err));
      void mutateDuels();
    } finally {
      if (alive.current) setCreating(false);
    }
  };

  const backToCard = () => {
    ctx.audio.sfx("click");
    runId.current++;
    setFight(null);
    setPhase("intro");
    setKoHit(false);
    void mutateDuels();
    ctx.refresh();
  };

  const amount = parseWager(wager, ctx.remaining);
  const members = list?.members ?? [];
  const deadBlue = koHit && fight !== null && !fight.win;
  const deadRed = koHit && fight !== null && fight.win;

  if (mode === "local") {
    return <WrestleLocal onSwitchToDuel={() => setMode("duel")} />;
  }

  return (
    <CinemaShell
      theme="wrestle"
      title="SPRING SLAM"
      tagline="Two engineers. One budget. Settle it with springs."
      hudExtra={
        <>
          <button
            className="cz-btn"
            style={{ fontSize: "0.72rem", padding: "0.35rem 0.7rem" }}
            onClick={() => {
              ctx.audio.sfx("click");
              setMode("local");
            }}
          >
            LOCAL MATCH
          </button>
          <span className="cz-chip">
            {list ? `${list.incoming.length} callout${list.incoming.length === 1 ? "" : "s"}` : "…"}
          </span>
        </>
      }
    >
      <style>{`
        .ws-ring {
          position: relative; overflow: hidden; border-radius: 20px;
          width: min(680px, 94vw); height: clamp(330px, 52vh, 440px);
          border: 1px solid color-mix(in srgb, var(--cz-accent2) 35%, transparent);
          background:
            radial-gradient(440px 210px at 50% 0%, rgba(250,204,21,0.14), transparent 70%),
            repeating-radial-gradient(circle at 50% -60%, rgba(148,163,184,0.09) 0 2px, transparent 2px 24px),
            linear-gradient(180deg, #0a0e24, #141b40);
          box-shadow: inset 0 0 70px rgba(0,0,0,0.6);
        }
        .ws-mat { position: absolute; inset: auto 0 0 0; height: 64px; background: linear-gradient(180deg, #2b3470, #171d45); border-top: 4px solid #cbd5e1; }
        .ws-rope { position: absolute; left: 3%; right: 3%; height: 3px; border-radius: 2px; background: linear-gradient(90deg, #f87171, #93c5fd); opacity: 0.75; }
        .ws-post { position: absolute; bottom: 58px; width: 10px; height: 128px; border-radius: 5px; }
        .ws-post-b { left: 2%; background: linear-gradient(180deg, #93c5fd, #1d4ed8); }
        .ws-post-r { right: 2%; background: linear-gradient(180deg, #fca5a5, #b91c1c); }
        .ws-plate {
          position: absolute; bottom: 10px; z-index: 20; max-width: 42%;
          font-family: var(--cz-font-display); font-size: 0.72rem; letter-spacing: 0.12em;
          padding: 2px 10px; border-radius: 6px; background: rgba(0,0,0,0.5);
          white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
        }
        .ws-plate-b { left: 10px; border: 1px solid #60a5fa; color: #93c5fd; }
        .ws-plate-r { right: 10px; border: 1px solid #f87171; color: #fca5a5; text-align: right; }
        .ws-fighter { position: absolute; left: 50%; bottom: 62px; z-index: 12; will-change: transform; }
        .ws-blob {
          position: relative; width: 76px; height: 76px; border-radius: 50%;
          transform-origin: 50% 88%;
          filter: drop-shadow(0 10px 12px rgba(0,0,0,0.55));
        }
        .ws-blue { background: radial-gradient(circle at 35% 28%, #bfdbfe, #2563eb 72%); border: 3px solid #60a5fa; }
        .ws-red { background: radial-gradient(circle at 65% 28%, #fecaca, #dc2626 72%); border: 3px solid #f87171; }
        .ws-face { position: absolute; top: 24px; display: flex; gap: 7px; align-items: center; }
        .ws-face-r { right: 12px; }
        .ws-face-l { left: 12px; }
        .ws-eye { width: 8px; height: 9px; border-radius: 50%; background: #0f172a; }
        .ws-eyes-dead { font-size: 13px; font-weight: 900; color: #0f172a; letter-spacing: 2px; line-height: 1; }
        .ws-mouth { position: absolute; top: 42px; left: 50%; transform: translateX(-50%); width: 13px; height: 6px; border-radius: 0 0 9px 9px; background: #0f172a; }
        .ws-leg { position: absolute; bottom: -28px; left: 50%; width: 13px; border-radius: 7px; transform-origin: top center; animation: ws-legspring 700ms ease-in-out infinite alternate; }
        .ws-leg-blue { background: linear-gradient(180deg, #1d4ed8, #172554); transform: translateX(-50%) rotate(-30deg); }
        .ws-leg-red { background: linear-gradient(180deg, #b91c1c, #450a0a); transform: translateX(-50%) rotate(30deg); }
        @keyframes ws-legspring { from { height: 36px; } to { height: 46px; } }
        .ws-caption {
          position: absolute; inset: 12px 0 auto 0; z-index: 25; text-align: center;
          font-family: var(--cz-font-display); font-size: clamp(0.9rem, 2.6vw, 1.2rem);
          letter-spacing: 0.08em; color: var(--cz-text); text-shadow: 0 2px 8px rgba(0,0,0,0.8);
        }
        .ws-ko-wrap { position: absolute; inset: 14% 0 auto 0; z-index: 40; text-align: center; pointer-events: none; }
        .ws-ko {
          display: inline-block; font-family: var(--cz-font-display); font-weight: 900;
          font-size: clamp(4.5rem, 15vw, 7.5rem); color: #facc15;
          text-shadow: 0 0 24px rgba(250,204,21,0.9), 0 6px 0 #92400e, 0 10px 30px rgba(0,0,0,0.8);
        }
        .ws-overlay {
          position: absolute; inset: 0; z-index: 30; display: flex; flex-direction: column;
          align-items: center; justify-content: center; gap: 0.6rem;
          background: rgba(4,6,22,0.55); text-align: center; padding: 1rem;
        }
        .ws-vs { font-family: var(--cz-font-display); font-size: clamp(1.2rem, 4vw, 1.9rem); letter-spacing: 0.08em; }
        .ws-skip { position: absolute; right: 10px; top: 10px; z-index: 35; font-size: 0.7rem; padding: 0.25rem 0.6rem; }
        .ws-poster {
          border: 2px solid color-mix(in srgb, var(--cz-accent) 55%, transparent);
          border-radius: 14px; padding: 0.9rem 1rem;
          background: linear-gradient(160deg, rgba(127,29,29,0.35), rgba(29,78,216,0.25));
          box-shadow: 0 8px 24px rgba(0,0,0,0.4), inset 0 1px 0 rgba(255,255,255,0.08);
        }
        .ws-poster-out { background: linear-gradient(160deg, rgba(29,78,216,0.28), rgba(15,23,42,0.4)); border-color: color-mix(in srgb, var(--cz-accent2) 55%, transparent); }
        .ws-accept { animation: ws-pulse 1s ease-in-out infinite alternate; }
        @keyframes ws-pulse {
          from { box-shadow: 0 0 10px color-mix(in srgb, var(--cz-accent) 45%, transparent); }
          to { box-shadow: 0 0 30px var(--cz-accent); transform: scale(1.04); }
        }
        .ws-section {
          font-family: var(--cz-font-display); font-size: 0.85rem; letter-spacing: 0.18em;
          color: var(--cz-dim); border-bottom: 1px solid color-mix(in srgb, var(--cz-text) 18%, transparent);
          padding-bottom: 0.3rem;
        }
        .ws-member { font-size: 0.82rem; padding: 0.3rem 0.7rem; }
        .ws-row { display: flex; align-items: center; justify-content: space-between; gap: 0.75rem; padding: 0.45rem 0; border-bottom: 1px dashed color-mix(in srgb, var(--cz-text) 14%, transparent); font-size: 0.85rem; }
        .ws-wl { font-family: var(--cz-font-display); width: 1.6rem; text-align: center; }
      `}</style>

      {fight ? (
        <div className="mx-auto flex min-h-full max-w-4xl flex-col items-center justify-center gap-4 px-4 py-8">
          <div className={`ws-ring ${phase === "done" && !fight.win ? "cz-shake" : ""}`}>
            <div className="ws-mat" />
            <div className="ws-rope" style={{ bottom: 100 }} />
            <div className="ws-rope" style={{ bottom: 130 }} />
            <div className="ws-rope" style={{ bottom: 160 }} />
            <div className="ws-post ws-post-b" />
            <div className="ws-post ws-post-r" />
            <div className="ws-plate ws-plate-b">YOU · BLUE CORNER</div>
            <div className="ws-plate ws-plate-r">{fight.rival} · RED CORNER</div>

            <FighterSprite corner="blue" pose={poseB} dur={frameDur} dead={deadBlue} />
            <FighterSprite corner="red" pose={poseR} dur={frameDur} dead={deadRed} />

            {phase === "live" && caption && !koHit ? (
              <div className="ws-caption cz-pop" key={caption}>
                {caption}
              </div>
            ) : null}
            {phase === "live" && !koHit ? (
              <button
                className="cz-btn ws-skip"
                onClick={() => {
                  ctx.audio.sfx("click");
                  skipRef.current = true;
                }}
              >
                SKIP TO KO
              </button>
            ) : null}

            {koHit ? (
              <>
                <div className="cz-flash" style={{ background: "rgba(250,204,21,0.7)" }} />
                <div className="ws-ko-wrap">
                  <span className="ws-ko cz-pop">KO!</span>
                </div>
              </>
            ) : null}

            {phase === "intro" ? (
              <div className="ws-overlay">
                <div className="cz-display text-xs tracking-[0.3em]" style={{ color: "var(--cz-dim)" }}>
                  TONIGHT — ONE FALL
                </div>
                <div className="ws-vs cz-display">
                  <span style={{ color: "#93c5fd" }}>YOU</span>
                  <span style={{ color: "var(--cz-dim)" }}> vs </span>
                  <span style={{ color: "#fca5a5" }}>{fight.rival.toUpperCase()}</span>
                </div>
                <div className="cz-display cz-glow text-lg" style={{ color: "var(--cz-accent)" }}>
                  {formatDollars(fight.wager)} ON THE LINE
                </div>
                <div className="text-xs" style={{ color: "var(--cz-dim)" }}>
                  TWO ENGINEERS. ONE BUDGET. SETTLE IT WITH SPRINGS.
                </div>
              </div>
            ) : null}

            {phase === "done" ? (
              <div className="ws-overlay">
                <div
                  className={`cz-display text-center text-xl font-bold md:text-2xl ${
                    fight.win ? "cz-glow cz-pop" : "cz-shake"
                  }`}
                  style={{ color: fight.win ? "var(--cz-accent2)" : "#f87171" }}
                >
                  {fight.win
                    ? `+${formatDollars(fight.net)} RIPPED FROM ${fight.rival.toUpperCase()}'S BUDGET`
                    : `${fight.rival.toUpperCase()} BODIES YOU FOR ${formatDollars(fight.wager)}`}
                </div>
                <div className="text-xs" style={{ color: "var(--cz-dim)" }}>
                  50/50. Purely a matter of spring.
                </div>
                <div className="mt-2 flex gap-3">
                  <button className="cz-btn cz-btn-primary" onClick={backToCard}>
                    BACK TO THE CARD
                  </button>
                  <button
                    className="cz-btn"
                    onClick={() => {
                      ctx.audio.sfx("click");
                      void runFight(fight);
                    }}
                  >
                    WATCH AGAIN
                  </button>
                </div>
              </div>
            ) : null}
          </div>
          {phase === "live" ? (
            <div className="cz-display text-sm" style={{ color: "var(--cz-dim)" }}>
              {formatDollars(fight.wager)} ON THE LINE — LOSER BUYS THE TOKENS
            </div>
          ) : null}
        </div>
      ) : (
        <div className="mx-auto max-w-5xl space-y-6 px-4 py-8">
          <div className="text-center">
            <div className="cz-display cz-glow text-2xl font-bold" style={{ color: "var(--cz-accent)" }}>
              TONIGHT&rsquo;S CARD
            </div>
            <div className="text-sm" style={{ color: "var(--cz-dim)" }}>
              TWO ENGINEERS. ONE BUDGET. SETTLE IT WITH SPRINGS. — Odds: 50/50. Purely a matter of spring.
            </div>
          </div>

          {error ? <div className="text-center text-sm text-red-400">{error}</div> : null}
          {!list ? (
            <div className="cz-display text-center" style={{ color: "var(--cz-dim)" }}>
              Reading the fight card…
            </div>
          ) : (
            <div className="grid gap-6 md:grid-cols-2">
              <div className="space-y-6">
                <div className="space-y-3">
                  <div className="ws-section">YOU HAVE BEEN CALLED OUT</div>
                  {list.incoming.length === 0 ? (
                    <div className="text-sm" style={{ color: "var(--cz-dim)" }}>
                      Nobody dares. For now.
                    </div>
                  ) : (
                    list.incoming.map((d) => (
                      <div key={d.id} className="ws-poster">
                        <div className="cz-display text-lg font-bold" style={{ color: "var(--cz-accent)" }}>
                          {d.challenger.label.toUpperCase()} CALLS YOU OUT
                        </div>
                        <div className="mb-3 text-sm" style={{ color: "var(--cz-dim)" }}>
                          {formatDollars(d.wager_usd)} on the line. One fall. No excuses.
                        </div>
                        <div className="flex flex-wrap gap-2">
                          <button
                            className="cz-btn cz-btn-primary ws-accept"
                            disabled={pendingId !== null}
                            onClick={() => void acceptDuel(d)}
                          >
                            {pendingId === d.id ? "LACING UP…" : "ACCEPT"}
                          </button>
                          <button
                            className="cz-btn"
                            disabled={pendingId !== null}
                            onClick={() => void dismissDuel(d)}
                          >
                            DECLINE
                          </button>
                        </div>
                      </div>
                    ))
                  )}
                </div>

                <div className="space-y-3">
                  <div className="ws-section">YOUR OPEN CHALLENGES</div>
                  {list.outgoing.length === 0 ? (
                    <div className="text-sm" style={{ color: "var(--cz-dim)" }}>
                      No gloves thrown yet.
                    </div>
                  ) : (
                    list.outgoing.map((d) => (
                      <div key={d.id} className="ws-poster ws-poster-out">
                        <div className="cz-display font-bold" style={{ color: "var(--cz-accent2)" }}>
                          YOU CALLED OUT {d.opponent.label.toUpperCase()}
                        </div>
                        <div className="mb-2 text-sm" style={{ color: "var(--cz-dim)" }}>
                          {formatDollars(d.wager_usd)} · awaiting an answer…
                        </div>
                        <button
                          className="cz-btn"
                          disabled={pendingId !== null}
                          onClick={() => void dismissDuel(d)}
                        >
                          {pendingId === d.id ? "…" : "CANCEL"}
                        </button>
                      </div>
                    ))
                  )}
                </div>
              </div>

              <div className="space-y-6">
                <div className="space-y-3">
                  <div className="ws-section">CALL SOMEONE OUT</div>
                  {members.length === 0 ? (
                    <div className="text-sm" style={{ color: "var(--cz-dim)" }}>
                      No coworkers to fight. Invite someone.
                    </div>
                  ) : (
                    <>
                      <div className="flex max-h-36 flex-wrap gap-2 overflow-y-auto">
                        {members.map((m) => (
                          <button
                            key={m.user_id}
                            className={`cz-btn ws-member ${opponentId === m.user_id ? "cz-btn-primary" : ""}`}
                            disabled={creating}
                            onClick={() => {
                              ctx.audio.sfx("click");
                              setOpponentId(m.user_id);
                            }}
                          >
                            {m.label}
                          </button>
                        ))}
                      </div>
                      <WagerPanel value={wager} onChange={setWager} disabled={creating} />
                      <button
                        className="cz-btn cz-btn-primary w-full"
                        disabled={creating || !opponentId || amount === null}
                        onClick={() => void createDuel()}
                      >
                        {creating
                          ? "THROWING…"
                          : !opponentId
                            ? "PICK A VICTIM"
                            : amount === null
                              ? "ENTER A WAGER"
                              : `THROW THE GLOVE — ${formatDollars(amount)}`}
                      </button>
                      <div className="text-center text-xs" style={{ color: "var(--cz-dim)" }}>
                        Winner takes the stake from the loser&rsquo;s 24h budget. 50/50. Purely a matter of spring.
                      </div>
                    </>
                  )}
                </div>

                <div className="space-y-3">
                  <div className="ws-section">RECENT RESULTS</div>
                  {list.recent.length === 0 ? (
                    <div className="text-sm" style={{ color: "var(--cz-dim)" }}>
                      The record books are empty. Cowardice, probably.
                    </div>
                  ) : (
                    <div className="max-h-72 overflow-y-auto">
                      {list.recent.map((d) => {
                        const rival = d.role === "challenger" ? d.opponent.label : d.challenger.label;
                        const won = d.i_won === true;
                        const net = d.my_net_usd ?? 0;
                        return (
                          <div key={d.id} className="ws-row">
                            <span className="flex min-w-0 items-center gap-2">
                              <span className="ws-wl" style={{ color: won ? "#4ade80" : "#f87171" }}>
                                {won ? "W" : "L"}
                              </span>
                              <span className="truncate" style={{ color: "var(--cz-dim)" }}>
                                vs {rival}
                              </span>
                            </span>
                            <span className="flex shrink-0 items-center gap-3">
                              <span style={{ color: net >= 0 ? "#4ade80" : "#f87171" }}>
                                {net >= 0 ? "+" : ""}
                                {formatDollars(net)}
                              </span>
                              {d.fight ? (
                                <button
                                  className="cz-btn ws-member"
                                  onClick={() => {
                                    ctx.audio.sfx("click");
                                    startFight(d);
                                  }}
                                >
                                  WATCH REPLAY
                                </button>
                              ) : null}
                            </span>
                          </div>
                        );
                      })}
                    </div>
                  )}
                </div>
              </div>
            </div>
          )}
        </div>
      )}
    </CinemaShell>
  );
}
