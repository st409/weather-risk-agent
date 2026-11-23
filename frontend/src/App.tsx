import { FormEvent, useEffect, useMemo, useRef, useState } from "react";
import { ChatResponse, Driver, Evidence, sendMessage } from "./api";

const EXAMPLES = [
  {
    title: "Winter exposure",
    body: "Which Midwest hubs lose the most days to snow and freezing weather.",
    prompt: "Which hubs in the Midwest are most exposed to winter disruption?",
    icon: "snow",
  },
  {
    title: "Hurricane and flood",
    body: "Miami and Houston, compared on coastal storms and flood exposure.",
    prompt: "Compare Miami and Houston in terms of hurricane and flood exposure.",
    icon: "storm",
  },
  {
    title: "Denver snowfall",
    body: "The share of days in Denver last year that had snowfall.",
    prompt: "What percentage of days in Denver last year had snowfall?",
    icon: "flake",
  },
  {
    title: "Dallas risk",
    body: "Why the Dallas hub's weather disruption score is high.",
    prompt: "Why is the Dallas hub's weather disruption risk high?",
    icon: "heat",
  },
] as const;

const SCORE_TITLE: Record<string, string> = {
  winter: "Winter",
  flood: "Flood",
  hurricane: "Hurricane",
  heat: "Heat",
  severe_storm: "Severe storm",
  overall: "Overall",
};

const HAZARD_ORDER = ["winter", "flood", "hurricane", "heat", "severe_storm"];

const DRIVER_LABEL: Record<string, string> = {
  snow_days_pct: "Snow-day rate",
  freeze_days_pct: "Freeze-day rate",
  winter_baseline_score: "FEMA winter baseline",
  winter_alert_score: "Winter alerts",
  flood_baseline_score: "FEMA flood baseline",
  heavy_rain_days_pct: "Heavy-rain days",
  very_heavy_rain_days_pct: "Very heavy-rain days",
  flood_alert_score: "Flood alerts",
  hurricane_baseline_score: "FEMA hurricane baseline",
  tropical_alert_score: "Tropical alerts",
  coastal_modifier: "Coastal surge flag",
  heat_days_pct: "Days at or above 95°F",
  extreme_heat_days_pct: "Days at or above 100°F",
  heat_baseline_score: "FEMA heat baseline",
  heat_alert_score: "Heat alerts",
  severe_baseline_score: "FEMA severe-storm baseline",
  severe_alert_score: "Severe-storm alerts",
  high_wind_days_pct: "High-wind days",
  winter_score: "Winter",
  flood_score: "Flood",
  hurricane_score: "Hurricane",
  heat_score: "Heat",
  severe_storm_score: "Severe storm",
};

const THREADS_KEY = "wra-threads-v1";
const ACTIVE_KEY = "wra-active-v1";

type Turn = { role: "user" | "assistant"; text: string; payload?: ChatResponse; live?: boolean };

type Thread = {
  id: string;
  sessionId?: string;
  title: string;
  turns: Turn[];
  updatedAt: number;
};

