"use client";

import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from "react";
import { createPortal } from "react-dom";
import { Volume2, VolumeX, X } from "lucide-react";
import type {
  BlackjackState,
  QuotaGambleList,
  QuotaGambleResult,
  QuotaUsage,
} from "@/lib/types";

export type CasinoGameId =
  | "coinflip"
  | "plinko"
  | "slots"
  | "blackjack"
  | "baccarat"
  | "rocket"
  | "sling"
  | "wrestle";

export const formatDollars = (value: number) =>
  value.toLocaleString(undefined, {
    style: "currency",
    currency: "USD",
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  });

// ---------------------------------------------------------------------------
// Audio: one WebAudio synth engine — generative music loops + SFX, no assets.
// ---------------------------------------------------------------------------

export type SfxName =
  | "click"
  | "chip"
  | "flip"
  | "card"
  | "win"
  | "bigwin"
  | "lose"
  | "push"
  | "tick"
  | "launch"
  | "explode"
  | "bounce"
  | "snap"
  | "whoosh"
  | "cashout";

type MusicSpec = {
  bpm: number;
  bass: number[];
  arp: number[];
  bassWave: OscillatorType;
  arpWave: OscillatorType;
  hat?: boolean;
  kick?: boolean;
  drone?: number;
};

const MUSIC: Record<CasinoGameId, MusicSpec> = {
  coinflip: { bpm: 96, bass: [45, 45, 52, 50], arp: [69, 72, 76, 72], bassWave: "triangle", arpWave: "sine", hat: true },
  plinko: { bpm: 118, bass: [43, 43, 48, 46], arp: [67, 71, 74, 79, 74, 71], bassWave: "square", arpWave: "square", hat: true, kick: true },
  slots: { bpm: 128, bass: [41, 41, 44, 46], arp: [65, 69, 72, 77, 72, 69], bassWave: "sawtooth", arpWave: "square", hat: true, kick: true },
  blackjack: { bpm: 74, bass: [38, 41, 43, 41], arp: [62, 65, 69, 72], bassWave: "sine", arpWave: "triangle" },
  baccarat: { bpm: 66, bass: [36, 43, 41, 43], arp: [60, 64, 67, 71], bassWave: "sine", arpWave: "sine", drone: 36 },
  rocket: { bpm: 140, bass: [40, 40, 40, 42], arp: [64, 67, 71, 74, 76, 74, 71, 67], bassWave: "sawtooth", arpWave: "sawtooth", hat: true, kick: true },
  sling: { bpm: 108, bass: [45, 48, 45, 43], arp: [69, 74, 76, 81], bassWave: "triangle", arpWave: "square", kick: true },
  wrestle: { bpm: 132, bass: [40, 40, 47, 45], arp: [64, 67, 71, 67, 64, 71], bassWave: "square", arpWave: "square", hat: true, kick: true },
};

const midiHz = (m: number) => 440 * Math.pow(2, (m - 69) / 12);

class SynthEngine {
  ctx: AudioContext;
  master: GainNode;
  musicBus: GainNode;
  sfxBus: GainNode;
  private step = 0;
  private timer: ReturnType<typeof setInterval> | null = null;
  private droneOsc: OscillatorNode | null = null;
  private noiseBuf: AudioBuffer;

  constructor() {
    this.ctx = new AudioContext();
    this.master = this.ctx.createGain();
    this.master.gain.value = 1;
    this.master.connect(this.ctx.destination);
    this.musicBus = this.ctx.createGain();
    this.musicBus.gain.value = 0.05;
    this.musicBus.connect(this.master);
    this.sfxBus = this.ctx.createGain();
    this.sfxBus.gain.value = 0.14;
    this.sfxBus.connect(this.master);
    this.noiseBuf = this.ctx.createBuffer(1, this.ctx.sampleRate, this.ctx.sampleRate);
    const data = this.noiseBuf.getChannelData(0);
    for (let i = 0; i < data.length; i++) data[i] = Math.random() * 2 - 1;
  }

  setMuted(muted: boolean) {
    this.master.gain.setTargetAtTime(muted ? 0 : 1, this.ctx.currentTime, 0.02);
  }

