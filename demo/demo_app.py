"""
AI Governance Gateway -- Live Demo

A small, standalone app for DEMOING what the gateway actually does to a
payload, using the *real* production code from app/policy/enforcement.py
and app/guardrails/ -- not a reimplementation or a mockup. Pick a
clearance level, write (or load a preset) message/tools/system-prompt,
and see exactly what gets stripped or masked before anything would reach
a real AI agent.

This intentionally skips real JWT authentication (Layer 1) and Redis/
Qdrant (Layers 4-5) -- it exists purely to make Layer 2 (policy
enforcement) and Layer 3 (data masking) visible and explorable. The
transformation functions it calls are byte-for-byte the same functions
main.py calls on real traffic.

Run with:
    uvicorn demo.demo_app:app --reload --port 8090
Then open http://localhost:8090 in a browser.
"""
from __future__ import annotations

import copy
import re
from typing import Any, Dict, List, Optional

from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from app.auth.jwt_utils import ClearanceLevel, UserContext
from app.config import get_settings
from app.guardrails.engine import get_engine, init_guardrails_engine
from app.observability.langfuse_logger import AuditLogger
from app.policy.enforcement import enforce_policy_on_payload, get_masking_exempt_entities

app = FastAPI(title="AI Governance Gateway -- Live Demo")

settings = get_settings()
audit_logger = AuditLogger(settings)


@app.on_event("startup")
async def startup() -> None:
    init_guardrails_engine(settings)


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