export function App() {
  const [threads, setThreads] = useState<Thread[]>(loadThreads);
  const [activeId, setActiveId] = useState<string | null>(loadActiveId);
  const [draft, setDraft] = useState("");
  const [pending, setPending] = useState(false);
  const [historyOpen, setHistoryOpen] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const endRef = useRef<HTMLDivElement>(null);
  const threadsRef = useRef(threads);
  threadsRef.current = threads;

  const active = threads.find((thread) => thread.id === activeId) ?? null;
  const turns = active?.turns ?? [];
  const chatting = turns.length > 0 || pending;

  useEffect(() => {
    sessionStorage.setItem(THREADS_KEY, JSON.stringify(threads));
  }, [threads]);

  useEffect(() => {
    if (activeId) sessionStorage.setItem(ACTIVE_KEY, activeId);
    else sessionStorage.removeItem(ACTIVE_KEY);
  }, [activeId]);

  useEffect(() => {
    endRef.current?.scrollIntoView({ block: "end" });
  }, [turns, pending]);

  const groups = useMemo(() => groupThreads(threads), [threads]);

  async function ask(text: string) {
    const message = text.trim();
    if (!message || pending) return;
    setDraft("");
    setError(null);
    setPending(true);
    const threadId = activeId ?? crypto.randomUUID();
    const prior = threadsRef.current.find((thread) => thread.id === threadId);
    const sessionForCall = prior?.sessionId;
    if (!activeId) setActiveId(threadId);
    setThreads((current) => {
      const existing = current.find((thread) => thread.id === threadId);
      const userTurn: Turn = { role: "user", text: message };
      if (!existing) {
        return [
          { id: threadId, title: clip(message), turns: [userTurn], updatedAt: Date.now() },
          ...current,
        ];
      }
      return current.map((thread) =>
        thread.id === threadId
          ? { ...thread, turns: [...thread.turns, userTurn], updatedAt: Date.now() }
          : thread,
      );
    });
    try {
      const payload = await sendMessage(message, sessionForCall);
      setThreads((current) =>
        current.map((thread) =>
          thread.id === threadId
            ? {
                ...thread,
                sessionId: payload.session_id,
                updatedAt: Date.now(),
                turns: [...thread.turns, { role: "assistant", text: payload.answer.answer, payload, live: true }],
              }
            : thread,
        ),
      );
    } catch (err) {
      setError(err instanceof Error ? err.message : "The request failed.");
    } finally {
      setPending(false);
    }
  }

  function onSubmit(event: FormEvent) {
    event.preventDefault();
    void ask(draft);
  }

  function newChat() {
    if (pending) return;
    setActiveId(null);
    setDraft("");
    setError(null);
  }

  function settleTurn(threadId: string | undefined, index: number) {
    if (!threadId) return;
    setThreads((current) =>
      current.map((thread) =>
        thread.id === threadId
          ? { ...thread, turns: thread.turns.map((turn, i) => (i === index && turn.live ? { ...turn, live: false } : turn)) }
          : thread,
      ),
    );
  }

  function openThread(id: string) {
    if (pending || id === activeId) return;
    setActiveId(id);
    setDraft("");
    setError(null);
  }

  return (
    <div className={historyOpen ? "shell" : "shell history-closed"}>
      <aside className="sidebar" hidden={!historyOpen}>
        <div className="side-head">
          <h2>Chat history</h2>
          <button className="side-mark" type="button" aria-label="Hide chat history" aria-expanded={historyOpen} onClick={() => setHistoryOpen(false)}>
            <PanelIcon />
          </button>
        </div>
        <button className="new-chat" type="button" onClick={newChat} disabled={pending}>
          <ComposeIcon />
          New chat
        </button>
        <div className="history">
          {groups.length === 0 && <p className="history-empty">Questions you ask will stay in this list.</p>}
          {groups.map((group) => (
            <section key={group.label}>
              <p className="history-label">{group.label}</p>
              {group.threads.map((thread) => (
                <button
                  key={thread.id}
                  type="button"
                  className={thread.id === activeId ? "history-item active" : "history-item"}
                  onClick={() => openThread(thread.id)}
                >
                  {thread.title}
                </button>
              ))}
            </section>
          ))}
        </div>
      </aside>

      <section className="main">
        <header className="topbar">
          <div className="product">
            {!historyOpen && (
              <button className="side-mark" type="button" aria-label="Show chat history" aria-expanded={false} onClick={() => setHistoryOpen(true)}>
                <PanelIcon />
              </button>
            )}
            Weather Risk
          </div>
        </header>

        <div className={chatting ? "stage live" : "stage"}>
          {!chatting && (
            <div className="welcome">
              <div className="hero">
                <h1>Where is the weather risk?</h1>
                <p className="subtitle">Ask about winter, floods, hurricanes, or a single hub.</p>
              </div>
              {error && <div className="error">{error}</div>}
              <Composer draft={draft} pending={pending} onDraft={setDraft} onSubmit={onSubmit} onAsk={ask} />
              <div className="topics">
                {EXAMPLES.map((example) => (
                  <button key={example.title} type="button" className="topic" disabled={pending} onClick={() => void ask(example.prompt)}>
                    <span className="topic-icon" aria-hidden="true">
                      <TopicIcon name={example.icon} />
                    </span>
                    <span className="topic-title">{example.title}</span>
                    <span className="topic-body">{example.body}</span>
                  </button>
                ))}
              </div>
            </div>
          )}

          {chatting && (
            <>
              <div className="transcript">
                {turns.map((turn, index) => (
                  <article key={`${turn.role}-${index}`} className={`bubble ${turn.role}`}>
                    {turn.role === "assistant" && turn.payload ? (
                      <Reply payload={turn.payload} live={turn.live} onDone={() => settleTurn(active?.id, index)} />
                    ) : (
                      <p>{turn.text}</p>
                    )}
                  </article>
                ))}
                {pending && <p className="pending">Checking observations and writing the reply…</p>}
                <div ref={endRef} />
              </div>
              {error && <div className="error">{error}</div>}
              <Composer draft={draft} pending={pending} onDraft={setDraft} onSubmit={onSubmit} onAsk={ask} />
            </>
          )}
        </div>
      </section>
    </div>
  );
}

