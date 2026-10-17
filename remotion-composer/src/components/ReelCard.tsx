import React from "react";
import { AbsoluteFill, interpolate, spring, useCurrentFrame, useVideoConfig } from "remotion";

// Animated content card for vertical (1080x1920) reels: a stat that counts up, a list whose items
// slide in one by one, or a quote revealed word by word. Matches ViralReel Studio's still cards
// (same colours and layout), so the preview and the HD export look alike.

export interface ReelCardData {
  kind: "stat" | "list" | "quote";
  title?: string; // stat: the number ("8.1B"); list: optional heading; quote: the quote
  label?: string; // stat: what it measures; quote: who said it
  items?: string[]; // list items
  source?: string; // stat source
}

const ACCENTS = { stat: "#FDE047", list: "#F472B6", quote: "#A78BFA" } as const;
const FONT = '"Inter", "Helvetica Neue", Arial, sans-serif';
const SIDE = 128;
// Content sits in the upper two thirds; the bottom is left for captions and the platform's buttons.
const TOP = 330;
const BOTTOM = 1330;

// Bigger text for shorter strings, so a card fills its space without overflowing.
const sizeFor = (text: string, max: number, min: number, perChar: number, freeChars: number) =>
  Math.max(min, Math.min(max, max - (text.length - freeChars) * perChar));

// A bare year ("2023") is a date, not an amount: show it as is instead of counting up.
const YEAR_RE = /^\s*(1[5-9]|20)\d{2}\s*$/;

// "8.1B" -> {prefix:"", value:8.1, decimals:1, suffix:"B"}; null when there's no number to count.
export const parseStat = (stat: string) => {
  if (YEAR_RE.test(stat)) return null;
  const m = stat.match(/^([^\d-]*)(-?\d[\d,]*(?:\.\d+)?)(.*)$/);
  if (!m) return null;
  const digits = m[2].replace(/,/g, "");
  return {
    prefix: m[1],
    value: Number(digits),
    decimals: digits.includes(".") ? digits.split(".")[1].length : 0,
    suffix: m[3],
    grouped: m[2].includes(","),
  };
};

const formatStat = (p: NonNullable<ReturnType<typeof parseStat>>, value: number) => {
  const fixed = value.toFixed(p.decimals);
  const shown = p.grouped
    ? Number(fixed).toLocaleString("en-US", { minimumFractionDigits: p.decimals, maximumFractionDigits: p.decimals })
    : fixed;
  return `${p.prefix}${shown}${p.suffix}`;
};

const Background: React.FC<{ accent: string }> = ({ accent }) => (
  <AbsoluteFill
    style={{
      background: `radial-gradient(circle at 75% 28%, ${accent}33 0%, ${accent}00 65%), linear-gradient(180deg, #0B0B14 0%, #1A1033 100%)`,
    }}
  />
);

const Column: React.FC<{ children: React.ReactNode }> = ({ children }) => (
  <div
    style={{
      position: "absolute",
      left: SIDE,
      right: SIDE,
      top: TOP,
      height: BOTTOM - TOP,
      display: "flex",
      flexDirection: "column",
      justifyContent: "center",
      fontFamily: FONT,
    }}
  >
    {children}
  </div>
);

const rise = (frame: number, fps: number, delayFrames: number) => {
  const t = spring({ frame: frame - delayFrames, fps, config: { damping: 18, stiffness: 120 } });
  return { opacity: t, transform: `translateY(${interpolate(t, [0, 1], [40, 0])}px)` };
};

const Stat: React.FC<{ card: ReelCardData }> = ({ card }) => {
  const frame = useCurrentFrame();
  const { fps } = useVideoConfig();
  const title = card.title || "";
  const parsed = parseStat(title);
  const progress = interpolate(frame, [0, fps * 1.1], [0, 1], { extrapolateRight: "clamp", easing: (t) => 1 - Math.pow(1 - t, 3) });
  const shown = parsed ? formatStat(parsed, parsed.value * progress) : title;
  const pop = spring({ frame, fps, config: { damping: 12, stiffness: 110 } });
  const size = sizeFor(title, 340, 150, 26, 4);
  return (
    <Column>
      <div style={{ color: ACCENTS.stat, fontWeight: 800, fontSize: size, lineHeight: 1.05, transform: `scale(${interpolate(pop, [0, 1], [0.8, 1])})`, transformOrigin: "left center", whiteSpace: "nowrap" }}>
        {shown}
      </div>
      {card.label && (
        <div style={{ color: "#F8FAFC", fontWeight: 600, fontSize: sizeFor(card.label, 72, 48, 0.5, 30), lineHeight: 1.2, marginTop: 40, ...rise(frame, fps, Math.round(fps * 0.5)) }}>
          {card.label}
        </div>
      )}
      {card.source && (
        <div style={{ color: "#94A3B8", fontWeight: 500, fontSize: 38, marginTop: 50, ...rise(frame, fps, Math.round(fps * 0.9)) }}>
          Source: {card.source}
        </div>
      )}
    </Column>
  );
};

