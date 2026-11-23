const HAZARD_PREFIXES = ["overall disruption", "severe storm", "hurricane", "winter", "flood", "heat"];

export function recognitionSupported(): boolean {
  return typeof window !== "undefined" && recognitionCtor() !== null;
}

export function recognitionCtor(): (new () => SpeechRecognition) | null {
  if (typeof window === "undefined") return null;
  return window.SpeechRecognition ?? window.webkitSpeechRecognition ?? null;
}

export function stopSpeaking(): void {
  window.speechSynthesis?.cancel();
}

export function speakText(text: string): void {
  const spoken = text.trim();
  if (!spoken || !window.speechSynthesis) return;
  window.speechSynthesis.cancel();
  const utterance = new SpeechSynthesisUtterance(spoken);
  utterance.lang = "en-US";
  window.speechSynthesis.speak(utterance);
}

/** Drop the Sources sentence and phrase the Scores line for speech. */
export function speakableAnswer(text: string): string {
  const withoutSources = text.replace(/\s*Sources\s*:\s[\s\S]*$/i, "").trim();
  const match = withoutSources.match(/^(.*?)(?:^|\s)Scores\s*:\s*([\s\S]+)$/i);
  if (!match) return withoutSources;
  const prose = match[1].trim();
  const scores = naturalScores(match[2].trim());
  return [prose, scores].filter(Boolean).join(" ");
}

function naturalScores(raw: string): string {
  const legend = /\(higher\s*=\s*more exposed\)/i.test(raw);
  const core = raw
    .replace(/\(higher\s*=\s*more exposed\)/gi, "")
    .replace(/[.\s]+$/g, "")
    .trim();
  const groups = core.split(/\s*;\s*/).filter(Boolean).map(speakScoreGroup);
  const tail = legend ? "Higher means more exposed." : "";
  if (!core) return ["Scores.", tail].filter(Boolean).join(" ");
  if (groups.some((group) => group === null)) {
    const fallback = core
      .replace(/(\d+)\.(\d+)/g, "$1 point $2")
      .replace(/\bn\/a\b/gi, "not available");
    return ["Scores.", `${fallback}.`, tail].filter(Boolean).join(" ").replace(/\s+/g, " ").trim();
  }
  return ["Scores.", ...(groups as string[]), tail].filter(Boolean).join(" ").replace(/\s+/g, " ").trim();
}

function speakScoreGroup(group: string): string | null {
  let label = "";
  let rest = group.trim();
  const lower = rest.toLowerCase();
  for (const prefix of HAZARD_PREFIXES) {
    if (lower.startsWith(`${prefix} `)) {
      label = prefix;
      rest = rest.slice(prefix.length).trim();
      break;
    }
  }
  const bits = rest.split(/\s*,\s*/).filter(Boolean).map(speakScoreBit);
  if (bits.some((bit) => bit === null)) return null;
  const body = (bits as string[]).join(". ");
  if (!label) return `${body}.`;
  return `${titleLabel(label)}: ${body}.`;
}

function speakScoreBit(bit: string): string | null {
  const match = bit.trim().match(/^(.+?)\s+(\d+\.\d+|\d+|n\/a)$/i);
  if (!match) return null;
  return `${match[1]}, ${speakNumber(match[2])}`;
}

function speakNumber(raw: string): string {
  if (raw.toLowerCase() === "n/a") return "not available";
  const [whole, fraction] = raw.split(".");
  if (!fraction) return whole;
  return `${whole} point ${fraction}`;
}

function titleLabel(label: string): string {
  return label.charAt(0).toUpperCase() + label.slice(1);
}
