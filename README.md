# GridLock

**Governed AI agents keep Toronto's power on through an extreme heat wave with the least harm, and can prove every decision.**

Every action is tied to an identity, checked against versioned policy *before* it runs, recorded in an append-only, hash-chained ledger in MongoDB Atlas, and can be halted or rolled back.

Built for MongoDB's **Agents in Context** hackathon (Toronto, November 14, 2026), **Problem Statement 1: Agents You Can Audit**.

> In June 2025 Ontario hit its highest demand peak in 12 years. GridLock replays the 2031 version of that day: a heat wave, a generator trip at 17:40, and a team of agents that has to close the gap without cutting power to the people who can least afford it.

## How it answers Track 1

| Track 1 asks | GridLock's answer |
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
cp .env.example .env    # fill in MONGODB_URI and any LLM or tracing keys you use

cd backend
uv venv --python 3.12 .venv && uv pip install -r requirements.txt
.venv/bin/python -m scripts.fetch_data      # falls back to synthetic data if downloads fail
.venv/bin/python -m scripts.build_world
.venv/bin/python -m scripts.init_db         # idempotent: collections, validators, indexes, search indexes, identities, policies
.venv/bin/python -m scripts.seed_memory
.venv/bin/uvicorn app.main:app --port 8000
```

Without `OPENAI_API_KEY`, every agent uses its deterministic fallback, and the run still completes.

From the repo root, in a second terminal, start the prompt screen on `http://localhost:5173`.

```bash
cd cooldown-react
cp .env.example .env.local    # set VITE_MAPBOX_TOKEN to a public Mapbox token
npm ci
npm run dev
```

Select an electrical zone to see reported IESO demand for June 24, 2025. The Toronto panel also shows the hourly airport temperature and the sum of household estimates in the 2021 neighbourhood profiles. During a run, the observations follow the simulation clock's hour in Toronto. See the [React app README](cooldown-react/README.md) for setup and controls.

Enter a prompt, select **Generate scenario**, and review the inferred settings before running it. The designer saves custom scenarios in MongoDB and can extract numbers from a prompt without an LLM key. **Toronto heatwave** starts the built-in scenario, which uses the published temperature profile and scales the IESO Toronto-zone demand pattern to the model's commercial load. Rerun `scripts.fetch_data` and `scripts.build_world` on an existing checkout to prepare the map observations and hydro lines.

Open **Agent activity** to inspect the ordered plan, written rationale, policy checks, approvals, and verified relief. Select **Show feeders on map** inside an action to locate its simulated targets beside the published hydro lines.

**Policies** shows the active rule set and its thresholds. The deterministic Policy Gate automatically authorizes eligible actions and restricts partial approvals to eligible targets. Feeder outages, large-impact actions, and other requests that trigger review rules wait for an on-shift supervisor. New decisions store the applied policy settings and version in the audit ledger for later review.

For audit and additional operator controls, start the Next.js run console in a third terminal. Set `NEXT_PUBLIC_MAPBOX_TOKEN` in `frontend/.env.local` to use its Toronto map. The activity panel links to the same run.

```bash
cd frontend
npm ci
npm run dev    # http://localhost:3000
```

## Data sources and licences

- Toronto neighbourhood boundaries, Neighbourhood Profiles (2021) and the Heat Relief Network: City of Toronto Open Data, [Open Government Licence – Toronto](https://open.toronto.ca/open-data-license/).
- Hourly weather for June 24, 2025: [Environment and Climate Change Canada](https://api.weather.gc.ca/collections/climate-hourly?lang=en). The simulation interpolates between observations without a temperature offset.
- Ten zone boundaries and reported hourly demand: [IESO Zonal Map](https://www.ieso.ca/localContent/zonal.map/index.html) and [Hourly Zonal Demand Report](https://reports-public.ieso.ca/public/DemandZonal/). The boundary and demand data are subject to [IESO's terms of use](https://www.ieso.ca/Terms-of-Use).
- Hydro and submerged hydro line locations: [Ontario Utility Line dataset](https://data.ontario.ca/dataset/utility-line). The simulated feeders have independent geometry and breaker state.

`data/raw/sources.json` records download status. The simulation has fallback inputs for offline use. The map reports missing observations as unavailable.

## Synthetic assumptions

Households, AC types, thermostat, EV and battery ownership, program consent, vulnerability flags, thermal parameters and comfort limits are synthetic, drawn from seeded distributions per PLAN.md §5.2 and calibrated so the uncontrolled 17:00 peak matches the scenario's fictional target. Impacts are reported in real-scale homes (simulated count × scale).

## Ethics

The population is synthetic and every operational name is fictional (CoolShift, ThermLink, ChargeFlow, PowerCell, TextLine, Lakeshore Grid Ops, and all residents and businesses). Vulnerability flags are synthetic attributes. Outbound messages contain only energy offers, event timing and public cool-space locations, never health or medical advice, and the Policy Gate enforces this.

## Built during the hackathon (Nov 14 2026, 10:45–17:00)

Everything in this repository: the simulation, the Policy Gate, the ledger, the executor and mock systems, the LangGraph agents, the API and the frontend. Concept inspired by a previous team idea; all code in this repository was written during the event.
