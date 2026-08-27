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
 internal AI backend / LLM infra
```

Every stage is an indepedent testable module; `app/main.py` wires them together into one route (`/v1/{path:path}`) that mirrors whatever API shape your upstream backend exposes (OpenAI-style chat completions by default).

## Features

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

Live and persisted views of every request/response are computer independently. What a used is authorized to see live (including any masking exemptions, and including resolved document content) is never the same ojbect graph as what gets written to the semantic cache or the audit log. The persisted copy is always built with **no exemptions applied** and with document bodies reduced to just their reference name, so even data an authorized person legitemately saw in the live conversation cannot later leak out through logs, traces, or a cache hit server to a different, different-privileged user.

### Rate limiting and token accounting
A Redis-backed sliding-window limiter throttles requests per user (`app/rate_limit/limiter.py`). Prompt and completion tokens are estimated with `tiktoken` for usage/cost tracking (`app/rate_limit/token_accounting.py`).

### Semantic caching

Responses are cached by embeding similarity (via Qdrant, embedded-local or networked) rather than exact string match, so a rephrased question can still produce a cache hit. Consistent with the persisted-view guarantee above, only the fully-masked, non-exempt version of a response is ever cached, i.e. a cache hit always returns the safe view, regardless of the current requester's own exemptions, since the cache is shared across users of differing clearance levels.

### Observability

Every call is logged to [Langfuse](https://langfuse.com) with the masked, persisted request/response, user metadata, token counts - with an additional masking hook applied at the logging boundary as a second layer of defense against document content ever reaching the trace.

## Project structure

```
app/
  auth/            JWT verification, auth middleware
  policy/           Permission matrix schema, YAML loader, enforcement
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
  test_gateway_e2e.py     End-to-end tests against the real app + real HTTP
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