  private tone(
    bus: GainNode,
    freq: number,
    opts: {
      wave?: OscillatorType;
      dur?: number;
      at?: number;
      gain?: number;
      glideTo?: number;
    } = {},
  ) {
    const { wave = "sine", dur = 0.15, at = 0, gain = 1, glideTo } = opts;
    const t = this.ctx.currentTime + at;
    const osc = this.ctx.createOscillator();
    const g = this.ctx.createGain();
    osc.type = wave;
    osc.frequency.setValueAtTime(freq, t);
    if (glideTo !== undefined) osc.frequency.exponentialRampToValueAtTime(Math.max(1, glideTo), t + dur);
    g.gain.setValueAtTime(gain, t);
    g.gain.exponentialRampToValueAtTime(0.001, t + dur);
    osc.connect(g).connect(bus);
    osc.start(t);
    osc.stop(t + dur + 0.02);
  }

  private noise(
    bus: GainNode,
    opts: { dur?: number; at?: number; gain?: number; freq?: number; type?: BiquadFilterType } = {},
  ) {
    const { dur = 0.08, at = 0, gain = 0.6, freq = 4000, type = "highpass" } = opts;
    const t = this.ctx.currentTime + at;
    const src = this.ctx.createBufferSource();
    src.buffer = this.noiseBuf;
    src.loop = true;
    const f = this.ctx.createBiquadFilter();
    f.type = type;
    f.frequency.value = freq;
    const g = this.ctx.createGain();
    g.gain.setValueAtTime(gain, t);
    g.gain.exponentialRampToValueAtTime(0.001, t + dur);
    src.connect(f).connect(g).connect(bus);
    src.start(t);
    src.stop(t + dur + 0.02);
  }

  music(theme: CasinoGameId | null) {
    if (this.timer) clearInterval(this.timer);
    this.timer = null;
    this.droneOsc?.stop();
    this.droneOsc = null;
    if (!theme) return;
    const spec = MUSIC[theme];
    const stepDur = 60 / spec.bpm / 4;
    this.step = 0;
    if (spec.drone !== undefined) {
      const osc = this.ctx.createOscillator();
      const g = this.ctx.createGain();
      osc.type = "sine";
      osc.frequency.value = midiHz(spec.drone);
      g.gain.value = 0.4;
      osc.connect(g).connect(this.musicBus);
      osc.start();
      this.droneOsc = osc;
    }
    this.timer = setInterval(() => {
      const s = this.step++;
      if (s % 4 === 0) {
        const note = spec.bass[(s / 4) % spec.bass.length];
        this.tone(this.musicBus, midiHz(note), { wave: spec.bassWave, dur: stepDur * 3.2, gain: 0.9 });
        if (spec.kick && s % 8 === 0) this.tone(this.musicBus, 90, { wave: "sine", dur: 0.09, gain: 1.2, glideTo: 45 });
      }
      this.tone(this.musicBus, midiHz(spec.arp[s % spec.arp.length]), {
        wave: spec.arpWave,
        dur: stepDur * 0.9,
        gain: 0.35,
      });
      if (spec.hat && s % 2 === 1) this.noise(this.musicBus, { dur: 0.03, gain: 0.25, freq: 7000 });
    }, stepDur * 1000);
  }

  sfx(name: SfxName) {
    const b = this.sfxBus;
    switch (name) {
      case "click":
        return this.tone(b, 880, { wave: "square", dur: 0.05, gain: 0.5 });
      case "chip":
        return this.tone(b, 950, { wave: "sine", dur: 0.04, gain: 0.6 });
      case "tick":
        return this.tone(b, 1250, { wave: "sine", dur: 0.025, gain: 0.4 });
      case "flip":
        return this.tone(b, 300, { wave: "triangle", dur: 0.45, glideTo: 950, gain: 0.5 });
      case "whoosh":
        return this.noise(b, { dur: 0.35, gain: 0.4, freq: 900, type: "bandpass" });
      case "card":
        return this.noise(b, { dur: 0.05, gain: 0.5, freq: 3000 });
      case "snap":
        return this.tone(b, 700, { wave: "triangle", dur: 0.08, glideTo: 200, gain: 0.7 });
      case "bounce":
        return this.tone(b, 380 + Math.random() * 220, { wave: "sine", dur: 0.05, gain: 0.5 });
      case "launch":
        this.tone(b, 80, { wave: "sawtooth", dur: 0.9, glideTo: 420, gain: 0.6 });
        return this.noise(b, { dur: 0.9, gain: 0.25, freq: 600, type: "bandpass" });
      case "explode":
        this.tone(b, 70, { wave: "sine", dur: 0.5, glideTo: 30, gain: 1.2 });
        return this.noise(b, { dur: 0.6, gain: 0.9, freq: 900, type: "lowpass" });
      case "cashout":
        this.tone(b, 880, { dur: 0.08, gain: 0.7 });
        return this.tone(b, 1320, { dur: 0.12, at: 0.08, gain: 0.7 });
      case "win":
        [523, 659, 784].forEach((f, i) => this.tone(b, f, { dur: 0.14, at: i * 0.09, gain: 0.7 }));
        return;
      case "bigwin":
        [523, 659, 784, 1047, 1319, 1568].forEach((f, i) =>
          this.tone(b, f, { dur: 0.2, at: i * 0.11, gain: 0.8 }),
        );
        return this.noise(b, { dur: 1.1, gain: 0.15, freq: 8000 });
      case "lose":
        [330, 262, 208].forEach((f, i) =>
          this.tone(b, f, { wave: "sawtooth", dur: 0.18, at: i * 0.14, gain: 0.45 }),
        );
        return;
      case "push":
        return this.tone(b, 440, { wave: "triangle", dur: 0.25, gain: 0.5 });
    }
  }
}