function Composer({
  draft,
  pending,
  onDraft,
  onSubmit,
  onAsk,
}: {
  draft: string;
  pending: boolean;
  onDraft: (value: string) => void;
  onSubmit: (event: FormEvent) => void;
  onAsk: (text: string) => void;
}) {
  return (
    <form className="composer" onSubmit={onSubmit}>
      <textarea
        aria-label="Message"
        value={draft}
        placeholder="Ask about a hub, a region, or a hazard…"
        onChange={(event) => onDraft(event.target.value)}
        onKeyDown={(event) => {
          if (event.key === "Enter" && !event.shiftKey) {
            event.preventDefault();
            onAsk(draft);
          }
        }}
      />
      <div className="composer-bar">
        <button className="send" type="submit" aria-label="Send" disabled={pending || !draft.trim()}>
          <ArrowIcon />
        </button>
      </div>
    </form>
  );
}

function Reply({ payload, live, onDone }: { payload: ChatResponse; live?: boolean; onDone?: () => void }) {
  const { answer, evidence } = payload;
  const detail = evidence.detail;
  const full = answer.answer;
  const [count, setCount] = useState(live ? 0 : full.length);
  const done = count >= full.length;
  const finished = useRef(false);

  useEffect(() => {
    if (!live || done) return;
    const id = window.setTimeout(() => setCount((current) => Math.min(full.length, current + 3)), 8);
    return () => window.clearTimeout(id);
  }, [live, done, count, full]);

  useEffect(() => {
    if (!live || !done || finished.current) return;
    finished.current = true;
    onDone?.();
  }, [live, done, onDone]);

  useEffect(() => {
    if (!live || done) return;
    document.querySelector(".transcript")?.scrollTo({ top: document.querySelector(".transcript")?.scrollHeight });
  }, [count, live, done]);

  return (
    <div className="reply">
      <p className="explain">
        {full.slice(0, count)}
        {live && !done && <span className="caret" aria-hidden="true" />}
      </p>
      {done ? <ReplyDetail answer={answer} evidence={evidence} detail={detail} /> : null}
    </div>
  );
}

function ReplyDetail({
  answer,
  evidence,
  detail,
}: {
  answer: ChatResponse["answer"];
  evidence: Evidence;
  detail: string | null | undefined;
}) {
  return (
    <>
      {detail === "methodology" || detail === "evidence" ? (
        <div className="block">
          <h3>Score breakdown</h3>
          {evidence.rankings.length > 1 && <ScoreTable evidence={evidence} columns={tableColumns(evidence)} />}
          {evidence.rankings[0]?.drivers.length > 0 && (
            <>
              <p className="meta">Drivers for {evidence.rankings[0].name}</p>
              <DriverTable drivers={evidence.rankings[0].drivers.slice(0, 4)} />
            </>
          )}
        </div>
      ) : null}
      {detail === "assumptions" || detail === "methodology" ? (
        <div className="block">
          <h3>Assumptions and limits</h3>
          <ul>
            {[...answer.assumptions, ...answer.uncertainties].map((item) => (
              <li key={item}>{item}</li>
            ))}
          </ul>
        </div>
      ) : null}
      {detail === "sources" && answer.sources.length > 0 ? (
        <div className="block">
          <h3>Sources</h3>
          <ul>
            {answer.sources.map((source) => (
              <li key={`${source.name}-${source.retrieved_at}`}>
                <strong>{source.name}</strong> · {source.source_type} · {source.retrieved_at}
                <div>{source.uncertainty_note}</div>
              </li>
            ))}
          </ul>
        </div>
      ) : null}
    </>
  );
}

function ScoreTable({ evidence, columns }: { evidence: Evidence; columns: string[] }) {
  return (
    <table>
      <thead>
        <tr>
          <th>Hub</th>
          {columns.map((column) => (
            <th key={column}>{SCORE_TITLE[column] ?? column}</th>
          ))}
        </tr>
      </thead>
      <tbody>
        {evidence.rankings.slice(0, 6).map((row) => (
          <tr key={row.hub_id}>
            <td>{row.name}</td>
            {columns.map((column) => (
              <td key={column}>{cellScore(row, column).toFixed(1)}</td>
            ))}
          </tr>
        ))}
      </tbody>
    </table>
  );
}

