from __future__ import annotations

import copy
import re
from typing import Any, Dict, List, Optional

from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from app.auth.jwt_utils import ClearanceLevel, UserContext
from app.cache.embeddings import EmbeddingEngine
from app.cache.semantic_cache import SemanticCache, extract_cache_query_text
from app.cache.vector_store import QdrantVectorStore
from app.config import get_settings
from app.documents.pipeline import generate_request_token, payload_references_documents, resolve_document_references
from app.documents.registry import DocumentRecord, DocumentRegistry
from app.guardrails.engine import get_engine, init_guardrails_engine
from app.guardrails.pipeline import build_persisted_view
from app.observability.langfuse_logger import AuditLogger
from app.policy.enforcement import (
    enforce_policy_on_payload,
    get_masking_exempt_entities,
    redact_payload_for_persisted_view,
)

app = FastAPI(title="AI Governance Gateway -- Live Demo")

settings = get_settings()
audit_logger = AuditLogger(settings)

semantic_cache: SemanticCache | None = None

demo_document_registry = DocumentRegistry()
demo_document_registry.register(
    DocumentRecord(
        doc_id="comp_plan_2026",
        name="Q3 Compensation Plan.pdf",
        content=(
            "Executive base salaries range from $180,000 to $340,000 depending on level. "
            "Bonus targets are 20-35% of base, paid quarterly."
        ),
        required_clearance=ClearanceLevel.SENIOR,
        required_department="hr",
    )
)
demo_document_registry.register(
    DocumentRecord(
        doc_id="onboarding_guide",
        name="New Hire Onboarding Guide.pdf",
        content="Day 1: IT setup and badge photo. Day 2: benefits enrollment. Day 3: team introductions.",
        required_clearance=None,  
        required_department=None, 
    )
)


@app.on_event("startup")
async def startup() -> None:
    global semantic_cache
    init_guardrails_engine(settings)

    embeddings = EmbeddingEngine(
        model_name=settings.embedding_model_name, fallback_dimensions=settings.embedding_fallback_dimensions
    )
    vector_store = QdrantVectorStore(
        path=settings.semantic_cache_path + "_demo",
        collection_name="demo_semantic_cache",
        dimensions=embeddings.dimensions,
    )
    semantic_cache = SemanticCache(embeddings, vector_store, similarity_threshold=settings.semantic_cache_similarity_threshold)


class DemoRequest(BaseModel):
    clearance_level: str
    department: str = "engineering"
    system_prompt: str = ""
    user_message: str = ""
    tools: List[str] = []


def _tool_name(tool_def: Dict[str, Any]) -> Optional[str]:
    if isinstance(tool_def.get("function"), dict):
        return tool_def["function"].get("name")
    return tool_def.get("name")


def _mask_messages_with_findings(
    messages: List[Dict[str, Any]], exempt_entities: Optional[List[str]] = None
) -> tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    engine = get_engine()
    masked_messages = []
    all_findings: List[Dict[str, Any]] = []

    for message in messages:
        content = message.get("content")
        if not isinstance(content, str):
            masked_messages.append(message)
            continue
        result = engine.mask_text(content, exempt_entities=exempt_entities)
        masked_messages.append({**message, "content": result.text})
        for finding in result.findings:
            all_findings.append(
                {
                    "entity_type": finding.entity_type,
                    "placeholder": finding.placeholder,
                    "score": round(finding.score, 3),
                    "message_role": message.get("role"),
                    "masked": finding.masked,
                }
            )
    return masked_messages, all_findings


_CARD_PATTERN = re.compile(r"\b(?:\d[ -]?){13,19}\b")
_MASKED_REF_PATTERN = re.compile(r"\[MASKED_[A-Z_]+_\d+\]")
_DOCUMENT_TAG_PATTERN = re.compile(r"\[\[DOCUMENT:([A-Za-z0-9_\-]+)\]\]")


