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