def _simulate_agent_response(payload: Dict[str, Any]) -> str:
    messages = payload.get("messages", [])
    tool_names = {_tool_name(t) for t in payload.get("tools", [])}
    user_messages = [m for m in messages if m.get("role") == "user"]
    last_user_text = user_messages[-1]["content"] if user_messages else ""

    masked_ref = _MASKED_REF_PATTERN.search(last_user_text)
    card_match = _CARD_PATTERN.search(last_user_text)
    wants_write = any(kw in last_user_text.lower() for kw in ("update", "write", "change", "modify", "set"))
    system_text = " ".join(m.get("content", "") for m in messages if m.get("role") == "system")

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

    exempt_entities = get_masking_exempt_entities(user, settings)
    after_masking_messages, findings = _mask_messages_with_findings(
        after_policy.get("messages", []), exempt_entities=exempt_entities
    )
    after_masking = {**after_policy, "messages": after_masking_messages}

    simulated_without_gateway = _simulate_agent_response(raw_payload)
    simulated_with_gateway = _simulate_agent_response(after_masking)

    response_for_logging = {"choices": [{"message": {"role": "assistant", "content": simulated_with_gateway}}]}
    try:
        audit_logger.log_call(
            user_id=user.user_id,
            department=user.department,
            masked_request=after_masking,
            masked_response=response_for_logging,
            prompt_tokens=0,
            completion_tokens=0,
            cache_hit=False,
        )
    except Exception:  # noqa: BLE001 - demo-only: never let logging break the demo response
        pass

    return {
        "raw_payload": raw_payload,
        "after_policy": after_policy,
        "after_masking": after_masking,
        "simulated_response_without_gateway": simulated_without_gateway,
        "simulated_response_with_gateway": simulated_with_gateway,
        "langfuse": {
            "enabled": audit_logger.enabled,
            "payload_sent": {"masked_request": after_masking, "masked_response": response_for_logging},
        },
        "summary": {
            "clearance_level": clearance.value,
            "department": user.department,
            "stripped_tools": stripped_tools,
            "system_prompt_redacted": system_prompt_redacted,
            "masking_exempt_entities": exempt_entities,
            "masking_findings": findings,
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
  .layout { display: grid; grid-template-columns: 340px 1fr; gap: 0; min-height: calc(100vh - 78px); }
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
  .placeholder-hit { color: var(--accent); font-weight: 600; }
</style>
</head>
<body>
<header>
  <h1>AI Governance Gateway &mdash; Live Demo</h1>
  <p>Runs the real Layer 2 (policy enforcement) and Layer 3 (data masking) code against whatever you type below.
     The response panels below are clearly-labeled <strong>simulations</strong> (there's no live AI agent connected) --
     everything above them (payload transformations) runs the actual production code.</p>
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

    <label>User message</label>
    <textarea id="userMessage" placeholder="Type a message, e.g. containing a card number or internal IP..."></textarea>

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
      <div class="empty">Run a request to see what got stripped or masked.</div>
    </div>
    <div class="columns">
      <div class="panel"><h3>1. Raw payload (as sent by the client)</h3><pre id="rawOut">&mdash;</pre></div>
      <div class="panel"><h3>2. After policy enforcement (Layer 2)</h3><pre id="policyOut">&mdash;</pre></div>
      <div class="panel"><h3>3. After data masking (Layer 3) &mdash; this is what reaches the AI agent</h3><pre id="maskOut">&mdash;</pre></div>
    </div>

    <div class="columns" style="grid-template-columns: 1fr 1fr;">
      <div class="panel" style="border-color: rgba(255,107,107,0.4);">
        <h3 style="color: var(--danger);">SIMULATED &mdash; response WITHOUT this gateway</h3>
        <p class="empty" style="margin-bottom:8px;">Not a real model call. Shows what an AI plausibly WOULD say if it received the raw, ungoverned payload directly.</p>
        <pre id="responseWithout">&mdash;</pre>
      </div>
      <div class="panel" style="border-color: rgba(61,220,151,0.4);">
        <h3 style="color: var(--ok);">SIMULATED &mdash; response WITH this gateway</h3>
        <p class="empty" style="margin-bottom:8px;">Not a real model call. Shows what an AI plausibly WOULD say given only the policy-filtered, masked payload above.</p>
        <pre id="responseWith">&mdash;</pre>
      </div>
    </div>

    <div class="panel" style="margin-top:14px;">
      <h3>Langfuse audit log <span id="langfuseStatus" class="badge ok">checking...</span></h3>
      <p class="empty" style="margin-bottom:8px;">This is the exact object passed to AuditLogger.log_call() -- only ever the already-masked request/response, per Layer 3's ordering guarantee. If Langfuse isn't configured (GATEWAY_LANGFUSE_ENABLED), this still shows what WOULD be sent, without needing a live account.</p>
      <pre id="langfuseOut">&mdash;</pre>
    </div>
  </div>
</div>

<script>
const PRESETS = [
  {
    label: "Credit card in message (junior)",
    clearance: "junior",
    system: "You are a helpful customer support assistant.",
    user: "Hi, can you process a refund? The customer's card is 4111 1111 1111 1111.",
    tools: ["web_search"]
  },
  {
    label: "Restricted internal system prompt (junior)",
    clearance: "junior",
    system: "You are a helpful assistant.\n[INTERNAL-SENIOR-ONLY]The Q3 merger valuation is $480M.[/INTERNAL-SENIOR-ONLY]\nAlways be polite.",
    user: "What's our current business outlook?",
    tools: ["web_search"]
  },
  {
    label: "Same prompt, but senior clearance (contrast)",
    clearance: "senior",
    system: "You are a helpful assistant.\n[INTERNAL-SENIOR-ONLY]The Q3 merger valuation is $480M.[/INTERNAL-SENIOR-ONLY]\nAlways be polite.",
    user: "What's our current business outlook?",
    tools: ["web_search"]
  },
  {
    label: "Restricted tool request (junior)",
    clearance: "junior",
    system: "You are a helpful internal engineering assistant.",
    user: "Can you update the pricing table in the database?",
    tools: ["web_search", "db.write_query", "code_interpreter"]
  },
  {
    label: "Internal IP + SWIFT code leak",
    clearance: "mid",
    department: "engineering",
    system: "You are a helpful assistant.",
    user: "Our db server is at 10.2.44.17 and wire funds via SWIFT code DEUTDEFF500.",
    tools: ["web_search"]
  },
  {
    label: "HR senior: authorized card lookup (exemption)",
    clearance: "senior",
    department: "hr",
    system: "You are an internal HR assistant.",
    user: "Is worker abb23456789's current card number this: 4111 1111 1111 1111?",
    tools: ["web_search"]
  },
  {
    label: "Same question, engineering senior (no exemption -> masked)",
    clearance: "senior",
    department: "engineering",
    system: "You are an internal HR assistant.",
    user: "Is worker abb23456789's current card number this: 4111 1111 1111 1111?",
    tools: ["web_search"]
  },
  {
    label: "HR senior sees peer comp tag redacted anyway (dept override)",
    clearance: "senior",
    department: "hr",
    system: "You are helpful.\n[HR-PEER-COMPENSATION]The HR director's salary is $310,000.[/HR-PEER-COMPENSATION]\nBe polite.",
    user: "What's our team's compensation structure?",
    tools: ["web_search"]
  },
  {
    label: "Self-service card update, junior (masked + no write tool)",
    clearance: "junior",
    department: "engineering",
    system: "You are a helpful internal assistant.",
    user: "Hi, can you update my data and write down my new credit card? The new card number is 4111 1111 1111 1111.",
    tools: ["web_search", "db.write_query"]
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
      clearance_level: clearance,
      department: department,
      system_prompt: systemPrompt,
      user_message: userMessage,
      tools: tools
    })
  });
  const data = await res.json();

  if (data.error) {
    alert(data.error);
    return;
  }

  document.getElementById("rawOut").textContent = JSON.stringify(data.raw_payload, null, 2);
  document.getElementById("policyOut").textContent = JSON.stringify(data.after_policy, null, 2);
  document.getElementById("maskOut").textContent = JSON.stringify(data.after_masking, null, 2);

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

  if (summary.stripped_tools.length === 0 && !summary.system_prompt_redacted && summary.masking_findings.length === 0) {
    html += `<span class="badge ok">Nothing stripped or masked &mdash; payload passed through unmodified</span>`;
  } else {
    if (summary.stripped_tools.length > 0) {
      html += `<div>Tools stripped: ` + summary.stripped_tools.map(t => `<span class="badge stripped">${t}</span>`).join("") + `</div>`;
    }
    if (summary.system_prompt_redacted) {
      html += `<div style="margin-top:6px;"><span class="badge stripped">System prompt content redacted</span></div>`;
    }
    if (maskedFindings.length > 0) {
      html += `<div style="margin-top:6px;">Entities masked: ` +
        maskedFindings.map(f => `<span class="badge masked">${f.entity_type} &rarr; ${f.placeholder} (score ${f.score})</span>`).join("") +
        `</div>`;
    }
    if (disclosedFindings.length > 0) {
      html += `<div style="margin-top:6px;">Authorized disclosures (detected, NOT masked, still logged): ` +
        disclosedFindings.map(f => `<span class="badge ok">${f.entity_type} (score ${f.score})</span>`).join("") +
        `</div>`;
    }
  }
  if (summary.masking_exempt_entities.length > 0) {
    html += `<div style="margin-top:6px;" class="empty">This clearance+department is exempt from masking: ${summary.masking_exempt_entities.join(", ")}</div>`;
  }
  el.innerHTML = html;
}

renderPresets();
</script>
</body>
</html>
"""