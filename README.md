# AI Governance Gateway — Project Overview

## What it is

A policy-enforcing reverse proxy that sits between people (or other AI agents) and an internal AI system. Every request passes through it first. It verifies who's asking, checks what they're allowed to do, strips sensitive data out of both the request and the response, keeps usage within budget, and produces an audit trail that is structurally incapable of leaking what it just redacted — even from the people who have direct access to the logs.

**In short**: it's a security checkpoint for AI traffic, built to be trusted by security and compliance teams, not just by the engineers who wrote it.

## The problem it solves

Any organization that gives employees or internal services access to an AI system runs into the same four problems immediately:

1. **Access control** — not everyone should be able to pull every internal document or category of sensitive data through the model, even indirectly.
2. **Data leakage in the conversation itself** — card numbers, SSNs, internal infrastructure details, credentials, and salary figures show up in both directions of real AI traffic constantly, usually by accident.
3. **Data leakage into observability tooling** — logs, traces, and caches are almost always *more* permissive about what they store than the live conversation was supposed to be, which quietly re-opens the leak that access control and masking just closed.
4. **Cost and abuse control** — an AI endpoint with no rate limiting or usage tracking is a budget and availability risk waiting to happen.

Most teams solve these with four separate tools bolted together. This project solves all four with one coherent request pipeline.

## Architecture

```
 client / app
      │
      ▼
 ┌─────────────────────────────────────────────────────────────┐
 │                     AI Governance Gateway                   │
 │                                                             │
 │  1. JWT authentication            (app/auth)                │
 │  2. Permission enforcement        (app/policy)              │
 │  3. Inbound data masking          (app/guardrails)          │
 │  4. Document access resolution    (app/documents)           │
 │  5. Rate limiting & token accounting (app/rate_limit)       │
 │  6. Semantic cache lookup         (app/cache)               │
 │  7. Upstream proxy call           (app/proxy)               │
 │  8. Outbound data masking         (app/guardrails)          │
 │  9. Persisted-view + audit log    (app/observability)       │
 └─────────────────────────────────────────────────────────────┘
      │
      ▼
 Internal AI backend / LLM infra
```

Every stage is an independent testable module; `app/main.py` wires them together into one route (`/v1/{path:path}`) that mirrors whatever API shape your upstream backend exposes (OpenAI-style chat completions by default).

## Core design principle: the live view and the persisted view are never the same object

This is the single idea the whole system is built around. What a user is authorized to see *live* — including any special exemptions their role grants them — is computed completely independently from what gets written to the cache or the audit log. The persisted copy is always built using the *strictest* possible redaction baseline across the entire organization, regardless of who actually made the request. That means even data a legitimately authorized person saw once in a live
conversation can never later leak out through a log export, a trace review, or a cache hit served to someone with less access.

## Key capabilities

- **Identity-aware access control** — JWT-based authentication with a YAML-defined permission matrix mapping role/department combinations to what they can do: which tools, which document categories, which data types they're exempt from having masked.
- **Real-time data masking** — built on Microsoft Presidio with custom detectors for bank cards, SWIFT/BIC codes, internal IP ranges, cloud credentials, and monetary amounts, scanning every request and response in both directions.
- **Document access control** — internal documents can be referenced by ID; access is checked against clearance and department before content is ever pulled into a conversation.
- **The persisted-view guarantee** — a structurally separate, always maximally-redacted copy of every interaction for anything that gets logged or cached, independent of live-view exemptions.
- **Rate limiting and cost tracking** — a Redis-backed sliding-window limiter per user, with token-level usage accounting.
- **Semantic caching, safely scoped** — responses are cached by meaning (not exact text match) for genuinely reusable answers, while personal/live-state questions ("how many PTO days do I have left")  are automatically excluded from ever being served out of cache.
- **Full audit logging** — every call logged to Langfuse with the persisted (never the live) view, plus a second, independent masking pass applied at the logging boundary as defense in depth.

## Extended for multi-agent / "deep agent" systems

The project also includes a six-stage extension that generalizes every guarantee above from a single request/response pair to an entire delegation tree — a main agent handing work to sub-agents, which may hand work to further sub-agents:

1. **Capability manifest & preflight checking** — permissions are resolved once from a verified identity and can only narrow at each subsequent hop, never widen, closing the risk of a sub-agent ending up with more access than the human who originally asked.
2. **Delegation-aware routing** — an actual decision function that chooses which sub-agent handles a request, using that narrowed manifest, denying or narrowing the request rather than trusting a sub-agent's own claims about what it needs.
3. **Inter-agent payload masking** — the same "full content live, safe placeholder only when persisted" pattern used for documents, extended to any payload exchanged between agents.
4. **Tool-call and tool-result masking** — JSON-aware masking that scans inside structured tool arguments and results, not just chat messages.
5. **Adversarial content scanning** — detects prompt-injection patterns in content flowing back up a delegation chain, since a sub-agent's output is untrusted input to whatever reads it next.
6. **Recursion limits & stitched tracing** — a hard depth ceiling plus chain-scoped rate limiting, and every hop of a multi-agent interaction linked into one reviewable trace instead of disconnected fragments.

## Technical stack

| Layer | Technology |
|---|---|
| API framework | FastAPI (async), Uvicorn |
| Authentication | JWT (RS256), PyJWT, cryptography |
| Data masking | Microsoft Presidio, spaCy NER, custom pattern recognizers |
| Rate limiting / usage | Redis, tiktoken |
| Semantic cache | Qdrant (embedded or networked), sentence-transformers with a deterministic fallback embedder |
| Observability | Langfuse |
| Config | Pydantic / pydantic-settings, environment-driven |
| Testing | pytest, real end-to-end tests against the running app (fake Redis, fake upstream server — not mocks of the code itself) |