export type CasinoAudio = {
  muted: boolean;
  toggleMuted: () => void;
  unlock: () => void;
  music: (theme: CasinoGameId | null) => void;
  sfx: (name: SfxName) => void;
};

export function useCasinoAudio(): CasinoAudio {
  const engineRef = useRef<SynthEngine | null>(null);
  const pendingTheme = useRef<CasinoGameId | null>(null);
  const [muted, setMuted] = useState(false);

  useEffect(() => {
    setMuted(localStorage.getItem("oddish-casino-muted") === "1");
    return () => {
      engineRef.current?.music(null);
      engineRef.current?.ctx.close().catch(() => {});
      engineRef.current = null;
    };
  }, []);

  const unlock = useCallback(() => {
    // Environments without (working) WebAudio just play silent.
    try {
      if (!engineRef.current) {
        engineRef.current = new SynthEngine();
        engineRef.current.setMuted(localStorage.getItem("oddish-casino-muted") === "1");
        if (pendingTheme.current) engineRef.current.music(pendingTheme.current);
      }
      engineRef.current.ctx.resume().catch(() => {});
    } catch {
      engineRef.current = null;
    }
  }, []);

  const music = useCallback((theme: CasinoGameId | null) => {
    pendingTheme.current = theme;
    engineRef.current?.music(theme);
  }, []);

  const sfx = useCallback((name: SfxName) => {
    engineRef.current?.sfx(name);
  }, []);

  const toggleMuted = useCallback(() => {
    setMuted((m) => {
      const next = !m;
      localStorage.setItem("oddish-casino-muted", next ? "1" : "0");
      engineRef.current?.setMuted(next);
      return next;
    });
  }, []);

  return useMemo(
    () => ({ muted, toggleMuted, unlock, music, sfx }),
    [muted, toggleMuted, unlock, music, sfx],
  );
}

// ---------------------------------------------------------------------------
// Game context: quota state + server round-trips, shared by every game scene.
// ---------------------------------------------------------------------------

export type CasinoCtx = {
  quota: QuotaUsage | undefined;
  history: QuotaGambleList | undefined;
  remaining: number;
  sessionNet: number;
  play: (body: Record<string, unknown>) => Promise<QuotaGambleResult>;
  playBlackjack: (body: Record<string, unknown>) => Promise<BlackjackState>;
  // For games that settle outside play()/playBlackjack() (duels): fold a
  // result into the session-net HUD chip.
  recordNet: (net: number) => void;
  refresh: () => void;
  audio: CasinoAudio;
  exit: () => void;
};

const CasinoContext = createContext<CasinoCtx | null>(null);

export function CasinoProvider({ ctx, children }: { ctx: CasinoCtx; children: ReactNode }) {
  return <CasinoContext.Provider value={ctx}>{children}</CasinoContext.Provider>;
}

export function useCasino(): CasinoCtx {
  const ctx = useContext(CasinoContext);
  if (!ctx) throw new Error("useCasino must be used inside CasinoProvider");
  return ctx;
}

// ---------------------------------------------------------------------------
// Cinema shell: full-screen takeover with per-game theme, HUD and music.
// ---------------------------------------------------------------------------

