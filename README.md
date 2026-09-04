# AI Governance Gateway

A policy-enforcing reverse proxy for internal LLM traffic. Every request between an application and the internal AI backend passes through this gateway first, which authenticates the caller, enforces role-based permissions, redacts sensitive data in both directions, rate-limits usage, and produces an audit trail that is structurally incapable of leaking what it just redacted.

## Purpose of this project

Organizations allowing an internal LLM for employees or services run into the same handful of problems immediately:

- **Who is allowed to ask what?** Not every employees or services should be able to pull every internal documnet or every category of sensitive data through the model, even indirectly.
- **What's leaking into prompts and completions?** Card numbers, SSNs, internal IP ranges, credentials, salary figures - this data shows up in both directions of LLM traffic constantly. 
- **What's leaking into the logs?** Observability tooling (traces, analytics, semantic caches) is usually *more* permissive about what it stores than the live conversation was supposed to be, which quietly reintroduces the leak was just prevented.
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

Every stage is an indepedent testable module; `app/main.py` wires them together into one route (`/v1/{path:path}`) that mirrors whatever API shape your upstream backend exposes (OpenAI-style chat completions by default).

## Features

### Capability preflight

Before any policy filtering, masking or upstream work happens, the gateway resolves a **capability manifest** - a single, one-time snapshot of what this specific request is allowed to do, built from the caller's verified identity (`app/policy/manifest.py`). If none of the tools or documents a request actually needs are coeverd by that manifest, the gateway responds immediately with a clear denial instead of spending work on policy filtering, masking, and an upstream call only to discover at the very end that the answer couldn't have been shown anyway. Partial coverage (some but not all requested tools permitted) is not treated as a denial - existing per-item filtering handles that gracefully.

This is the same manifest that the foundation of the multi-agent extension plan below is built on: it's specifically designed to be narrowed and handed to a sub-agent, never re-derived from scratch or widened at each step.

### Identity & acess control
JWTs (RS256, verified against a configured public key) carry `sub`, `department`, and `clearance_level` claims. A YAML-defined permission matrix (`app/policy/permissions.yaml`) maps clearance levels: junior, mid, senior, admin - to what they're allowed to do, with per-department overrides layered on top (e.g. HR's sentior tier gets additional allowances that engineering's senior ties doesn't). This governs tool-call availability, restricted document tags, and masking exemptions.

### Data masking (guardrails)

Built on [Microsoft Presidio] with a spaCy NER pipeline, extended with custom recognizers for: 
- Bank card / creadit card numbers
- SWIFT/BIC codes
- Internal/private IP ranges
- AWS access keys and private-key headers
- Proprietary source markers
- Monetary amounts

Every request and response is scanned before it leaves or enters the gateway. Matches are replaced with typed placeholders (`[MASKED_CREDIT_CARD_1]`) unless the caller's permission policy specifically exempts them from that entity type, e.g. HR stadd at a given clearance level may be authorized to see a real card number on file, while everyone else sees it masked.

### Document access control

Request can reference internal documents by ID. A document registry (`app/documents/registry.py`) checks the requester's clearance and department against each document's required clearance and restricted tags before its content is resolved into the request, so unauthorized references are stripped rather than silently passed through.

### The persisted-view guarantee

Live and persisted views of every request/response are computed independently. What a user is authorized to see live (including any masking exemptions, and including resolved document content) is never the same object graph as what gets written to the semantic cache or the audit log. The persisted copy is always built with **no exemptions applied**, using the *most restrictive* redaction baseline across the entire permission matrix - not just their reference name. This means even data an authorized person legitimately saw in the live conversation cannot later leak out through logs, traces, or a cache hit served to a different, less-priveleged user, traces, or a cache hit served to a different, less-priveleged user, and the persisted/audit view always reads as if the most restricted possible viewer in the organization is the one looking at it, regardless of who actually made the request.

### Rate limiting and token accounting
A Redis-backed sliding-window limiter throttles requests per user (`app/rate_limit/limiter.py`). Prompt and completion tokens are estimated with `tiktoken` for usage/cost tracking (`app/rate_limit/token_accounting.py`).

### Semantic caching

Responses are cached by embeding similarity (via Qdrant, embedded-local or networked) rather than exact string match, so a rephrased question can still produce a cache hit. Consistent with the persisted-view guarantee above, only the fully-masked, non-exempt version of a response is ever cached, i.e. a cache hit always returns the safe view, regardless of the current requester's own exemptions, since the cache is shared across users of differing clearance levels.

### What does and doesn't get cached

Not every question has the same answer, especially when they are person-specific. So caching by similarity alone isn't safe for everything. The gateway draws a line between two kinds of questions:

- **Cacheable, for general questions.** "What's the parental leave policy?", "How do I file an expense report?" These are the questions have one correct answer regardless of who's asking or when, so serving a cached response is both safe and desirable, so it's faster and cheaper, and the answer doesn't go stale within a reasonable TTL.
- **Not cacheable, for personal questions.** "How many holiday days do I have left?", "What's my current PTO balance?" The answer to these questions depend on *who's asking* and *what's true right now*, so caching them by semantic similarity would mean one employee's balance getting served to another employee asking a similarly-worded questions, or a correct ansewr today becoming wrong later the moment something changes. Ansewrs like this must always go to the live upstream call, per request, per user.

In practice this means requests are only eligible for the semantic cache when they don't reference document IDs, don't invoke tools/function-calling (which is how personal/live-data lookups are expected to be surfaced), and aren'totherwise flagged as user-specific by the request shape. Anything on that path bypasses the cache lookup and always goes to the upstream, so the response reflects the current state for that specific person rather than a similarity match to something someone else previously asked.
 
