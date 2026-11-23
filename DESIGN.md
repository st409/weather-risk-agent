# Design

## Architecture

```mermaid
flowchart TD
  User["User"] --> ChatUI["Chat UI"]
  ChatUI --> ApiChat["POST /api/chat"]
  ApiChat --> Orchestrator["Agent Orchestrator"]
  Orchestrator --> PlannerLLM["LLM Intent Parser JSON"]
  Orchestrator --> RiskEngine["Deterministic Risk Engine"]
  RiskEngine --> WeatherClients["Open-Meteo and NWS"]
  RiskEngine --> HazardStore["SQLite plus FEMA snapshot"]
  Orchestrator --> AnswerLLM["LLM Answer Composer JSON"]
  AnswerLLM --> ApiChat
  ApiChat --> ChatUI
```

The model does not rank hubs. Python fetches data, scores it, and checks JSON schema. If the model is missing, fails schema validation twice, or the draft fails the answer check, the deterministic briefing is returned and the scores still come from Python.

## Repository structure

```text
weather-risk-agent/
├── backend/
│   ├── app/
│   │   ├── api/        FastAPI routes: chat, hubs, rate limit
│   │   ├── agent/      orchestrator, intent rules, composer, model calls, schemas
│   │   ├── risk/       scoring, weather and FEMA clients, hub list
│   │   └── db/         SQLite schema and repository
│   ├── data/           weather_risk.db, FEMA snapshot CSV and metadata
│   ├── tests/          pytest
│   └── evals/          eval_set.json and run_eval.py
├── frontend/src/       Vite + React chat UI, voice input and read-aloud
├── scripts/            setup, dev, test, eval, seed tasks
├── docs/ai-session/    01-plan.md, 02-ui-deploy-iteration.md, 03-code-review.md
├── render.yaml         Render service settings for the API
├── README.md
├── DESIGN.md
└── AI_SESSION.md
```

## Components

| Piece | Role |
| --- | --- |
| `backend/app/api/chat.py` | `POST /api/chat` |
| `backend/app/api/hubs.py` | Hub list, one hub score, compare, refresh |
| `backend/app/agent/orchestrator.py` | Session, tool routing, model calls |
| `backend/app/agent/session_context.py` | Intent rules and follow-up memory |
| `backend/app/agent/schemas.py` | Pydantic contracts for intent and answer |
| `backend/app/agent/llm.py` | Schema-checked model calls and the retry |
| `backend/app/agent/composer.py` | Wording built from scores |
| `backend/app/risk/scoring.py` | Formulas below |
| `backend/app/risk/weather.py` | Open-Meteo archive and NWS active alerts |
| `backend/app/risk/hazards.py` | FEMA county snapshot |
| `backend/app/risk/hubs.py` | The ten hubs |
| `frontend/` | Chat. The answer text includes the scores line and a sources sentence. Drivers, assumptions, and the source list render only when the question asks for a breakdown, the methodology, or sources. Follow-up questions are returned by the API and are not shown as buttons. |

Direct score routes exist so the system is not only a chatbot.

## Data

Historical daily weather is the Open-Meteo archive (`temperature_2m_max`, `temperature_2m_min`, `precipitation_sum`, `snowfall_sum`, `wind_gusts_10m_max`), in Fahrenheit, inches, and mph. Snowfall stays in centimeters. No token. This is the reproducible live source. NOAA CDO is not used.

Current alerts are the National Weather Service active alerts for the hub point. Severity weights, which the brief did not specify, are Extreme 3, Severe 2, Moderate 1, Minor 0.5, Unknown 0.25.

FEMA National Risk Index hazard-type scores for the hub counties were queried on 2026-11-23 from the public counties feature service and stored in `backend/data/fema_nri_snapshot.csv`. They are already on a 0–100 scale. A null hurricane score is stored blank and scored as 0, and that 0 keeps the 0.70 baseline weight. It is not dropped and rescaled, so an unpublished cell (Denver, Minneapolis) and a published 0 (Los Angeles) use the same baseline number. That note is included when the question is about hurricane. On another question it is left out of the answer. A null coastal-flood score is stored blank and does not zero the flood baseline, because that baseline is the greater of inland and coastal. The severe-storm baseline is the average of tornado, lightning, hail, and strong wind.

The default window is the previous complete calendar year. "Last year" uses that window. Alerts are current, not historical. Each hazard score mixes that historical window with the current alert term (winter 0.10, flood 0.15, hurricane 0.20, heat 0.15, severe storm 0.30). The number is a blend of historical exposure and current operational risk, not a pure historical index.

Every answer carries `source`, `source_type` (`live_api`, `cached_api`, or `static_public_dataset`), `retrieved_at`, and a short uncertainty note. If Open-Meteo cannot be reached, that hub is left out of the result and no stand-in number is used. If NWS cannot be reached, the alert term is dropped and the remaining weights are rescaled. A successful call with no active alert stays a real zero. Tests set `WEATHER_DATA_MODE=fixture` so they do not need the network.

## Storage

SQLite holds hubs, the FEMA rows, cached daily observations, cached alerts, score snapshots, and, while the app is running, chat sessions, messages, and session context. The committed database does not keep chat rows. Session context keeps the last hubs, region, hazards, metric, time window, intent, ordered result ids, and the previous user message. That previous turn is sent to the intent parser on the next question. The answer call receives computed scores as read-only input and has no score field in its schema. The response scores are the ones Python computed.

SQLite fits because it is a single file with no server to run, and ten hubs plus their cached weather are far below anything that needs a networked database. The committed file is also what the free Render instance starts with.

