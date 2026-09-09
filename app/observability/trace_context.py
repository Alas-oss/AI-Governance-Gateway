from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Optional

def _generate_id() -> str:
    return uuid.uuid4().hex

@dataclass(frozen=True)
class TraceContext:
    trace_id: str
    span_id: str
    parent_span_id: Optional[str] = None
    depth: int = 0

    def child_span(self) -> "TraceContext":
        return TraceContext(
            trace_id=self.trace_id,
            span_id=_generate_id(),
            parent_span_id=self.span_id,
            depth=self.depth + 1,
        )

def new_trace() -> TraceContext:
    return TraceContext(trace_id=_generate_id(), span_id=_generate_id(), parent_span_id=None, depth=0)

def continue_trace(*, trace_id: Optional[str], parent_span_id: Optional[str] = None, 
    depth: int = 0) -> TraceContext:
    if not trace_id:
        return new_trace()
    return TraceContext(trace_id=trace_id, span_id=_generate_id(), parent_span_id=parent_span_id, depth=depth)