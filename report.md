# Report: AI Governance Gateway

## Problem

Organizations that expose an internal LLM to employees or internal services run into the same set of problems almost immediately, and most teams end up solving them with a pile of separate, loosely-integrated tools:

- **Identity and authorization** aren't enforced consistently — anyone who can reach the model's endpoint can ask it anything, regardless of role.
- **Sensitive data leaks through prompts and completions** — card numbers, SSNs, salary figures, internal infrastructure details, credentials — often without anyone intending to expose them.
- **Observability tooling re-introduces the leak it just prevented.** Traces, logs, and semantic caches are typically *more* permissive about what they retain than the live conversation was supposed to be, so "we mask sensitive data" and "we log everything for debugging" quietly contradict each other.
- **No usage controls.** Without rate limiting and token accounting, a single misbehaving client can both blow through cost budgets and degrade the service for everyone else.

This project is a single, self-contained gateway that solves all four in one request pipeline, rather than stitching together an auth proxy, a DLP tool, a caching layer, and an observability SDK separately.

## Approach

The gateway is built as a reverse proxy: nothing talks to the internal AI backend directly. Every request passes through a fixed pipeline before being forwarded, and every response passes back through the equivalent pipeline in reverse before reaching the caller.

The core design decision underpinning the whole project is a strict separation between two views of every request/response:

- The **live view** — what the authenticated, authorized caller is shown in the actual HTTP response, computed with their specific role's masking exemptions applied.
- The **persisted view** — what (if anything) gets written to the audit log or the semantic cache, computed completely independently, with *no* exemptions applied and document content reduced to a reference name only.

Treating these as genuinely separate computations (not two passes over the same shared state) is what makes it possible to say, with confidence, that something an authorized person legitimately saw once cannot later leak out through a trace, a log export, or a cache hit served to someone without that same authorization.

## Architecture

```
 client / app
      │
      ▼
 1. JWT authentication            (app/auth)
 2. Permission enforcement        (app/policy)
 3. Inbound data masking          (app/guardrails)
 4. Document access resolution    (app/documents)
 5. Rate limiting & token accounting (app/rate_limit)
 6. Semantic cache lookup         (app/cache)
 7. Upstream proxy call           (app/proxy)
 8. Outbound data masking         (app/guardrails)
 9. Persisted-view + audit log    (app/observability)
      │
      ▼
 internal AI backend
```

Each stage is an independently testable module. `app/main.py` wires them together behind a single catch-all route (`/v1/{path:path}`) that mirrors whatever API shape the upstream backend exposes — OpenAI-style chat completions by default.

### Key components

- **Auth** (`app/auth`) — RS256 JWT verification; claims carry user id, department, and clearance level.
- **Policy** (`app/policy`) — a YAML-defined permission matrix with clearance tiers (junior → admin) and per-department overrides, governing tool availability, restricted document tags, and masking exemptions.
- **Guardrails** (`app/guardrails`) — Microsoft Presidio + spaCy NER, plus custom recognizers for bank cards, SWIFT/BIC codes, internal IPs, AWS keys, private-key headers, proprietary source markers, and monetary amounts.
- **Documents** (`app/documents`) — a registry that checks a requester's clearance/department against a document's required clearance and restricted tags before resolving its content into a request.
- **Rate limiting & token accounting** (`app/rate_limit`) — Redis-backed sliding-window limiting, `tiktoken`-based prompt/completion counting.
- **Cache** (`app/cache`) — embedding-similarity caching over Qdrant, deliberately scoped to only the fully-masked persisted view, and only for requests that don't involve tools or document references (see README for the reasoning — some questions, like a personal PTO balance, must never be answered from cache).
- **Observability** (`app/observability`) — Langfuse audit logging of the persisted view, with an additional masking hook applied at the logging boundary as defense in depth.

## Testing & validation

The project includes an end-to-end test suite (`tests/test_gateway_e2e.py`) that runs against the real FastAPI application — real startup/shutdown lifecycle, the real Presidio/spaCy guardrails engine — rather than mocking the gateway's own internals. Two supporting pieces make this possible without external infrastructure:

- A real `fakeredis` TCP server standing in for Redis.
- A minimal real HTTP server standing in for the upstream AI backend, so the proxy layer is exercised over an actual network call, not a dynamicaly patched function.

The suite verifies, among other things:
- Masking exemptions apply correctly to the live response for an authorized role, and are denied correctly for an unauthorized one.
- The persisted/audit-log copy of a response always remains fully masked, even when the live response legitimately showed real data to an authorized caller.
- Unauthenticated requests are rejected before reaching any downstream logic.

