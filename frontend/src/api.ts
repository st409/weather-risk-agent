export type ScoreItem = {
  hub_id: string;
  hub_name: string;
  hazard: string;
  score: number;
  unit: string;
};

export type SourceItem = {
  name: string;
  source_type: "live_api" | "cached_api" | "static_public_dataset";
  retrieved_at: string;
  uncertainty_note: string;
};

export type Answer = {
  answer: string;
  key_findings: string[];
  scores: ScoreItem[];
  assumptions: string[];
  uncertainties: string[];
  sources: SourceItem[];
  follow_up_questions: string[];
};

export type Driver = {
  metric_name: string;
  value: number | null;
  weight: number;
  contribution: number;
  source: string;
  freshness: string;
  confidence: string;
};

export type Ranking = {
  hub_id: string;
  name: string;
  region: string;
  overall_score: number;
  focus_score: number;
  hazard_scores: Record<string, number>;
  drivers: Driver[];
  active_events: string[];
};

export type Evidence = {
  window_label: string;
  window_start: string;
  window_end: string;
  focus_hazards: string[];
  safety_framing: boolean;
  used_fallback_weather: boolean;
  rankings: Ranking[];
  detail?: string | null;
  metric: {
    name: string;
    label: string;
    hub_name: string;
    value: number | null;
    numerator: number;
    denominator: number;
    unit: string;
  } | null;
};

export type ChatResponse = {
  session_id: string;
  answer: Answer;
  intent: {
    intent: string;
    hub_ids: string[];
    region: string | null;
    hazards: string[];
    needs_clarification: boolean;
  };
  evidence: Evidence;
};

export type Health = {
  status: string;
  data_mode: string;
  llm_enabled: boolean;
  hub_count: number;
};

export async function getHealth(): Promise<Health> {
  const response = await fetch("/api/health");
  if (!response.ok) {
    throw new Error("Backend is not responding.");
  }
  return response.json();
}

export async function sendMessage(message: string, sessionId?: string): Promise<ChatResponse> {
  const response = await fetch("/api/chat", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ message, session_id: sessionId }),
  });
  if (!response.ok) {
    const detail = await response.text();
    throw new Error(detail || `Request failed (${response.status})`);
  }
  return response.json();
}