If you're extending this gateway to sit in front of tools that expose per-user live data, keep that boundary in mind: caching should stay scoped to answers that are true independent of *who* is asking and *when*.

### Observability

Every call is logged to [Langfuse](https://langfuse.com) with the masked, persisted request/response, user metadata, token counts - with an additional masking hook applied at the logging boundary as a second layer of defense against document content ever reaching the trace.

## Project structure

```
app/
  auth/            JWT verification, auth middleware
  policy/           Permission matrix schema, YAML loader, enforcement,
                      capability manifest (preflight + delegation model)
  guardrails/        Presidio-based masking engine, custom recognizers
  documents/          Document registry & authorization
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
  test_gateway_e2e.py         End-to-end tests against the real app + real HTTP
  test_capability_manifest.py Unit tests for the capability manifest module
```

## Setup
 
```bash
python3 -m venv venv
source venv/bin/activate        # venv\Scripts\Activate.ps1 on Windows
pip install -r requirements.txt
python -m spacy download en_core_web_sm
```
 
If the spaCy model download is blocked in your environment, install the
wheel directly instead:
```bash
pip install https://github.com/explosion/spacy-models/releases/download/en_core_web_sm-3.8.0/en_core_web_sm-3.8.0-py3-none-any.whl
```
 
You'll also need Redis reachable at `GATEWAY_REDIS_URL` (defaults to
`redis://localhost:6379/0`) — either a real install (`brew install redis`)
or, for local development, the bundled stand-in:
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

```bah
pip install fakeredis pytest pytest-asyncio
pytest test/ -v
```

---
## Extending this to a multi-agent / deep-agent system

Everything above describes a single step: one caller, one gateway pass, one upstream call. A deep-agent system: a main agent that delegates work to one or more sub-agents, possibly several layers deep - needs the same guarantees to hold across the *whole* delegation chain, not just at the edges. A sub-agent three steps deep should never be able to see more than the human who originally asked the question was allowed to see, and nothing exchanged between agetns shoul dleak into the audit trail any momre than a single-step request already does.

the plan below shows how this gateway is going to sit at every step of that chain, not just at the outermost part, it's going to be built incrementally so each stage is usable and tested on its own before the next one gets published, so that nothing that depends on each other would fall domino style due to rushed development.

### Stage 1 - Capability manifest and preflight checking (DONE)

Every checkpoint in the single-step gateway currently re-derivs permissions from a raw identity independently, whereever it happens to run in the pipeline. That's fine for one step; it stops being fine when there's a delegation chain, since nothing then stops a sub-agent from ending up with more access than the original caller.

this stage introduces the `CapabilityManifest` (`app/policy/manifest.py`): a single object, resolved once form the caller's verified identity before any agent does any work, that states exactly what tools and documents this request chain may use. It supports `narrow()` - producing the manifest for the next hep, which can only have equal or fewer capabilities than the current one, never more - and a depth ceiling that raises rather than silently allowing unbounded delegation. A preflight check runs this manifest against the incoming request immediately: if nothing the request needs is actually usable, the gateway denies it right away instead of doing a full round trip throuhg masking and the upstream call only to discover the denial once it's time to respond.

### Stage 2 - Delegation-aware rounting

Wire the capability manifest into the actual main-agent to sub-agent handoff: the main agent resolves a manifest for the incoming qquery, and either routes to the sub-agent whose task matches, or - if the manifest can't cover what the query needs at all - so it responds directly with a clearance explanation, or delegates a narrower, redacted version of the permitted. Each sub-avent recieves a `narrow()`ed manifest, never the original caller's raw identity or a freshly-resovled one.

### Stage 3 - Inter-agent payload masking

Generalize the sentinel-wrap pattern this gateway already uses for document content (`resolve_document_references` / `strip_document_content`) to any payload passed between agents. A sub-agent recieves the full content it needs to do its job; only a sentinel reference to that payload is ever eligible to reach the persisted/cache view for that step, the same way document bodies are already handled today.

### Stage 4 - Tool-call and tool-result checkpointing

A tool being permitted (per the capability manifest) doesn't mean its *output* is automatically safe to persist - a permitted lookup tool can still return a real SSN or account number in its result. Extend the existing inbound/outbound masking pipeline to run against tool call arguments and tool call results, not only chat messages.

### Stage 5 - Adversarial content scanning

Sensitive data flowing *out* of the system isn't only risk in a multi-agent chain - content flowing back *up* the chain (a sub-agent's tool result, a resolved document) is untrusted input to whatever agent revies it next, and could contain a prompt-injection attempt. Add a recognizer, registered alongside the existing PII.secret recognizers in `app/guardrails/recognizers.py`, tht flags known injection patterns rather than masking them, which is the same infrastructure, a different action taken on a match.

### Stage 6 - Recursion limits and stitched tracing

combine the manifest's depth ceiling (a hard, structural limit) with the existing Redis-backed rate limiter keyed per delegation chain rather than per user, so a single session can't generate unbounded fan-out even within one chain. Give every step in a delegation tree a shared trace/session ID in Langfuse with parent'child span relationships, so a reviewer can follow an entire multi-agent interaction as one trace, using the same most-restrictive redaction baseline the persisted-view guarantee already applies today, extended to cover every span in the chain rather than one request's persisted view in isolation.


Each stage is designed to be independently shippable and tested - the single-hop gateway document above keeps working unmodified at every stage of this plan; each stage only adds what's needed for the next laer of delegation to be safe.