def _simulate_agent_response(payload: Dict[str, Any], document_names_available: List[str]) -> str:
    messages = payload.get("messages", [])
    tool_names = {_tool_name(t) for t in payload.get("tools", [])}
    user_messages = [m for m in messages if m.get("role") == "user"]
    last_user_text = user_messages[-1]["content"] if user_messages else ""

    masked_ref = _MASKED_REF_PATTERN.search(last_user_text)
    card_match = _CARD_PATTERN.search(last_user_text)
    wants_write = any(kw in last_user_text.lower() for kw in ("update", "write", "change", "modify", "set"))
    system_text = " ".join(m.get("content", "") for m in messages if m.get("role") == "system")

    if document_names_available:
        names = ", ".join(document_names_available)
        return f"Based on {names}, here's a summary of the relevant sections you asked about."

    if "access not authorized" in last_user_text or "access not authorized" in system_text:
        return "I can see a document was referenced here, but I don't have access to open it, so I can't use its contents."

    if masked_ref:
        return (
            f"I can see a reference to {masked_ref.group(0)} in this request, but I can't tell you "
            f"what the original value was -- it was never included in what I received."
        )

    if card_match and wants_write:
        if "db.write_query" in tool_names:
            return f"Done -- I've updated the record with card number {card_match.group(0)}."
        return (
            f"I can see the card number {card_match.group(0)} in your message, but I wasn't given "
            f"a tool that can write that to the database, so I can't complete this myself."
        )

    if card_match:
        return f"Confirmed -- the card number on this request is {card_match.group(0)}."

    if "REDACTED" in system_text:
        return "I don't have that information available to answer with."

    if wants_write and "db.write_query" in tool_names:
        return "Done -- I've made that update."

    if wants_write:
        return "I understand the request, but I wasn't given a tool that can make that change myself."

    return "Happy to help -- based on what you've told me, here's a general answer to your question."