Supporting tooling for manual/local testing was also built out: `scripts/generate_mock_jwt.py` (mints dev JWTs against a throwaway RSA keypair) and `tools/run_fake_redis.py` / `tools/run_fake_upstream.py` (standalone stand-ins for Redis and the upstream backend, for running the gateway locally without any real infrastructure).

## Limitations & future work

- **Permission model is role/department-based, not attribute-based.** A more granular ABAC model (e.g. per-record ownership) would be a natural next step for finer-grained document access.
- **Masking is entity-type based, not context-aware.** Presidio's detectors are strong for structured PII but can miss context-dependent sensitivity (e.g. a project codename that's only sensitive in combination with other terms).
- **Single upstream backend per deployment.** Routing to multiple upstream models/backends based on policy (e.g. "this department's requests go to a smaller, cheaper model") isn't yet supported.
- **Cache eligibility is currently a static rule** (no tools, no document references). A more general per-endpoint or per-intent classifier for cacheability would generalize better as more tool-backed endpoints are added.



README.md:
# AI Governance Gateway 

A policy-enforcing reverse proxy for internal LLM traffic. Every request between an application and the internal AI backend passes through this gateway first, which authenticates the caller, enforces role-based permissions, redacts sensitive data in both directions, rate-limits usage, and produces an audit trail that is structurally incapable of leaking what it just redacted.

## Purpose of this project

Organizations allowing an internal LLM for employees or services run into the same handful of problems immediately:

- **Who is allowed to ask what?** Not every employee or service should be able to pull every internal document or every category of sensitive data through the l, even indirectly.
- **What's leaking into prompts and completions?** Card numbers, SSNs, internal IP ranges, credentials, salary figures - this data shows up in both directions of LLM traffic constantly.
- **What's leaking into the logs?** Observability tooling (traces, analytics, semantic caches) is usually *more* permissive about what it stores than the live conversation was supposed to be, which quietly reintroduces the leak that was just prevented.
- **Nobody's tracking cost or usage.** Unbounded token usage and no per-user rate limiting on an LLM endpoint is a budget and availability risk.

This gateway addresses all four with a single request pipeline, rather than bolting separate tools together.

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

## Features

### Capability preflight

Before any policy filtering, masking or upstream work happens, the gateway resolves a **capability manifest** - a single, one-time snapshot of what this specific request is allowed to do, built from the caller's verified identity (`app/policy/manifest.py`). If none of the tools or documents a request actually needs are covered by that manifest, the gateway responds immediately with a clear denial instead of spending work on policy filtering, masking, and an upstream call only to discover at the very end that the answer couldn't have been shown anyway. Partial coverage (some but not all requested tools permitted) is not treated as a denial - existing per-item filtering handles that gracefully.

This is the same manifest that the foundation of the multi-agent extension plan below is built on: it's specifically designed to be narrowed and handed to a sub-agent, never re-derived from scratch or widened at each step.

### Identity & access control

JWTs (RS256, verified against a configured public key) carry `sub`, `department`, and `clearance_level` claims. A YAML-defined permission matrix(`app/policy/permissions.yaml`) maps clearance levels: junior, mid, senior, admin - to what they're allowed to do, with per-department overrides layered on top (e.g. HR's senior tier gets additional allowances that engineering's senior tier doesn't). This governs tool-call availability, restricted document tags, and masking exemptions.

### Data masking (guardrails)

Built on Microsoft Presidio with a spaCy NER pipeline, extended with custom recognizers for: 
- Bank card / credit card numbers
- SWIFT/BIC codes
- Internal/private IP ranges
- AWS access keys and private-key headers
- Proprietary source markers
- Monetary amounts

Every request and response is scanned before it leaves or enters the gateway. Matches are replaced with typed placeholders (`[MASKED_CREDIT_CARD_1]`) unless the caller's permission policy specifically exempts them from that entity type, e.g. HR staff at a given clearance level may be authorized to use a real card number on file, while everyone else sees it masked.

### Document access control

Requests can reference internal documents by ID. A document registry (`app/documents/registry.py`) checks the requester's clearance and department against each document's required clearance and restricted tags before its content is resolved into the request, so unauthorized references are stripped rather than silently passed through.

### The persisted-view guarantee

Live and persisted views of every request/response are computed independently. What a user is authorized to see live (including any masking exemptions, and including resolved document content) is never the same object graph as what gets written to the semantic cache or the audit log. The persisted copy is always built with **no exemptions applied**, using the *most restrictive* redaction baseline across the entire permission matrix, not just the actual requester's own clearance, and with document bodies reduced to just their reference name. This means that even data an authorized person legitimately saw in the live conversation cannot later leak through logs, traces, or a cache hit served to a different, less-privileged user, and the persisted/audit view always reads as if the most restricted possible viewer in the organization is the one looking at it, regardless of who actually made the request.