function DriverTable({ drivers }: { drivers: Driver[] }) {
  return (
    <table>
      <thead>
        <tr>
          <th>Factor</th>
          <th>Value</th>
          <th>Weight</th>
          <th>Points</th>
        </tr>
      </thead>
      <tbody>
        {drivers.map((driver) => (
          <tr key={driver.metric_name}>
            <td>{driverLabel(driver.metric_name)}</td>
            <td>{driver.value === null ? "—" : driver.value.toFixed(1)}</td>
            <td>{driver.weight.toFixed(2)}</td>
            <td>{driver.contribution.toFixed(1)}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

function tableColumns(evidence: Evidence): string[] {
  if (evidence.focus_hazards.length > 1) return evidence.focus_hazards;
  if (evidence.rankings.length > 1) return evidence.focus_hazards.length === 1 ? evidence.focus_hazards : ["overall"];
  if (evidence.focus_hazards.length === 1) return evidence.focus_hazards;
  return ["overall", ...HAZARD_ORDER];
}

function cellScore(row: Evidence["rankings"][number], column: string): number {
  if (column === "overall") return row.overall_score;
  if (column in row.hazard_scores) return row.hazard_scores[column];
  return row.focus_score;
}

function driverLabel(name: string): string {
  return DRIVER_LABEL[name] ?? name.replaceAll("_", " ");
}

function clip(text: string): string {
  const clean = text.replace(/\s+/g, " ").trim();
  return clean.length > 52 ? `${clean.slice(0, 49)}…` : clean;
}

function loadThreads(): Thread[] {
  try {
    const raw = sessionStorage.getItem(THREADS_KEY);
    if (!raw) return [];
    const parsed = JSON.parse(raw) as Thread[];
    return Array.isArray(parsed) ? parsed : [];
  } catch {
    return [];
  }
}

function loadActiveId(): string | null {
  return sessionStorage.getItem(ACTIVE_KEY);
}

function groupThreads(threads: Thread[]): { label: string; threads: Thread[] }[] {
  const buckets: Record<string, Thread[]> = { TODAY: [], YESTERDAY: [], EARLIER: [] };
  const sorted = [...threads].sort((a, b) => b.updatedAt - a.updatedAt);
  for (const thread of sorted) buckets[ageLabel(thread.updatedAt)].push(thread);
  return (["TODAY", "YESTERDAY", "EARLIER"] as const)
    .filter((label) => buckets[label].length > 0)
    .map((label) => ({ label, threads: buckets[label] }));
}

function ageLabel(timestamp: number): "TODAY" | "YESTERDAY" | "EARLIER" {
  const start = (date: Date) => new Date(date.getFullYear(), date.getMonth(), date.getDate()).getTime();
  const diff = start(new Date()) - start(new Date(timestamp));
  if (diff <= 0) return "TODAY";
  if (diff === 86_400_000) return "YESTERDAY";
  return "EARLIER";
}

function PanelIcon() {
  return (
    <svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" strokeWidth="1.8">
      <rect x="4" y="5" width="16" height="14" rx="2" />
      <path d="M10 5v14" />
    </svg>
  );
}

function ComposeIcon() {
  return (
    <svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" strokeWidth="1.8">
      <path d="M5 19h4l9.5-9.5a2.1 2.1 0 0 0-3-3L6 16v3z" />
      <path d="M13.5 7.5l3 3" />
    </svg>
  );
}

function ArrowIcon() {
  return (
    <svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" strokeWidth="2.2">
      <path d="M12 18V6" />
      <path d="M6.5 11.5 12 6l5.5 5.5" />
    </svg>
  );
}

function TopicIcon({ name }: { name: "snow" | "storm" | "flake" | "heat" }) {
  if (name === "storm") {
    return (
      <svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" strokeWidth="1.8">
        <path d="M12 3c2 3 2 5 0 8 3 0 5 2 5 5a5 5 0 1 1-10 0c0-3 2-5 5-5 2-3 2-5 0-8z" />
      </svg>
    );
  }
  if (name === "heat") {
    return (
      <svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" strokeWidth="1.8">
        <path d="M12 14a3 3 0 1 0 0 6 3 3 0 0 0 0-6z" />
        <path d="M12 14V4" />
        <path d="M9 7.5 12 4l3 3.5" />
      </svg>
    );
  }
  if (name === "flake") {
    return (
      <svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" strokeWidth="1.8">
        <path d="M12 3v18M5 7.5l14 9M5 16.5l14-9" />
      </svg>
    );
  }
  return (
    <svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" strokeWidth="1.8">
      <path d="M4 16h16" />
      <path d="M7 16V9M12 16V6M17 16v-5" />
    </svg>
  );
}