@app.post("/api/run")
async def run_pipeline(req: DemoRequest) -> Dict[str, Any]:
    try:
        clearance = ClearanceLevel(req.clearance_level)
    except ValueError:
        return {"error": f"Unknown clearance level '{req.clearance_level}'"}

    user = UserContext(user_id="demo-user", department=req.department, clearance_level=clearance)

    messages = []
    if req.system_prompt.strip():
        messages.append({"role": "system", "content": req.system_prompt})
    if req.user_message.strip():
        messages.append({"role": "user", "content": req.user_message})

    raw_payload: Dict[str, Any] = {
        "model": "internal-agent-v1",
        "messages": messages,
        "tools": [{"type": "function", "function": {"name": name}} for name in req.tools],
    }

    after_policy = enforce_policy_on_payload(copy.deepcopy(raw_payload), user, settings)

    raw_tool_names = {_tool_name(t) for t in raw_payload["tools"]}
    kept_tool_names = {_tool_name(t) for t in after_policy.get("tools", [])}
    stripped_tools = sorted(raw_tool_names - kept_tool_names)

    system_prompt_redacted = False
    for raw_msg, policy_msg in zip(raw_payload["messages"], after_policy.get("messages", [])):
        if raw_msg.get("role") == "system" and raw_msg.get("content") != policy_msg.get("content"):
            system_prompt_redacted = True

    document_token = generate_request_token()
    has_document_reference = payload_references_documents(after_policy)
    document_names_resolved: List[str] = []
    if has_document_reference:
        after_policy, any_resolved = resolve_document_references(
            after_policy, user, demo_document_registry, document_token
        )
        if any_resolved:
            for message in after_policy.get("messages", []):
                content = message.get("content", "")
                for match in re.finditer(r"\u27e6DOC:[0-9a-f]+:([^\u27e7]+)\u27e7", content):
                    document_names_resolved.append(match.group(1))

    exempt_entities = get_masking_exempt_entities(user, settings)
    after_masking_messages, findings = _mask_messages_with_findings(
        after_policy.get("messages", []), exempt_entities=exempt_entities
    )
    live_view = {**after_policy, "messages": after_masking_messages}

    persisted_source, had_restricted_context = redact_payload_for_persisted_view(after_policy, settings)
    persisted_view = build_persisted_view(
        persisted_source, get_engine(), document_token=document_token if has_document_reference else None
    )

    cacheable = not bool(after_policy.get("tools")) and not has_document_reference
    cache_query_text = ""
    cache_hit = False
    cache_similarity: Optional[float] = None
    lookup_result = None
    if cacheable and semantic_cache is not None:
        cache_query_text = extract_cache_query_text(persisted_view)
        lookup_result = semantic_cache.lookup(cache_query_text)
        if lookup_result.hit and lookup_result.response_payload is not None:
            cache_hit = True
            cache_similarity = lookup_result.similarity_score

    simulated_without_gateway = _simulate_agent_response(raw_payload, [])

    if cache_hit:
        persisted_response = lookup_result.response_payload
        simulated_with_gateway = persisted_response["choices"][0]["message"]["content"]
    else:
        simulated_with_gateway = _simulate_agent_response(live_view, document_names_resolved)

        if document_names_resolved:
            persisted_response = {
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": "[RESPONSE OMITTED FROM AUDIT LOG: generated using a restricted document]",
                        }
                    }
                ]
            }
        elif had_restricted_context:
            persisted_response = {
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": "[RESPONSE OMITTED FROM AUDIT LOG: generated using restricted internal context]",
                        }
                    }
                ]
            }
        else:
            persisted_response = {"choices": [{"message": {"role": "assistant", "content": simulated_with_gateway}}]}

        if cacheable and semantic_cache is not None and cache_query_text:
            semantic_cache.store(cache_query_text, persisted_response)

    try:
        audit_logger.log_call(
            user_id=user.user_id,
            department=user.department,
            masked_request=persisted_view,
            masked_response=persisted_response,
            prompt_tokens=0,
            completion_tokens=0,
            cache_hit=cache_hit,
        )
    except Exception:  # noqa: BLE001 - demo-only: never let logging break the demo response
        pass

    return {
        "raw_payload": raw_payload,
        "after_policy": after_policy,
        "live_view": live_view,
        "persisted_view": persisted_view,
        "simulated_response_without_gateway": simulated_without_gateway,
        "simulated_response_with_gateway": simulated_with_gateway,
        "langfuse": {
            "enabled": audit_logger.enabled,
            "payload_sent": {"masked_request": persisted_view, "masked_response": persisted_response},
        },
        "cache": {
            "cacheable": cacheable,
            "hit": cache_hit,
            "similarity_score": round(cache_similarity, 3) if cache_similarity is not None else None,
        },
        "summary": {
            "clearance_level": clearance.value,
            "department": user.department,
            "stripped_tools": stripped_tools,
            "system_prompt_redacted": system_prompt_redacted,
            "masking_exempt_entities": exempt_entities,
            "masking_findings": findings,
            "has_document_reference": has_document_reference,
            "documents_resolved": document_names_resolved,
            "cacheable": cacheable,
        },
    }


@app.get("/", response_class=HTMLResponse)
async def index() -> str:
    return _PAGE_HTML


