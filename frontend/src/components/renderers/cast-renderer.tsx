"use client";

import type React from "react";
import { useState, useEffect, useRef } from "react";
import { Button } from "@/components/ui/button";
import { Slider } from "@/components/ui/slider";
import {
  Play,
  Pause,
  RotateCcw,
  SkipForward,
  SkipBack,
  FastForward,
} from "lucide-react";
import type { JSX } from "react";

interface CastHeader {
  version: number;
  width: number;
  height: number;
  timestamp: number;
  env?: Record<string, string>;
}

interface CastEvent {
  time: number;
  type: "o" | "i";
  data: string;
}

interface CastRendererProps {
  content: string;
}

/** Skip silent gaps longer than this many seconds during playback. */
const MAX_PAUSE_TIME = 1.5;

/** Cap on timeline event markers so huge recordings don't render thousands of divs. */
const MAX_TIMELINE_MARKERS = 200;

/**
 * Interprets the subset of ANSI escapes that matter for a readable replay
 * (carriage returns, clear-screen/line, and basic foreground colors) and
 * returns styled spans for the terminal viewport.
 */
function processAnsiEscapes(text: string): JSX.Element[] {
  const lines: string[] = [];
  let currentLine = "";
  let i = 0;

  while (i < text.length) {
    const char = text[i];

    if (char === "\r") {
      if (i + 1 < text.length && text[i + 1] === "\n") {
        lines.push(currentLine);
        currentLine = "";
        i += 2;
      } else {
        currentLine = "";
        i++;
      }
    } else if (char === "\n") {
      lines.push(currentLine);
      currentLine = "";
      i++;
    } else if (char === "\x1b" && i + 1 < text.length && text[i + 1] === "[") {
      let j = i + 2;
      while (j < text.length && !/[a-zA-Z]/.test(text[j])) {
        j++;
      }
      if (j < text.length) {
        const command = text[j];
        const params = text.slice(i + 2, j);

        if (command === "J") {
          if (params === "2" || params === "3") {
            lines.length = 0;
            currentLine = "";
          }
        } else if (command === "K") {
          currentLine = "";
        }
        i = j + 1;
      } else {
        i++;
      }
    } else if (char === "\x1b" && text.slice(i, i + 6) === "\x1b[?2004") {
      let j = i;
      while (j < text.length && text[j] !== "h" && text[j] !== "l") {
        j++;
      }
      i = j + 1;
    } else {
      currentLine += char;
      i++;
    }
  }

  if (currentLine) {
    lines.push(currentLine);
  }

  const finalText = lines.join("\n");
  const elements: JSX.Element[] = [];
  let currentIndex = 0;
  let elementKey = 0;
  let currentStyles: React.CSSProperties = {};

  // eslint-disable-next-line no-control-regex
  const colorRegex = /\x1b\[([0-9;]*)?m/g;
  let match;

  while ((match = colorRegex.exec(finalText)) !== null) {
    if (match.index > currentIndex) {
      const textContent = finalText.slice(currentIndex, match.index);
      if (textContent) {
        elements.push(
          <span key={elementKey++} style={currentStyles}>
            {textContent}
          </span>
        );
      }
    }

    const codes = match[1]
      ? match[1].split(";").map((code) => Number.parseInt(code) || 0)
      : [0];

    for (const code of codes) {
      if (code === 0) {
        currentStyles = {};
      } else if (code === 1) {
        currentStyles = { ...currentStyles, fontWeight: "bold" };
      } else if (code >= 30 && code <= 37) {
        const colors = [
          "#000000",
          "#cd0000",
          "#00cd00",
          "#cdcd00",
          "#0000ee",
          "#cd00cd",
          "#00cdcd",
          "#e5e5e5",
        ];
        currentStyles = { ...currentStyles, color: colors[code - 30] };
      } else if (code >= 90 && code <= 97) {
        const brightColors = [
          "#7f7f7f",
          "#ff0000",
          "#00ff00",
          "#ffff00",
          "#5c5cff",
          "#ff00ff",
          "#00ffff",
          "#ffffff",
        ];
        currentStyles = { ...currentStyles, color: brightColors[code - 90] };
      } else if (code === 39) {
        const next = { ...currentStyles };
        delete next.color;
        currentStyles = next;
      }
    }

    currentIndex = colorRegex.lastIndex;
  }

  if (currentIndex < finalText.length) {
    const textContent = finalText.slice(currentIndex);
    if (textContent) {
      elements.push(
        <span key={elementKey++} style={currentStyles}>
          {textContent}
        </span>
      );
    }
  }

  return elements.length > 0 ? elements : [<span key={0}>{finalText}</span>];
}

function formatTime(seconds: number): string {
  const mins = Math.floor(seconds / 60);
  const secs = Math.floor(seconds % 60);
  return `${mins}:${secs.toString().padStart(2, "0")}`;
}

/** Playback viewer for asciinema `.cast` terminal recordings. */
export function CastRenderer({ content }: CastRendererProps) {
  const [header, setHeader] = useState<CastHeader | null>(null);
  const [events, setEvents] = useState<CastEvent[]>([]);
  const [parseError, setParseError] = useState<string | null>(null);
  const [currentEventIndex, setCurrentEventIndex] = useState(0);
  const [isPlaying, setIsPlaying] = useState(false);
  const [currentTime, setCurrentTime] = useState(0);
  const [totalTime, setTotalTime] = useState(0);
  const [playbackSpeed, setPlaybackSpeed] = useState(1);
  const [terminalElements, setTerminalElements] = useState<JSX.Element[]>([]);

  const intervalRef = useRef<ReturnType<typeof setInterval> | null>(null);
  const startTimeRef = useRef<number>(0);
  const playStartTimeRef = useRef<number>(0);
  const terminalRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    try {
      const lines = content.trim().split("\n");
      const headerLine = lines[0];
      const eventLines = lines.slice(1);

      const parsedHeader = JSON.parse(headerLine) as CastHeader;

      const parsedEvents: CastEvent[] = eventLines
        .filter((line) => line.trim())
        .map((line) => {
          const [time, type, data] = JSON.parse(line) as [
            number,
            "o" | "i",
            string,
          ];
          return { time, type, data };
        });

      setHeader(parsedHeader);
      setEvents(parsedEvents);
      setTotalTime(parsedEvents[parsedEvents.length - 1]?.time ?? 0);
      setCurrentEventIndex(0);
      setCurrentTime(0);
      setTerminalElements([]);
      setParseError(null);
    } catch (error) {
      setParseError(
        error instanceof Error ? error.message : "Failed to parse cast data"
      );
    }
  }, [content]);

  useEffect(() => {
    if (!isPlaying || events.length === 0) {
      if (intervalRef.current) {
        clearInterval(intervalRef.current);
      }
      return;
    }

    playStartTimeRef.current = Date.now();
    startTimeRef.current = currentTime;

    intervalRef.current = setInterval(() => {
      const elapsed =
        ((Date.now() - playStartTimeRef.current) / 1000) * playbackSpeed;
      const newTime = startTimeRef.current + elapsed;

      setCurrentTime(newTime);

      let newContent = "";
      let newEventIndex = 0;

      for (let i = 0; i < events.length; i++) {
        if (events[i].time <= newTime) {
          if (events[i].type === "o") {
            newContent += events[i].data;
          }
          newEventIndex = i + 1;
        } else {
          break;
        }
      }

      setTerminalElements(processAnsiEscapes(newContent));
      setCurrentEventIndex(newEventIndex);

      // Auto-skip long silent gaps so replays stay watchable.
      if (newEventIndex < events.length) {
        const nextEvent = events[newEventIndex];
        const timeSinceLastEvent =
          newTime - (events[newEventIndex - 1]?.time ?? 0);
        const timeToNextEvent = nextEvent.time - newTime;

        if (
          timeToNextEvent > MAX_PAUSE_TIME &&
          timeSinceLastEvent >= MAX_PAUSE_TIME
        ) {
          setCurrentTime(nextEvent.time);
          startTimeRef.current = nextEvent.time;
          playStartTimeRef.current = Date.now();
        }
      }

      if (newTime >= totalTime) {
        setIsPlaying(false);
        setCurrentTime(totalTime);
      }
    }, 50);

    return () => {
      if (intervalRef.current) {
        clearInterval(intervalRef.current);
      }
    };
    // currentTime is intentionally read once on play start via startTimeRef.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [isPlaying, events, totalTime, playbackSpeed]);

  useEffect(() => {
    if (terminalRef.current && isPlaying) {
      terminalRef.current.scrollTop = terminalRef.current.scrollHeight;
    }
  }, [terminalElements, isPlaying]);

  const seekToTime = (time: number) => {
    setIsPlaying(false);

    let seekContent = "";
    let eventIndex = 0;

    for (let i = 0; i < events.length; i++) {
      if (events[i].time <= time) {
        if (events[i].type === "o") {
          seekContent += events[i].data;
        }
        eventIndex = i + 1;
      } else {
        break;
      }
    }

    setCurrentEventIndex(eventIndex);
    setCurrentTime(time);
    setTerminalElements(processAnsiEscapes(seekContent));
  };

  const resetPlayback = () => {
    setIsPlaying(false);
    setCurrentEventIndex(0);
    setCurrentTime(0);
    setTerminalElements([]);
    if (intervalRef.current) {
      clearInterval(intervalRef.current);
    }
  };

  const togglePlayback = () => {
    if (currentTime >= totalTime) {
      resetPlayback();
      setIsPlaying(true);
    } else {
      setIsPlaying(!isPlaying);
    }
  };

  const scrollTerminalToBottom = () => {
    setTimeout(() => {
      if (terminalRef.current) {
        terminalRef.current.scrollTop = terminalRef.current.scrollHeight;
      }
    }, 100);
  };

  const goToNextEvent = () => {
    if (currentEventIndex < events.length) {
      const nextEvent = events[currentEventIndex];
      const wasPlaying = isPlaying;
      seekToTime(nextEvent.time);
      if (wasPlaying) {
        setIsPlaying(true);
      }
      scrollTerminalToBottom();
    }
  };

  const goToPreviousEvent = () => {
    if (currentEventIndex > 0) {
      const prevEvent = events[currentEventIndex - 2];
      const wasPlaying = isPlaying;
      seekToTime(prevEvent?.time ?? 0);
      if (wasPlaying) {
        setIsPlaying(true);
      }
      scrollTerminalToBottom();
    }
  };

  useEffect(() => {
    const handleKeyPress = (e: KeyboardEvent) => {
      if (
        e.target instanceof HTMLInputElement ||
        e.target instanceof HTMLSelectElement ||
        e.target instanceof HTMLTextAreaElement
      ) {
        return;
      }

      switch (e.key) {
        case " ":
          e.preventDefault();
          togglePlayback();
          break;
        case "r":
          resetPlayback();
          break;
        case "ArrowRight":
          goToNextEvent();
          break;
        case "ArrowLeft":
          goToPreviousEvent();
          break;
      }
    };

    window.addEventListener("keydown", handleKeyPress);
    return () => window.removeEventListener("keydown", handleKeyPress);
    // Handlers are re-created each render; re-binding on these is enough.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [isPlaying, currentEventIndex, currentTime, totalTime, events.length]);

  useEffect(() => {
    return () => {
      if (intervalRef.current) {
        clearInterval(intervalRef.current);
      }
    };
  }, []);

  if (parseError) {
    return (
      <div className="text-destructive p-4 text-sm">
        Failed to parse cast recording: {parseError}
      </div>
    );
  }

  if (!header || events.length === 0) {
    return (
      <div className="text-muted-foreground flex items-center justify-center p-8 text-sm">
        No playable events in this recording
      </div>
    );
  }

  // Sample markers evenly when there are too many events to draw.
  const markerStep = Math.max(
    1,
    Math.ceil(events.length / MAX_TIMELINE_MARKERS)
  );
  const markers = events.filter((_, index) => index % markerStep === 0);

  return (
    <div className="space-y-4 p-4">
      <div
        ref={terminalRef}
        className="border-border bg-card text-foreground overflow-auto rounded-md border p-4 font-mono text-sm"
        style={{
          width: "100%",
          height: `${Math.min(header.height * 1.2 + 2, 32)}rem`,
          whiteSpace: "pre-wrap",
          wordBreak: "break-all",
        }}
      >
        {terminalElements.length > 0 ? terminalElements : "$ "}
        <span className="animate-pulse">█</span>
      </div>

      <div className="space-y-3">
        <div className="space-y-2">
          <div className="relative">
            <Slider
              value={[currentTime]}
              max={totalTime}
              step={0.1}
              onValueChange={([value]) => seekToTime(value)}
              className="w-full"
            />
            <div className="pointer-events-none absolute top-0 left-0 h-full w-full">
              {markers.map((event, index) => {
                const position =
                  totalTime > 0 ? (event.time / totalTime) * 100 : 0;
                return (
                  <div
                    key={index}
                    className="bg-primary/40 absolute top-1/2 h-2 w-1 -translate-y-1/2 rounded-full transition-all"
                    style={{ left: `${position}%` }}
                    title={`Event at ${formatTime(event.time)}`}
                  />
                );
              })}
            </div>
          </div>
          <div className="text-muted-foreground flex justify-between text-xs">
            <span>{formatTime(currentTime)}</span>
            <span>{formatTime(totalTime)}</span>
          </div>
        </div>

        <div className="border-border bg-card/50 flex items-center justify-center gap-2 rounded-lg border p-2">
          <Button
            onClick={resetPlayback}
            variant="ghost"
            size="sm"
            className="h-8 w-8 p-0"
          >
            <RotateCcw className="h-4 w-4" />
          </Button>

          <Button
            onClick={goToPreviousEvent}
            variant="ghost"
            size="sm"
            disabled={currentEventIndex <= 0}
            className="h-8 w-8 p-0"
          >
            <SkipBack className="h-4 w-4" />
          </Button>

          <Button
            onClick={togglePlayback}
            variant="default"
            size="sm"
            className="h-8 w-8 p-0"
          >
            {isPlaying ? (
              <Pause className="h-4 w-4" />
            ) : (
              <Play className="h-4 w-4" />
            )}
          </Button>

          <Button
            onClick={goToNextEvent}
            variant="ghost"
            size="sm"
            disabled={currentEventIndex >= events.length}
            className="h-8 w-8 p-0"
          >
            <SkipForward className="h-4 w-4" />
          </Button>

          <div className="border-border ml-2 flex items-center gap-2 border-l pl-2">
            <FastForward className="text-muted-foreground h-3 w-3" />
            <select
              value={playbackSpeed}
              onChange={(e) => setPlaybackSpeed(Number(e.target.value))}
              className="border-border bg-card text-foreground focus:ring-ring rounded border px-2 py-1 text-xs focus:ring-1 focus:outline-none"
            >
              <option value={0.5}>0.5x</option>
              <option value={1}>1x</option>
              <option value={1.5}>1.5x</option>
              <option value={2}>2x</option>
              <option value={4}>4x</option>
            </select>
          </div>
        </div>

        <div className="text-muted-foreground text-center text-xs">
          Keyboard: Space = Play/Pause, R = Reset, ← → = Navigate events
        </div>
      </div>
    </div>
  );
}