type Theme = {
  bg: string;
  layers: string;
  accent: string;
  accent2: string;
  text: string;
  dim: string;
  fontDisplay: string;
  fontBody: string;
};

export const THEMES: Record<CasinoGameId, Theme> = {
  coinflip: {
    bg: "#0b0e1a",
    layers:
      "radial-gradient(1200px 600px at 50% -10%, rgba(251,191,36,0.18), transparent 60%), radial-gradient(900px 500px at 80% 110%, rgba(56,189,248,0.12), transparent 60%)",
    accent: "#fbbf24",
    accent2: "#38bdf8",
    text: "#f8fafc",
    dim: "rgba(248,250,252,0.55)",
    fontDisplay: "'Trebuchet MS', 'Arial Black', sans-serif",
    fontBody: "'Trebuchet MS', sans-serif",
  },
  plinko: {
    bg: "#120b1e",
    layers:
      "radial-gradient(1000px 700px at 50% 0%, rgba(217,70,239,0.20), transparent 55%), radial-gradient(800px 600px at 15% 100%, rgba(34,211,238,0.14), transparent 60%)",
    accent: "#e879f9",
    accent2: "#22d3ee",
    text: "#fdf4ff",
    dim: "rgba(253,244,255,0.55)",
    fontDisplay: "'Arial Rounded MT Bold', 'Trebuchet MS', sans-serif",
    fontBody: "'Trebuchet MS', sans-serif",
  },
  slots: {
    bg: "#160a06",
    layers:
      "radial-gradient(1100px 600px at 50% -5%, rgba(239,68,68,0.22), transparent 60%), radial-gradient(900px 500px at 90% 105%, rgba(251,191,36,0.16), transparent 55%)",
    accent: "#f59e0b",
    accent2: "#ef4444",
    text: "#fffbeb",
    dim: "rgba(255,251,235,0.55)",
    fontDisplay: "'Impact', 'Arial Black', sans-serif",
    fontBody: "'Verdana', sans-serif",
  },
  blackjack: {
    bg: "#04140c",
    layers:
      "radial-gradient(1200px 700px at 50% 15%, rgba(16,185,129,0.16), transparent 60%), radial-gradient(700px 500px at 10% 110%, rgba(250,204,21,0.08), transparent 60%)",
    accent: "#34d399",
    accent2: "#facc15",
    text: "#ecfdf5",
    dim: "rgba(236,253,245,0.55)",
    fontDisplay: "'Georgia', serif",
    fontBody: "'Georgia', serif",
  },
  baccarat: {
    bg: "#140510",
    layers:
      "radial-gradient(1100px 650px at 50% -10%, rgba(190,24,93,0.22), transparent 60%), radial-gradient(900px 600px at 85% 110%, rgba(217,119,6,0.14), transparent 60%)",
    accent: "#f472b6",
    accent2: "#fbbf24",
    text: "#fdf2f8",
    dim: "rgba(253,242,248,0.6)",
    fontDisplay: "'Didot', 'Georgia', serif",
    fontBody: "'Georgia', serif",
  },
  rocket: {
    bg: "#030712",
    layers:
      "radial-gradient(1300px 800px at 50% 120%, rgba(249,115,22,0.18), transparent 55%), radial-gradient(900px 600px at 50% -20%, rgba(99,102,241,0.20), transparent 60%)",
    accent: "#fb923c",
    accent2: "#818cf8",
    text: "#f9fafb",
    dim: "rgba(249,250,251,0.55)",
    fontDisplay: "'Courier New', monospace",
    fontBody: "'Courier New', monospace",
  },
  sling: {
    bg: "#0a1405",
    layers:
      "radial-gradient(1100px 700px at 50% -10%, rgba(132,204,22,0.18), transparent 55%), radial-gradient(800px 600px at 90% 110%, rgba(245,158,11,0.14), transparent 60%)",
    accent: "#a3e635",
    accent2: "#fbbf24",
    text: "#f7fee7",
    dim: "rgba(247,254,231,0.55)",
    fontDisplay: "'Arial Rounded MT Bold', 'Trebuchet MS', sans-serif",
    fontBody: "'Trebuchet MS', sans-serif",
  },
  wrestle: {
    bg: "#0b0f2a",
    layers:
      "radial-gradient(900px 600px at 0% 100%, rgba(248,113,113,0.20), transparent 55%), radial-gradient(900px 600px at 100% 100%, rgba(96,165,250,0.20), transparent 55%), radial-gradient(1200px 500px at 50% -10%, rgba(250,204,21,0.10), transparent 60%)",
    accent: "#f87171",
    accent2: "#60a5fa",
    text: "#f8fafc",
    dim: "rgba(248,250,252,0.55)",
    fontDisplay: "'Impact', 'Arial Black', sans-serif",
    fontBody: "'Trebuchet MS', sans-serif",
  },
};