const List: React.FC<{ card: ReelCardData }> = ({ card }) => {
  const frame = useCurrentFrame();
  const { fps } = useVideoConfig();
  const items = (card.items || []).slice(0, 3);
  const stagger = Math.round(fps * 0.4);
  const start = card.title ? Math.round(fps * 0.35) : 0;
  return (
    <Column>
      {card.title && (
        <div style={{ color: "#F8FAFC", fontWeight: 800, fontSize: sizeFor(card.title, 84, 56, 0.8, 20), lineHeight: 1.15, marginBottom: 70, ...rise(frame, fps, 0) }}>
          {card.title}
        </div>
      )}
      {items.map((item, i) => {
        const t = spring({ frame: frame - start - i * stagger, fps, config: { damping: 16, stiffness: 120 } });
        return (
          <div key={i} style={{ display: "flex", alignItems: "center", gap: 40, marginBottom: 50, opacity: t, transform: `translateX(${interpolate(t, [0, 1], [-80, 0])}px)` }}>
            <div style={{ width: 100, height: 100, borderRadius: 50, background: ACCENTS.list, color: "#0B0B14", fontWeight: 800, fontSize: 56, display: "flex", alignItems: "center", justifyContent: "center", flexShrink: 0, transform: `scale(${t})` }}>
              {i + 1}
            </div>
            <div style={{ color: "#F8FAFC", fontWeight: 700, fontSize: sizeFor(item, 68, 46, 0.7, 18), lineHeight: 1.2 }}>{item}</div>
          </div>
        );
      })}
    </Column>
  );
};

const Quote: React.FC<{ card: ReelCardData }> = ({ card }) => {
  const frame = useCurrentFrame();
  const { fps } = useVideoConfig();
  const words = (card.title || "").split(/\s+/).filter(Boolean);
  const perWord = Math.max(2, Math.round((fps * 1.6) / Math.max(words.length, 1)));
  const mark = spring({ frame, fps, config: { damping: 12, stiffness: 100 } });
  const size = sizeFor(card.title || "", 104, 60, 0.45, 40);
  const done = Math.round(fps * 0.3) + words.length * perWord;
  return (
    <Column>
      <div style={{ color: ACCENTS.quote, fontWeight: 800, fontSize: 220, lineHeight: 0.8, fontFamily: "Georgia, serif", transform: `scale(${mark})`, transformOrigin: "left top" }}>“</div>
      <div style={{ color: "#F8FAFC", fontWeight: 800, fontSize: size, lineHeight: 1.2, marginTop: 30 }}>
        {words.map((w, i) => {
          const o = interpolate(frame, [Math.round(fps * 0.3) + i * perWord, Math.round(fps * 0.3) + (i + 1) * perWord], [0, 1], { extrapolateLeft: "clamp", extrapolateRight: "clamp" });
          return (
            <span key={i} style={{ opacity: o }}>
              {w}{" "}
            </span>
          );
        })}
      </div>
      {card.label && (
        <div style={{ color: "#94A3B8", fontWeight: 600, fontSize: 44, marginTop: 50, ...rise(frame, fps, done) }}>— {card.label}</div>
      )}
    </Column>
  );
};

export const ReelCard: React.FC<{ card: ReelCardData }> = ({ card }) => (
  <AbsoluteFill>
    <Background accent={ACCENTS[card.kind] || ACCENTS.stat} />
    {card.kind === "list" ? <List card={card} /> : card.kind === "quote" ? <Quote card={card} /> : <Stat card={card} />}
  </AbsoluteFill>
);