_PAGE_HTML = r"""
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8" />
<title>AI Governance Gateway -- Live Demo</title>
<style>
  :root { --bg: #0f1115; --panel: #171a21; --border: #2a2f3a; --text: #e6e8ec; --muted: #8b93a3;
          --accent: #5b8cff; --danger: #ff6b6b; --ok: #3ddc97; }
  * { box-sizing: border-box; }
  body { margin: 0; background: var(--bg); color: var(--text); font-family: -apple-system, Segoe UI, Roboto, sans-serif; }
  header { padding: 20px 28px; border-bottom: 1px solid var(--border); }
  header h1 { margin: 0 0 4px 0; font-size: 20px; }
  header p { margin: 0; color: var(--muted); font-size: 13px; }
  .layout { display: grid; grid-template-columns: 360px 1fr; gap: 0; min-height: calc(100vh - 78px); }
  .sidebar { padding: 20px; border-right: 1px solid var(--border); }
  .main { padding: 20px; overflow-x: auto; }
  label { display: block; font-size: 12px; color: var(--muted); margin: 14px 0 4px; }
  select, textarea, input { width: 100%; background: var(--panel); color: var(--text); border: 1px solid var(--border);
    border-radius: 6px; padding: 8px; font-size: 13px; font-family: inherit; }
  textarea { min-height: 70px; resize: vertical; font-family: ui-monospace, monospace; }
  .tools { display: flex; flex-direction: column; gap: 6px; margin-top: 4px; }
  .tools label { display: flex; align-items: center; gap: 6px; font-size: 12px; color: var(--text); margin: 0; }
  .presets { display: flex; flex-direction: column; gap: 6px; margin-top: 4px; }
  button.preset { background: var(--panel); border: 1px solid var(--border); color: var(--text); text-align: left;
    padding: 8px 10px; border-radius: 6px; cursor: pointer; font-size: 12px; }
  button.preset:hover { border-color: var(--accent); }
  button.run { width: 100%; margin-top: 18px; background: var(--accent); color: white; border: none;
    padding: 10px; border-radius: 6px; font-weight: 600; cursor: pointer; font-size: 14px; }
  button.run:hover { opacity: 0.9; }
  .columns { display: grid; grid-template-columns: 1fr 1fr 1fr; gap: 14px; margin-top: 14px; }
  .columns2 { display: grid; grid-template-columns: 1fr 1fr; gap: 14px; margin-top: 14px; }
  .panel { background: var(--panel); border: 1px solid var(--border); border-radius: 8px; padding: 12px; }
  .panel h3 { margin: 0 0 8px 0; font-size: 13px; color: var(--muted); text-transform: uppercase; letter-spacing: 0.04em; }
  pre { margin: 0; white-space: pre-wrap; word-break: break-word; font-size: 12px; line-height: 1.5;
    font-family: ui-monospace, monospace; }
  .summary { margin-top: 14px; background: var(--panel); border: 1px solid var(--border); border-radius: 8px; padding: 14px; }
  .summary h3 { margin: 0 0 10px 0; font-size: 13px; color: var(--muted); text-transform: uppercase; }
  .badge { display: inline-block; padding: 3px 8px; border-radius: 999px; font-size: 11px; margin: 2px 4px 2px 0; }
  .badge.stripped { background: rgba(255,107,107,0.15); color: var(--danger); border: 1px solid rgba(255,107,107,0.4); }
  .badge.masked { background: rgba(91,140,255,0.15); color: var(--accent); border: 1px solid rgba(91,140,255,0.4); }
  .badge.ok { background: rgba(61,220,151,0.15); color: var(--ok); border: 1px solid rgba(61,220,151,0.4); }
  .empty { color: var(--muted); font-size: 12px; }
  code.inline { background: var(--panel); border: 1px solid var(--border); border-radius: 4px; padding: 1px 5px; font-family: ui-monospace, monospace; }
</style>
</head>
<body>
<header>
  <h1>AI Governance Gateway &mdash; Live Demo</h1>
  <p>Runs the real policy, masking, persisted-view, and document-access code against whatever you type below.
     The response panels are clearly-labeled <strong>simulations</strong> (no live AI agent connected) --
     everything else runs the actual production code.</p>
</header>
<div class="layout">
  <div class="sidebar">
    <label>Clearance level</label>
    <select id="clearance">
      <option value="junior">junior</option>
      <option value="mid">mid</option>
      <option value="senior">senior</option>
      <option value="admin">admin</option>
    </select>

    <label>Department</label>
    <input id="department" value="engineering" />

    <label>System prompt (optional)</label>
    <textarea id="systemPrompt" placeholder="e.g. You are a helpful assistant.&#10;[INTERNAL-SENIOR-ONLY]secret content[/INTERNAL-SENIOR-ONLY]"></textarea>

    <label>User message &mdash; try <code class="inline">[[DOCUMENT:comp_plan_2026]]</code> or <code class="inline">[[DOCUMENT:onboarding_guide]]</code></label>
    <textarea id="userMessage" placeholder="Type a message..."></textarea>

    <label>Requested tools</label>
    <div class="tools">
      <label><input type="checkbox" value="web_search" checked> web_search</label>
      <label><input type="checkbox" value="calculator"> calculator</label>
      <label><input type="checkbox" value="docs.read_internal"> docs.read_internal</label>
      <label><input type="checkbox" value="db.readonly_query"> db.readonly_query</label>
      <label><input type="checkbox" value="db.write_query"> db.write_query</label>
      <label><input type="checkbox" value="code_interpreter"> code_interpreter</label>
      <label><input type="checkbox" value="internal.readonly.customer_lookup"> internal.readonly.customer_lookup</label>
    </div>

    <label>Preset examples</label>
    <div class="presets" id="presets"></div>

    <button class="run" onclick="runPipeline()">Run through the gateway &rarr;</button>
  </div>

  <div class="main">
    <div class="summary" id="summary">
      <h3>Summary</h3>
      <div class="empty">Run a request to see what got stripped, masked, or resolved.</div>
    </div>
    <div class="columns">
      <div class="panel"><h3>1. Raw payload (as sent by the client)</h3><pre id="rawOut">&mdash;</pre></div>
      <div class="panel"><h3>2. After policy + document resolution</h3><pre id="policyOut">&mdash;</pre></div>
      <div class="panel"><h3>3. Live view &mdash; what the AI agent actually receives</h3><pre id="liveOut">&mdash;</pre></div>
    </div>

    <div class="panel" style="margin-top:14px; border-color: rgba(255,107,107,0.4);">
      <h3 style="color: var(--danger);">4. Persisted view &mdash; the ONLY thing allowed into the cache and Langfuse</h3>
      <p class="empty" style="margin-bottom:8px;">Always fully masked (ignores every exemption), broader entity set, and any resolved document content is stripped to a name-only placeholder. Compare this against panel 3 above for an exempted or document-referencing request -- they should differ.</p>
      <pre id="persistedOut">&mdash;</pre>
    </div>

    <div class="columns2">
      <div class="panel" style="border-color: rgba(255,107,107,0.4);">
        <h3 style="color: var(--danger);">SIMULATED &mdash; response WITHOUT this gateway</h3>
        <p class="empty" style="margin-bottom:8px;">Not a real model call. Shows what an AI plausibly WOULD say if it received the raw, ungoverned payload directly.</p>
        <pre id="responseWithout">&mdash;</pre>
      </div>
      <div class="panel" style="border-color: rgba(61,220,151,0.4);">
        <h3 style="color: var(--ok);">SIMULATED &mdash; response WITH this gateway</h3>
        <p class="empty" style="margin-bottom:8px;">Not a real model call. Shows what an AI plausibly WOULD say given only the live view above.</p>
        <pre id="responseWith">&mdash;</pre>
      </div>
    </div>

    <div class="panel" style="margin-top:14px;">
      <h3>Langfuse audit log <span id="langfuseStatus" class="badge ok">checking...</span></h3>
      <p class="empty" style="margin-bottom:8px;">This is the exact object passed to AuditLogger.log_call() -- always the persisted view (panel 4), never the live view. If Langfuse isn't configured, this still shows what WOULD be sent.</p>
      <pre id="langfuseOut">&mdash;</pre>
    </div>
  </div>
</div>

<script>
const PRESETS = [
  {
    label: "Credit card in message (junior)",
    clearance: "junior", department: "engineering",
    system: "You are a helpful customer support assistant.",
    user: "Hi, can you process a refund? The customer's card is 4111 1111 1111 1111.",
    tools: ["web_search"]
  },
  {
    label: "Restricted internal system prompt (junior)",
    clearance: "junior", department: "engineering",
    system: "You are a helpful assistant.\n[INTERNAL-SENIOR-ONLY]The Q3 merger valuation is $480M.[/INTERNAL-SENIOR-ONLY]\nAlways be polite.",
    user: "What's our current business outlook?",
    tools: ["web_search"]
  },
  {
    label: "Same prompt, but senior clearance (contrast)",
    clearance: "senior", department: "engineering",
    system: "You are a helpful assistant.\n[INTERNAL-SENIOR-ONLY]The Q3 merger valuation is $480M.[/INTERNAL-SENIOR-ONLY]\nAlways be polite.",
    user: "What's our current business outlook?",
    tools: ["web_search"]
  },
  {
    label: "Restricted tool request (junior)",
    clearance: "junior", department: "engineering",
    system: "You are a helpful internal engineering assistant.",
    user: "Can you update the pricing table in the database?",
    tools: ["web_search", "db.write_query", "code_interpreter"]
  },
  {
    label: "Internal IP + SWIFT code leak",
    clearance: "mid", department: "engineering",
    system: "You are a helpful assistant.",
    user: "Our db server is at 10.2.44.17 and wire funds via SWIFT code DEUTDEFF500.",
    tools: ["web_search"]
  },
  {
    label: "HR senior: authorized card lookup (exemption)",
    clearance: "senior", department: "hr",
    system: "You are an internal HR assistant.",
    user: "Is worker abb23456789's current card number this: 4111 1111 1111 1111?",
    tools: ["web_search"]
  },
  {
    label: "Same question, engineering senior (no exemption -> masked)",
    clearance: "senior", department: "engineering",
    system: "You are an internal HR assistant.",
    user: "Is worker abb23456789's current card number this: 4111 1111 1111 1111?",
    tools: ["web_search"]
  },
  {
    label: "Salary mentioned live, masked only in persisted view",
    clearance: "mid", department: "engineering",
    system: "You are a helpful HR assistant.",
    user: "Something to flag: my salary is $55,555 per month and I think there's a payroll error.",
    tools: ["web_search"]
  },
  {
    label: "Self-service card update, junior (masked + no write tool)",
    clearance: "junior", department: "engineering",
    system: "You are a helpful internal assistant.",
    user: "Hi, can you update my data and write down my new credit card? The new card number is 4111 1111 1111 1111.",
    tools: ["web_search", "db.write_query"]
  },
  {
    label: "Document access: HR senior (authorized)",
    clearance: "senior", department: "hr",
    system: "You are an internal HR assistant.",
    user: "Please review [[DOCUMENT:comp_plan_2026]] and summarize the compensation ranges.",
    tools: ["web_search"]
  },
  {
    label: "Document access: engineering senior (denied)",
    clearance: "senior", department: "engineering",
    system: "You are an internal HR assistant.",
    user: "Please review [[DOCUMENT:comp_plan_2026]] and summarize the compensation ranges.",
    tools: ["web_search"]
  },
  {
    label: "Document access: unrestricted doc, any employee",
    clearance: "junior", department: "engineering",
    system: "You are a helpful onboarding assistant.",
    user: "What does [[DOCUMENT:onboarding_guide]] say I should do on day 1?",
    tools: ["web_search"]
  }
];

function renderPresets() {
  const container = document.getElementById("presets");
  PRESETS.forEach((preset, i) => {
    const btn = document.createElement("button");
    btn.className = "preset";
    btn.textContent = preset.label;
    btn.onclick = () => loadPreset(i);
    container.appendChild(btn);
  });
}

function loadPreset(i) {
  const p = PRESETS[i];
  document.getElementById("clearance").value = p.clearance;
  document.getElementById("department").value = p.department || "engineering";
  document.getElementById("systemPrompt").value = p.system;
  document.getElementById("userMessage").value = p.user;
  document.querySelectorAll(".tools input[type=checkbox]").forEach(cb => {
    cb.checked = p.tools.includes(cb.value);
  });
}

async function runPipeline() {
  const clearance = document.getElementById("clearance").value;
  const department = document.getElementById("department").value;
  const systemPrompt = document.getElementById("systemPrompt").value;
  const userMessage = document.getElementById("userMessage").value;
  const tools = Array.from(document.querySelectorAll(".tools input[type=checkbox]:checked")).map(cb => cb.value);

  const res = await fetch("/api/run", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      clearance_level: clearance, department: department,
      system_prompt: systemPrompt, user_message: userMessage, tools: tools
    })
  });
  const data = await res.json();

  if (data.error) { alert(data.error); return; }

  document.getElementById("rawOut").textContent = JSON.stringify(data.raw_payload, null, 2);
  document.getElementById("policyOut").textContent = JSON.stringify(data.after_policy, null, 2);
  document.getElementById("liveOut").textContent = JSON.stringify(data.live_view, null, 2);
  document.getElementById("persistedOut").textContent = JSON.stringify(data.persisted_view, null, 2);

  document.getElementById("responseWithout").textContent = data.simulated_response_without_gateway;
  document.getElementById("responseWith").textContent = data.simulated_response_with_gateway;

  const lfBadge = document.getElementById("langfuseStatus");
  if (data.langfuse.enabled) {
    lfBadge.textContent = "ENABLED -- really sent";
    lfBadge.className = "badge ok";
  } else {
    lfBadge.textContent = "not configured -- showing what would be sent";
    lfBadge.className = "badge stripped";
  }
  document.getElementById("langfuseOut").textContent = JSON.stringify(data.langfuse.payload_sent, null, 2);

  renderSummary(data.summary);
}

function renderSummary(summary) {
  const el = document.getElementById("summary");
  let html = `<h3>Summary &mdash; clearance: ${summary.clearance_level}, department: ${summary.department}</h3>`;

  const maskedFindings = summary.masking_findings.filter(f => f.masked);
  const disclosedFindings = summary.masking_findings.filter(f => !f.masked);

  let anything = false;
  if (summary.stripped_tools.length > 0) {
    anything = true;
    html += `<div>Tools stripped: ` + summary.stripped_tools.map(t => `<span class="badge stripped">${t}</span>`).join("") + `</div>`;
  }
  if (summary.system_prompt_redacted) {
    anything = true;
    html += `<div style="margin-top:6px;"><span class="badge stripped">System prompt content redacted</span></div>`;
  }
  if (maskedFindings.length > 0) {
    anything = true;
    html += `<div style="margin-top:6px;">Entities masked: ` +
      maskedFindings.map(f => `<span class="badge masked">${f.entity_type} &rarr; ${f.placeholder} (score ${f.score})</span>`).join("") + `</div>`;
  }
  if (disclosedFindings.length > 0) {
    anything = true;
    html += `<div style="margin-top:6px;">Authorized disclosures (detected, NOT masked live, still logged as masked in persisted view): ` +
      disclosedFindings.map(f => `<span class="badge ok">${f.entity_type} (score ${f.score})</span>`).join("") + `</div>`;
  }
  if (summary.has_document_reference) {
    anything = true;
    if (summary.documents_resolved.length > 0) {
      html += `<div style="margin-top:6px;">Document(s) resolved (visible live, name-only in persisted view): ` +
        summary.documents_resolved.map(n => `<span class="badge ok">${n}</span>`).join("") + `</div>`;
    } else {
      html += `<div style="margin-top:6px;"><span class="badge stripped">Document reference present but access denied -- never fetched</span></div>`;
    }
  }
  if (!anything) {
    html += `<span class="badge ok">Nothing stripped, masked, or resolved &mdash; payload passed through unmodified</span>`;
  }
  if (summary.masking_exempt_entities.length > 0) {
    html += `<div style="margin-top:6px;" class="empty">This clearance+department is exempt from live masking: ${summary.masking_exempt_entities.join(", ")}</div>`;
  }
  html += `<div style="margin-top:6px;" class="empty">Cacheable this turn: ${summary.cacheable ? "yes" : "no (tools and/or document references disable caching)"}</div>`;
  el.innerHTML = html;
}

renderPresets();
</script>
</body>
</html>
"""