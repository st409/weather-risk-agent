# AI session summary

This file summarizes the build session. The full Cursor chat is the source transcript; export it from the chat history if a file export is required. There is no separate public transcript URL.

## Request

Build a narrow weather-risk agent for 10 US logistics hubs: deterministic scores, live public weather, schema-checked model calls, follow-up memory, tests, evals, and docs. Keep it in this folder. Do not commit it and do not publish it unless asked.

A later instruction narrowed the work to the submission minimum and said to skip bonus items. That means no score-history chart and no deployment. The required chat, scoring, weather source, schema boundary, evals, and docs stayed.

## Decisions

- Open-Meteo archive for daily history because it needs no token. NOAA CDO was skipped.
- NWS active alerts for the current alert term. Severity weights are documented in `DESIGN.md` because the brief required a weighted count but did not define the weights.
- FEMA county hazard-type risk index scores were queried from the public counties feature service on 2026-11-23 and saved as a CSV. They were not invented. Large-county scores sit near 100, and the answers state that.
- Null FEMA hurricane or coastal-flood cells are treated as 0 and called out when that hazard is in view.
- Surge modifier is limited to Miami, Houston, and New York/Newark.
- Default analysis window is the previous complete calendar year. Alerts stay current.
- The rule parser is authoritative for explicit hubs, regions, and hazards, including follow-ups such as "What about floods only?". The model may fill gaps only when a key is set and the rules are unsure.
- Python always attaches the scores and sources. Model prose is optional.
- Evals are assertion checks in fixture mode, with the model turned off.
- Package requires Python 3.10 or newer so it also runs on the 3.10 interpreter available on the build machine. The brief asked for 3.11+.

## Prompts used for the model

Intent system prompt, in `backend/app/agent/llm.py`:

> You convert one chat turn about weather disruption at a fixed set of US logistics hubs into JSON. Do not compute scores, rankings, or percentages. Use only hub ids from the list in the user message. The user message includes previous_turn from this same chat. Read it before you classify. If the new message continues that turn and does not name new hubs, set intent to follow_up and leave hub_ids empty. A follow-up such as "What about floods only?" or "ומה לגבי שיטפונות בלבד?" keeps the previous hubs and changes only the hazard the user just named. If a place is outside the hub list, set needs_clarification to true and name the place in clarifying_question. Set needs_clarification to false when the request can be answered from the hub list.

Answer system prompt:

> You rewrite a factual skeleton into a logistics briefing. Start with the direct answer. If the skeleton is a day count, lead with the count, the percent, the >= 1 cm threshold, and the Open-Meteo source. Do not add a risk score. When the skeleton explains a score, keep every contributor it names, largest first, including heat, and keep the line that shows those points sum to the total. Do not shorten that list. Name each place as "<Name> hub (<Region> region)". If the skeleton says two scores are practically tied, keep that. If it says a FEMA baseline does not separate two hubs, keep each hub's own baseline value and the differentiating observed metric. Keep one relevant uncertainty from the skeleton. Do not mention a hazard the skeleton left out. Use only numbers that appear in the skeleton or the evidence. Do not invent hubs, drivers, or scores. A snow day is daily snowfall >= 1 cm. Return follow_up_questions as an empty list. The server supplies them.

## Corrections

- Hand-authored FEMA-like baselines were rejected. The snapshot uses the published risk index scores instead.
- Bonus chart and deployment work was not started after the scope was cut to the submission minimum.