### Rate limiting and token accounting
A Redis-backed sliding-window throttles requests per user (`app/rate_limit/limiter.py`). Prompt and completion tokens are estimated with `tiktoken` for usage/cost tracking (`app/rate_limit/token_accounting.py`).

### Semantic caching 

Responses are cached by embedding similarity (via Qdrant, embedded-local or networked) rather than exact string match, so a rephrased question can still produce a cache hit. Consistent with the persisted-view guarantee above, only the fully-masked, non-exempt version of a response is ever cached, a cache hit always returns the safe view, regardless of the current requester's own exemptions, since the cache is shared across users of differing clearance levels.

### What does and doesn't get cached

Not every question has the same answer, especially when it's person-specific. So caching by similarity alone isn't safe for everything. The gateway draws a line between two kinds of questions:

- **Cacheable, for general questions.** "What's the parental leave policy?", "How do I file an expense report?" These questions have one correct answer regardless of who's asking or when, so serving a cached response is both safe and desirable, as it's faster, cheaper, and the answer doesn't go stale withing a reasonable TTL.
- **Not cacheable, for personal questions.** "How many holiday days do I have left?", "What's my current PTO balance?" The answer to these questions depends on *who's asking* and *what's true right now*, so caching them by semantic similarity would mean one employee's balance getting served to another employee asking a similarly-worded question, or a correct answer today becoming wrong later the moment something changes. Answers like this must always go to a live upstream call, per request, per user.

In practice this means requests are only eligible for the semantic cache when they don't reference document IDs, don't invoke tools/function-calling (which is how personal/live-data lookups are expected to be surfaced), and aren't otherwise flagged as user-specific by the request shape. Anything on that path bypasses the cache lookup and always goes to the upstream, so the response reflects the current state for that specific person rather than a similarity match to something someone else previously asked.

If the expectation is for this gateway to sit in front of the tools that expose per-user live data, keep that boundary in mind: as caching should stay scoped to answers that are true independent of *who* is asking and *when*.

### Observability

Every call is logged to Langfuse with the masked, persisted request/response, user metadata, and token counts; with an additional masking hook applied at the logging boundary as a second layer of defense against document content ever reaching the trace.

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

### Changes that make it applicable to deep-agent systems

A single-step gateway only has to answer one question: is the request/response safe? A deep-agent system needs the same question answered but for an entire *tree* of requests: a main agent handling work to a sub-agent, which may hand work to another sub-agent, several layers deep. The new implemetations exist because that tree intorduces gaps a single-step model has no way to address:

- **Permissions have to narrow monotonically down the tree.** Without the `CapabilityManifest`, there's no structural reason a sub-agent couldn't end up with more access than the querier who originally asked the questioin - it should just depend on what identity happened to get passed to it. The manifest can only shrink step by step, never grow.
- **Something has to actually make the narrowing decision.** The previous step built the mechanism; the next one's `route_delegation` is the point where "which sub-agent handles this, and with what permissions" becomes a governed decision instead of an open handoff, so a sub-agent claiming it needs a tool the manifest doesn't grant can never cause that tool to leak into its resulting permissions.
- **Content passed between agents needs the same protection that documents already had.** Most of what flows between agents in a chain: tool results, intermediate reasoning, a sub-agent's draft output - isn't a document, so it didn't have a path to being kept out of the audit trail until the next stage that generalized the sentinel-wrap pattern to any inter-agent payload.
- **Tool use is most of what a deep-agent system actually does.** A permitted tool can still be invoked with sensitive arguments or return sensitive data in its result. Next stage is JSON-aware masking closes what would otherwise be the largest blind spot in the pipeline, since sub-agents mostly act through tools, not chat messages.
- **Untrusted content flows back up the chain, not just out to the user.** Every stage before this one protected data flowing toward a less-priveleged viewer or a log. None of them protected against a sub-agen't tool result or a resolved document carrying a prompt-injection attempt back into another agent's context, so a compromised or manipulated node partway down the tree becomes an attack surface against the agents above it, not just against the end user.
- **The tree needs a hard ceiling, and it needs to be auditable.** A security control nobody can review after the fact isn't really a control. This stage combines a structural depth ceiling with chain-scoped rate limiting (catching rapid fan-out before it's deep enough to hit that ceiling) and trace stitching, so an entire multi-agent interaction shows up as one connected, reviewable trace instead of disconnected fragments.

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