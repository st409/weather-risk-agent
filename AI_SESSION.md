# AI session index

This is a short index of how the project was built with Cursor. The full exported chats are in [`docs/ai-session/`](docs/ai-session/). This file summarizes decisions only and does not quote them.

## What shipped

A weather-risk chat agent for 10 fixed US logistics hubs.

- Deterministic scores in Python. A language model only parses intent and rephrases wording, and only when `OPENAI_API_KEY` is set.
- Live public weather (Open-Meteo archive, NWS active alerts) plus a stored FEMA National Risk Index snapshot, in SQLite.
- Follow-up memory ("What about floods only?" keeps the previous hubs).
- Tests and fixture-mode evals that run offline, with no model.
- Deployed on Render: chat UI https://weather-risk-agent-dsev.onrender.com/ and API https://weather-risk-api.onrender.com. See [README.md](README.md) for the free-plan cold start.
- Bonus: optional voice input and read-aloud in the chat, using the browser Web Speech API.

Design details and formulas are in [DESIGN.md](DESIGN.md).

## Decisions

- Open-Meteo archive for daily history because it needs no token. NOAA CDO was not used.
- NWS active alerts for the current alert term. Severity weights are documented in `DESIGN.md` because the brief required a weighted count but did not define the weights.
- FEMA county hazard-type risk index scores were queried from the public counties feature service on 2026-11-23 and saved as a CSV. They were not invented. Large-county scores sit near 100, and the answers say so.
- Null FEMA hurricane or coastal-flood cells are treated as 0 and called out when that hazard is in view.
- Surge modifier is limited to Miami, Houston, and New York/Newark.
- Default analysis window is the previous complete calendar year. Alerts stay current.
- The rule parser is authoritative for explicit hubs, regions, and hazards, including follow-ups. The model may fill gaps only when a key is set and the rules are unsure.
- Python always attaches the scores and sources. Model prose is optional.
- Evals are assertion checks in fixture mode, with the model turned off.
- The package requires Python 3.10 or newer so it also runs on the 3.10 interpreter available on the build machine. The brief asked for 3.11+.
- SQLite for storage: a single file, no server, enough for 10 hubs.

## Answer pipeline and rules

Deterministic scores, then a composer skeleton, then an optional LLM rephrase, then a validator. A rejected draft is replaced by the skeleton. The prompts are in `backend/app/agent/llm.py` and the validator is in `backend/app/agent/composer.py`.

Current answer rules, as enforced by the prompt and the validator:

- Plain city names everywhere ("Miami", not a "hub (Region region)" form).
- A normal answer is two or three short sentences, then a `Scores:` line, then a one-line `Sources:` clause.
- A plural ranking names every scored hub in the opening sentence, without digits.
- Every number in the draft must appear in the skeleton or the evidence. A spelled-out count counts as the same fact.
- No month names or custom ranges. Only calendar year 2025 and a trailing 365 days are scored, and the reply says so.
- A removed hazard is not used to explain a result.
- The model has no score field. Follow-up suggestions come from the server.

## Corrections along the way

- Hand-authored FEMA-like baselines were rejected. The snapshot uses the published risk index scores instead.
- Snowfall is stored in centimeters, and the snow-day threshold is 1 cm.
- Answer wording was tightened over several rounds so that drafts keep the computed numbers and names.

## Full chats

- [01-plan.md](docs/ai-session/01-plan.md): planning, scoping, initial build, evals and answer wording
- [02-ui-deploy-iteration.md](docs/ai-session/02-ui-deploy-iteration.md): UI, voice, Render deploy, live QA iteration
- [03-code-review.md](docs/ai-session/03-code-review.md): code review, then round 1 of fixes