export function CinemaShell({
  theme,
  title,
  tagline,
  hudExtra,
  children,
}: {
  theme: CasinoGameId;
  title: string;
  tagline?: string;
  hudExtra?: ReactNode;
  children: ReactNode;
}) {
  const ctx = useCasino();
  const t = THEMES[theme];
  const [mounted, setMounted] = useState(false);

  useEffect(() => {
    setMounted(true);
    ctx.audio.music(theme);
    const prev = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") ctx.exit();
    };
    window.addEventListener("keydown", onKey);
    return () => {
      ctx.audio.music(null);
      document.body.style.overflow = prev;
      window.removeEventListener("keydown", onKey);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  if (!mounted) return null;

  return createPortal(
    <div
      className="cz-root fixed inset-0 z-[100] flex flex-col"
      style={
        {
          background: t.bg,
          color: t.text,
          fontFamily: t.fontBody,
          "--cz-accent": t.accent,
          "--cz-accent2": t.accent2,
          "--cz-text": t.text,
          "--cz-dim": t.dim,
          "--cz-font-display": t.fontDisplay,
        } as React.CSSProperties
      }
    >
      <style>{`
        .cz-root { animation: cz-enter 600ms cubic-bezier(0.2, 0.8, 0.2, 1); }
        @keyframes cz-enter { from { opacity: 0; transform: scale(1.04); } to { opacity: 1; transform: scale(1); } }
        .cz-root::after {
          content: ""; position: absolute; inset: 0; pointer-events: none;
          background: radial-gradient(ellipse at center, transparent 55%, rgba(0,0,0,0.55) 100%);
        }
        .cz-display { font-family: var(--cz-font-display); letter-spacing: 0.06em; }
        .cz-glow { text-shadow: 0 0 18px var(--cz-accent), 0 0 40px color-mix(in srgb, var(--cz-accent) 40%, transparent); }
        .cz-btn {
          border: 1px solid color-mix(in srgb, var(--cz-accent) 55%, transparent);
          background: color-mix(in srgb, var(--cz-accent) 14%, transparent);
          color: var(--cz-text); border-radius: 10px; padding: 0.5rem 1rem;
          font-family: var(--cz-font-display); letter-spacing: 0.05em;
          transition: transform 120ms, background 120ms, box-shadow 120ms; cursor: pointer;
        }
        .cz-btn:hover:not(:disabled) { background: color-mix(in srgb, var(--cz-accent) 28%, transparent); box-shadow: 0 0 18px color-mix(in srgb, var(--cz-accent) 45%, transparent); transform: translateY(-1px); }
        .cz-btn:active:not(:disabled) { transform: translateY(1px) scale(0.98); }
        .cz-btn:disabled { opacity: 0.35; cursor: not-allowed; }
        .cz-btn-primary { background: color-mix(in srgb, var(--cz-accent) 75%, black); border-color: var(--cz-accent); box-shadow: 0 0 22px color-mix(in srgb, var(--cz-accent) 40%, transparent); font-size: 1.05rem; padding: 0.7rem 1.6rem; }
        .cz-chip { border: 1px solid color-mix(in srgb, var(--cz-text) 25%, transparent); border-radius: 999px; padding: 0.2rem 0.75rem; font-size: 0.75rem; color: var(--cz-dim); }
        .cz-input {
          background: rgba(0,0,0,0.45); border: 1px solid color-mix(in srgb, var(--cz-accent) 45%, transparent);
          color: var(--cz-text); border-radius: 10px; padding: 0.5rem 0.75rem; width: 7.5rem; text-align: right;
          font-family: var(--cz-font-display); font-size: 1rem;
        }
        .cz-input:focus { outline: 2px solid var(--cz-accent); }
        @keyframes cz-pop { 0% { transform: scale(0.4); opacity: 0; } 60% { transform: scale(1.15); } 100% { transform: scale(1); opacity: 1; } }
        .cz-pop { animation: cz-pop 450ms cubic-bezier(0.2, 0.8, 0.2, 1); }
        @keyframes cz-shake { 0%,100% { transform: translateX(0); } 20% { transform: translateX(-8px); } 40% { transform: translateX(8px); } 60% { transform: translateX(-5px); } 80% { transform: translateX(5px); } }
        .cz-shake { animation: cz-shake 400ms; }
        @keyframes cz-flash { from { opacity: 0.9; } to { opacity: 0; } }
        .cz-flash { position: absolute; inset: 0; pointer-events: none; animation: cz-flash 700ms ease-out forwards; }
      `}</style>

      <div className="relative z-10 flex items-center justify-between gap-3 px-4 py-3" style={{ borderBottom: `1px solid color-mix(in srgb, ${t.accent} 25%, transparent)` }}>
        <div className="min-w-0">
          <div className="cz-display cz-glow truncate text-lg font-bold" style={{ color: t.accent }}>
            {title}
          </div>
          {tagline ? <div className="truncate text-xs" style={{ color: t.dim }}>{tagline}</div> : null}
        </div>
        <div className="flex shrink-0 items-center gap-2">
          {hudExtra}
          <span className="cz-chip">
            bankroll {ctx.quota ? formatDollars(ctx.remaining) : "…"}
          </span>
          <span className="cz-chip" style={{ color: ctx.sessionNet > 0 ? t.accent : ctx.sessionNet < 0 ? "#f87171" : undefined }}>
            session {ctx.sessionNet >= 0 ? "+" : ""}{formatDollars(ctx.sessionNet)}
          </span>
          <button className="cz-btn" aria-label={ctx.audio.muted ? "Unmute" : "Mute"} onClick={() => { ctx.audio.unlock(); ctx.audio.toggleMuted(); }}>
            {ctx.audio.muted ? <VolumeX className="h-4 w-4" /> : <Volume2 className="h-4 w-4" />}
          </button>
          <button className="cz-btn" onClick={() => { ctx.audio.sfx("click"); ctx.exit(); }} aria-label="Exit game">
            <span className="flex items-center gap-1"><X className="h-4 w-4" /> EXIT</span>
          </button>
        </div>
      </div>

      <div className="relative z-0 min-h-0 flex-1 overflow-y-auto" style={{ background: t.layers }}>
        {children}
      </div>
    </div>,
    document.body,
  );
}

// ---------------------------------------------------------------------------
// Wager panel: shared bet controls so every game feels consistent.
// ---------------------------------------------------------------------------

export function WagerPanel({
  value,
  onChange,
  disabled,
}: {
  value: string;
  onChange: (next: string) => void;
  disabled?: boolean;
}) {
  const ctx = useCasino();
  const set = (v: number) => {
    ctx.audio.sfx("chip");
    onChange((Math.floor(Math.min(Math.max(0, v), MAX_WAGER) * 100) / 100).toFixed(2));
  };
  return (
    <div className="flex flex-wrap items-center justify-center gap-2">
      <input
        className="cz-input"
        value={value}
        inputMode="decimal"
        aria-label="Wager in dollars"
        disabled={disabled}
        onChange={(e) => onChange(e.target.value)}
      />
      {[1, 5, 10, 25].map((p) => (
        <button key={p} className="cz-btn" disabled={disabled || p > ctx.remaining} onClick={() => set(p)}>
          ${p}
        </button>
      ))}
      <button className="cz-btn" disabled={disabled || ctx.remaining <= 0} onClick={() => set(ctx.remaining / 2)}>
        HALF
      </button>
      <button className="cz-btn" disabled={disabled || ctx.remaining <= 0} onClick={() => set(ctx.remaining)}>
        MAX
      </button>
    </div>
  );
}

// Server-side cap (schemas.QuotaGambleRequest): keeps payouts inside NUMERIC(12,4).
export const MAX_WAGER = 500000;

export function parseWager(value: string, remaining: number): number | null {
  const wager = Number.parseFloat(value);
  const cap = Math.min(remaining, MAX_WAGER);
  if (!Number.isFinite(wager) || wager <= 0 || wager > cap + 1e-9) return null;
  return Math.floor(wager * 100) / 100;
}

export function errorMessage(err: unknown): string {
  return err instanceof Error ? err.message : "Bet failed";
}