Follow-ups are resolved in Python before tools run. After "Compare Miami and Houston in terms of hurricane and flood exposure", "What about floods only?" stays a comparison of those two hubs with hazards set to flood. "Why is the top one risky?" explains the first hub in the previous ordering. If the reference cannot be resolved, the agent asks one question.

## Scoring

```text
clamp_0_100(x) = min(100, max(0, x))
normalize(value, high) = clamp_0_100((value / high) * 100)
```

Winter:

```text
snow_days_pct = days_with_snowfall_gte_1cm / valid_days * 100
freeze_days_pct = days_with_min_temp_lte_32f / valid_days * 100
winter_alert_score = normalize(winter_alert_weighted_count, 5)

winter_score =
  0.40 * normalize(snow_days_pct, 35) +
  0.30 * normalize(freeze_days_pct, 60) +
  0.20 * winter_baseline_score +
  0.10 * winter_alert_score
```

Flood:

```text
flood_score =
  0.35 * flood_baseline_score +
  0.30 * normalize(heavy_rain_days_pct, 15) +
  0.20 * normalize(very_heavy_rain_days_pct, 5) +
  0.15 * flood_alert_score
```

`heavy_rain_days_pct` is days at or above 1 inch. `very_heavy_rain_days_pct` is days at or above 2 inches. `flood_alert_score = normalize(count, 5)`.

Hurricane:

```text
hurricane_score =
  0.70 * hurricane_baseline_score +
  0.20 * normalize(tropical_alert_weighted_count, 3) +
  0.10 * coastal_modifier
```

`coastal_modifier` is a fixed surge-exposure flag, not a FEMA score and not a distance to the coast. It is 100 for Miami, Houston, and New York/Newark, the hubs treated as exposed to tropical-cyclone surge, and 0 otherwise. Los Angeles is coastal, but its coastal flood stays in the flood baseline and is not treated as tropical surge.

Heat:

```text
heat_score =
  0.35 * normalize(heat_days_pct, 30) +
  0.25 * normalize(extreme_heat_days_pct, 10) +
  0.25 * heat_baseline_score +
  0.15 * heat_alert_score
```

Heat days are maxima at or above 95 F. Extreme heat days are at or above 100 F. Alert high is 5.

Severe storm:

```text
severe_storm_score =
  0.50 * severe_baseline_score +
  0.30 * severe_alert_score +
  0.20 * normalize(high_wind_days_pct, 10)
```

High-wind days are gusts at or above 40 mph. Alert high is 5.

Overall:

```text
overall_risk =
  0.30 * winter_score +
  0.25 * flood_score +
  0.20 * hurricane_score +
  0.15 * severe_storm_score +
  0.10 * heat_score
```

Midwest winter ranking keeps `region == Midwest` and sorts by `winter_score` descending. A missing input is dropped and the remaining weights are rescaled. A real zero is kept. Each driver has metric name, raw value, weight, contribution, source, freshness, and confidence. Contribution is the effective weight times the normalized value, so contributions sum to the score.

Business-importance weights are stored as 1.0 and are not applied.

## Model boundary

Two schema-checked calls when a key is present:

1. Intent JSON: `rank_hubs`, `compare_hubs`, `metric_lookup`, `explain_hub`, `follow_up`, `alerts`, `methodology`, `greeting`, `off_topic`, `unsupported_language`, or `unknown`, plus hubs, region, hazards, metric, window, and a clarification flag. Greeting, off-topic, methodology, and non-English turns are answered in code and do not call the model.
2. Answer JSON: `answer` and `follow_up_questions` only. Findings, scores, assumptions, uncertainties, and sources stay on the Python result.

The model schema has no score or source field. Python keeps the scores and sources it computed. Explicit hubs and hazards found in the text stay authoritative. One schema retry, then the deterministic answer. A draft that passes the schema can still be rejected by the answer check and replaced by the skeleton.

Answer pipeline: deterministic scores, then the composer builds a wording skeleton from them, then the model (if a key is set) rephrases the skeleton, then a validator checks the draft against the evidence and the answer rules. A rejected draft is replaced by the skeleton, so the user always gets a score-backed answer.

"Safer" means a lower disruption score. With no hubs in context, the agent asks which hubs to compare and states that definition.

## Evaluation

`backend/evals/eval_set.json` checks status, schema, hub names, non-null metrics, assumptions, and sources. There is no model judge. Fixture mode keeps the run offline. One case feeds invalid JSON twice and checks that schema validation fails closed.

## Assumptions

Answers are English only. A message in another language gets one English redirect and no ranking.

Supported time windows are the previous calendar year, the last 365 days, and the most recent finished winter (December through February). A single month, a quarter, or a custom range is not scored. The reply names calendar year and the last 365 days, then answers for the previous calendar year. "Last winter" is scored on its own. A trailing-365 answer says "in the last 365 days". The previous intent is kept, so a snow-day question stays a snow-day question.

Overall weights are fixed: winter 0.30, flood 0.25, hurricane 0.20, severe storm 0.15, heat 0.10. These weights are an assumption. In a real system they would be calibrated against the company's downtime days and costs. Asking to ignore a hazard drops that weight and renormalizes the rest. Business-importance weights stay at 1.0 and are not applied. The ranking is weather exposure only, not an investment priority. A later ranking would also need shipment volume and the cost of downtime.

## Scope left out

Fixed hubs and five hazards only. No wildfire score, no freight weighting, and no score-history chart. Deployment is on Render (API and chat UI). FEMA scores for these urban counties are often saturated; the daily rates are what separate them, and the answers say so.

Bonus: optional voice input (mic) and read-aloud, using the browser Web Speech API in the frontend. Nothing on the backend depends on it.
