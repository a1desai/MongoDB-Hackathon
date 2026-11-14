# Cooldown

**Governed AI agents keep Toronto's power on through an extreme heat wave with the least harm, and can prove every decision.**

Every action is tied to an identity, checked against versioned policy *before* it runs, recorded in an append-only, hash-chained ledger in MongoDB Atlas, and can be halted or rolled back.

Built for MongoDB's **Agents in Context** hackathon (Toronto, November 14, 2026), **Problem Statement 1: Agents You Can Audit**.

> In June 2025 Ontario hit its highest demand peak in 12 years. Cooldown replays the 2031 version of that day: a heat wave, a generator trip at 17:40, and a team of agents that has to close the gap without cutting power to the people who can least afford it.

## How it answers Track 1

| Track 1 asks | Cooldown's answer |
| --- | --- |
| Decide what an agent may do, and enforce it *before* the action | Identities with scopes and an acting-for human, versioned policies, and a deterministic Policy Gate (16 rules) that evaluates every action request before the Executor can touch any system |
| Record what the agent did, why, and under whose authority | Append-only, hash-chained ledger with schema validation. Each entry records the actor, the human it acts for, evidence (marked trusted or untrusted), policy version, per-rule results and the LangSmith trace |
| Stop or roll back an agent mid-run | A kill switch in `agent_control`, watched by a change stream. Every execution stores an undo command; rollbacks are themselves gated and logged |
| Real actions across systems | Five mock control systems (thermostats, EV chargers, batteries, SMS, feeder breakers), each with its own API, key, latency and failure quirks. The Executor uses idempotency keys and confirms "took effect" by reading state back |

## Architecture

```mermaid
flowchart LR
  subgraph World["Simulated Toronto (numpy, per tick)"]
    P[Thermal physics<br/>10,000 homes] --> G[Grid: demand vs supply]
    R[Residents, archetypes,<br/>businesses (LLM)] --> P
  end
  subgraph Agents["LangGraph (checkpointed in Atlas)"]
    F[Forecaster] --> PL[Planner<br/>LLM + fallback]
    M[(event_memory<br/>Vector Search)] --> PL
    PL --> T[Targeter] --> GATE{{Policy Gate<br/>16 deterministic rules}}
    GATE -- needs approval --> A[interrupt: human approval]
    GATE -- allow / partial --> X[Executor]
    A -- approved --> X
    X --> V[Verifier]
  end
  X -->|HTTP + Idempotency-Key| MS[Mock systems:<br/>ThermLink, ChargeFlow,<br/>PowerCell, TextLine, GridOps]
  MS --> World
  GATE & X & V & A --> L[(Ledger<br/>hash chain)]
  L -->|change stream| UI[Next.js + Mapbox<br/>agent console]
  UI -->|approve / halt / roll back| API[FastAPI]
  API --> A
```

## How MongoDB Atlas is used

Atlas is the only database: all durable state, decisions, approvals and the ledger live there.

- **Schema validation**: `$jsonSchema` validators with `validationAction: "error"` on the ledger, action requests, decisions, policies and identities, so a malformed audit record cannot be written.
- **Hash-chained ledger**: `ledger.append()` takes its sequence number from an atomic `$inc` counter and chains SHA-256 hashes; `GET /ledger/verify` recomputes the chain and reports the first broken entry.
- **Change streams**: ledger inserts stream to the console (the UI renders what is in the database, not what an agent claims), approval updates resume the paused LangGraph thread, and kill-switch updates halt the agents immediately.
- **Time series**: `feeder_load` stores demand and supply per section and tick.
- **Geospatial**: 2dsphere indexes on homes, feeder sections and facilities; `$near` finds the nearest open cool space for pre-outage notices.
- **Vector Search + Automated Embedding**: past heat events and policy documents are embedded by Atlas itself (`autoEmbed`, Voyage `voyage-4`), and the Planner retrieves similar past events with `$vectorSearch`.
- **Hybrid search**: the Auditor answers questions about a run with `$rankFusion` over Atlas Search (lexical) and Vector Search on ledger summaries.
- **LangGraph checkpoints**: every agent graph step is persisted with `langgraph-checkpoint-mongodb`, so a run survives a crash and approvals can resume a paused graph.
- **Aggregations**: metrics, governed-vs-baseline comparison and per-quintile outage equity.

## Voyage AI and LangSmith

- **Voyage AI** models generate every embedding, through Atlas Automated Embedding (no embedding pipeline in our code). `EMBED_MODE=client` switches to calling the Voyage API directly.
- **LangSmith** traces each agent call and graph run, tagged with the run id; every console card links to its trace.

## Run it

```bash
cp .env.example .env    # fill in MONGODB_URI, OPENAI_API_KEY, NEXT_PUBLIC_MAPBOX_TOKEN, LANGSMITH_API_KEY

cd backend
uv venv --python 3.12 .venv && uv pip install -r requirements.txt
.venv/bin/python -m scripts.fetch_data      # falls back to synthetic data if downloads fail
.venv/bin/python -m scripts.build_world
.venv/bin/python -m scripts.init_db         # idempotent: collections, validators, indexes, search indexes, identities, policies
.venv/bin/python -m scripts.seed_memory
.venv/bin/uvicorn app.main:app --port 8000

cd ../frontend && npm install && npm run dev   # http://localhost:3000
```

Without `OPENAI_API_KEY`, every agent uses its deterministic fallback, and the run still completes.

## Data sources and licences

- Toronto neighbourhood boundaries, Neighbourhood Profiles (2021) and the Heat Relief Network: City of Toronto Open Data, [Open Government Licence – Toronto](https://open.toronto.ca/open-data-license/).
- Hourly weather: Environment and Climate Change Canada, [Open Government Licence – Canada](https://open.canada.ca/en/open-government-licence-canada).
- Ontario hourly demand (curve shape only): IESO public reports, used with attribution.

Each download falls back to a synthetic generator if it fails; `data/raw/sources.json` records which sources were used.

## Synthetic assumptions

Households, AC types, thermostat, EV and battery ownership, program consent, vulnerability flags, thermal parameters and comfort limits are synthetic, drawn from seeded distributions per PLAN.md §5.2 and calibrated so the uncontrolled 17:00 peak matches the scenario's fictional target. Impacts are reported in real-scale homes (simulated count × scale).

## Ethics

The population is synthetic and every operational name is fictional (CoolShift, ThermLink, ChargeFlow, PowerCell, TextLine, Lakeshore Grid Ops, and all residents and businesses). Vulnerability flags are synthetic attributes. Outbound messages contain only energy offers, event timing and public cool-space locations, never health or medical advice, and the Policy Gate enforces this.

## Built during the hackathon (Nov 14 2026, 10:45–17:00)

Everything in this repository: the simulation, the Policy Gate, the ledger, the executor and mock systems, the LangGraph agents, the API and the frontend. Concept inspired by a previous team idea; all code in this repository was written during the event.