## Project structure

```
app/
  auth/            JWT verification, auth middleware
  policy/           Permission matrix schema, YAML loader, enforcement,
                      capability manifest (preflight + delegation model),
                      delegation-aware routing
  guardrails/        Presidio-based masking engine, custom recognizers,
                       JSON-aware masking for tool calls/results,
                       adversarial/prompt-injection scanning
  documents/          Document registry & authorization
  interagent/          Generalized sentinel-wrap masking for any payload
                         passed between agents (not just documents)
  rate_limit/          Sliding-window limiter, Redis client, token accounting
  cache/                Semantic cache, embeddings, Qdrant vector store
  proxy/                 Upstream HTTP client
  observability/          Langfuse audit logging
  config.py                 Centralized settings (env-driven)
  main.py                     FastAPI app & request pipeline
demo/
  demo_app.py       Standalone interactive demo (live view vs. persisted view)
scripts/
  generate_mock_jwt.py    Mint a dev JWT against a throwaway RSA keypair
tools/
  run_fake_redis.py       Standalone in-memory Redis stand-in for local dev
  run_fake_upstream.py    Standalone canned-response upstream for local dev
tests/
  test_gateway_e2e.py           End-to-end tests against the real app + real HTTP
  test_capability_manifest.py   Unit tests for the capability manifest module
  test_delegation_routing.py    Unit tests for delegation-aware routing
  test_interagent_payload.py    Unit + integration tests for inter-agent payload masking
  test_tool_call_masking.py     Unit + integration tests for tool-call/tool-result masking
  test_injection_scanning.py    Unit tests for adversarial/prompt-injection scanning
```

## Engineering practices demonstrated

- **Security-first architecture** — the persisted-view guarantee isn't bolt-on feature; it's the organizing principle the whole request pipeline is built around, and it's specifically designed so a single point of compromise (a log, a cache, an over-permissioned user) can't cascade into a full data leak.
- **Defense in depth** — sensitive content is scrubbed at multiple independent layers (document stripping, entity masking, a second masking pass at the logging SDK boundary) so a failure in one layer doesn't mean a total failure.
- **Real end-to-end testing, not mocked-around testing** — the test suite runs against the actual FastAPI app with a real (if lightweight) Redis and HTTP upstream, so passing tests are strong evidence of real behavior, not just that the code's own abstractions agree with themselves.
- **Incremental, backward-compatible extension** — the six-stage multi-agent extension was built stage by stage, each one fully tested and shippable on its own, with every new capability additive to the existing single-hop gateway rather than requiring a rewrite.
- **Conservative security defaults** — new detection capabilities (like prompt-injection scanning) ship with logging-only behavior by default and require an explicit, deliberate opt-in before they're allowed to block real traffic, reflecting an awareness that untuned automated security controls are themselves a risk to availability.

## Setup

```bash
python3 -m venv venv
source venv/bin/activate        # venv\Scripts\Activate.ps1 on Windows
pip install -r requirements.txt
python -m space download en_core_web_sm
```

If the spaCy model download is blocked in your environment, install the wheel directly instead:
```bash
pip install https://github.com/explosion/spacy-models/releases/download/en_core_web_sm-3.8.0/en_core_web_sm-3.8.0-py3-none-any.whl
```
It's also important to have Redis be reachable at `GATEWAY_REDIS_URL` (it defaults to `redis://localhost:6379/0`), which leads to either a real install (`brew install redis`) or, for local development, the bundle stand-in:
```bash
pip install fakeredis
python -m tools.run_fake_redis
```

### Configuration
 
All settings are environment-driven (prefix `GATEWAY_`), loadable from a
`.env` file. Key variables:
 
| Variable | Purpose |
|---|---|
| `GATEWAY_UPSTREAM_BASE_URL` | Base URL of the internal AI backend being proxied to |
| `GATEWAY_JWT_PUBLIC_KEY` | PEM public key used to verify incoming JWTs |
| `GATEWAY_REDIS_URL` | Redis connection string for rate limiting / token accounting |
| `GATEWAY_PERMISSIONS_FILE_PATH` | Path to the permission matrix YAML |
| `GATEWAY_SEMANTIC_CACHE_ENABLED` | Toggle the semantic cache |
| `GATEWAY_LANGFUSE_ENABLED` | Toggle audit logging to Langfuse |
 
See `app/config.py` for the complete, documented list.

### Minting a dev token
 
```bash
python scripts/generate_mock_jwt.py --user-id dev-1 --department hr --clearance senior
```
The first run generates a throwaway RSA keypair and prints the public key
to put in `GATEWAY_JWT_PUBLIC_KEY`; subsequent runs reuse it.
 
## Running the gateway
 
```bash
uvicorn app.main:app --reload --port 8080
```
 
For a quick local test without a real upstream AI backend:
```bash
python -m tools.run_fake_upstream --port 9000
```
and set `GATEWAY_UPSTREAM_BASE_URL=http://localhost:9000`.
 
## Running the interactive demo
 
```bash
uvicorn demo.demo_app:app --reload --port 8090
```
Open `http://localhost:8090` and try the document-access and
salary-masking presets to see the live view and the persisted view
side by side.

## Running the tests

```bash
pip install fakeredis pytest pytest-asyncio
pytest tests/ -v
```