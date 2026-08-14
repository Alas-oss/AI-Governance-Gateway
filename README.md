# AI Governance Gateway

A stateless FastAPI reverse proxy that sits between internal developers and core AI Agent/LLM infrastructure, enforcing auth, tool/data-access policy, PII masking, rate limiting, and compliant logging before anything reaches the model or observability tools.

So far I've built the Auth part including: core proxy, routing, enterprice JWT authorization - and policy & proxy part that encapsules: dynamic tool stripping / system-prompt redaction by clearance level. Next I'm planning on adding Presidio masking, Redis rate limiting, semantic cache, Langfuse.

## Project layout

```
app/
  main.py              FastAPI app, catch-all proxy route, lifespan wiring
  config.py             Settings (env-driven, GATEWAY_ prefix)
  auth/
    jwt_utils.py         Token verification + UserContext extraction
    middleware.py         JWTAuthMiddleware 
  policy/
    schema.py             Pydantic schema for permissions.yaml
    permissions.yaml       Clearance -> allowed tools / redaction tags
    enforcement.py          Tool filtering + system-prompt redaction 
  proxy/
    client.py              Async httpx upstream client 
scripts/
  generate_mock_jwt.py    Mint a mock RS256 JWT for local testing 
tests/
  test_auth.py             Unit + integration tests for auth 
  test_policy_enforcement.py  Junior-vs-senior policy tests 
  fixtures/permissions.yaml
requirements.txt
.env.example
```

## Setup

```powershell
python -m venv .venv
& .venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

## Generate a mock JWT for local testing 

The gateway expects the enterprise IdP to issue RS256 JWTs with custom claims `department` and `clearance_level` (one of `junior`, `mid`, `senior`, `admin`), in addition to the standard `subs`/`iat`/`exp`.

```powershell
python scripts/generate_mock_jwt.py --user-id dev-42 --department payments --clearance senior
```
This prints a signed token and the PEM public key needed to verify it. 
Copy the public key into `GATEWAY_JWT_PUBLIC_KEY` (from `.env`).

## Run the gateway

```powershell
Copy-Item .env.example .env
uvicorn app.main:app --reload --port 8080
```
Send a request:

```
$headers = @{
    "Authorization" = "Bearer <token from generate_mock_jwt.py>"
    "Content-Type"  = "application/json"
}

$body = @{
    model    = "internal-agent-v1"
    messages = @(...) # Replace with your array items
    tools    = @(...) # Replace with your array items
} | ConvertTo-Json -Depth 10

Invoke-RestMethod -Uri "http://localhost:8080/v1/chat/completions" -Method Post -Headers $headers -Body $body
```

- Missing/expired/invalid token -> `401 Unauthorized`
- Unauthorized tools in `tools[]` -> silently stripped per `permissions.yaml`
- System-prompt fragments wrapped in `[INTERNAL-SENIOR-ONLY]...[/INTERNAL-SENIOR-ONLY]` (or similar tags) -> redacted for clearance levels that don't have that tag in their `restricted_doc_tags` allowlist

## Editing the permission matrix

Edit `pp/policy/permissions.yaml`. Each clearance level defines:

- `allowed_tools` - exact tool/function names permitted
- `allowed_tool_prefixes` - namespace prefixes permitted (e.g. `internal.readonly.*`)
- `restricted_doc_tags` - system-prompt tags stripped for that level
- `allow_all_tools` - bypass filtering entirely (reserved for `admin`)

The matrix is loaded once at startup and cached; restart the process (or add an admin reload endpoint) to pick changes.
