from __future__ import annotations

from app.observability.trace_context import TraceContext, continue_trace, new_trace


def test_new_trace_has_no_parent_and_depth_zero():
    ctx = new_trace()
    assert ctx.parent_span_id is None
    assert ctx.depth == 0
    assert ctx.trace_id
    assert ctx.span_id


def test_two_new_traces_are_independent():
    a = new_trace()
    b = new_trace()
    assert a.trace_id != b.trace_id
    assert a.span_id != b.span_id


def test_child_span_keeps_trace_id_and_links_parent():
    root = new_trace()
    child = root.child_span()

    assert child.trace_id == root.trace_id
    assert child.parent_span_id == root.span_id
    assert child.span_id != root.span_id
    assert child.depth == root.depth + 1


def test_child_span_chain_accumulates_depth():
    root = new_trace()
    step1 = root.child_span()
    step2 = step1.child_span()
    step3 = step2.child_span()

    assert [root.depth, step1.depth, step2.depth, step3.depth] == [0, 1, 2, 3]
    assert step3.trace_id == root.trace_id
    assert step3.parent_span_id == step2.span_id


def test_continue_trace_with_explicit_trace_id():
    ctx = continue_trace(trace_id="external-trace-abc", parent_span_id="external-span-1", depth=3)

    assert ctx.trace_id == "external-trace-abc"
    assert ctx.parent_span_id == "external-span-1"
    assert ctx.depth == 3
    assert ctx.span_id 


def test_continue_trace_without_trace_id_starts_fresh():
    ctx = continue_trace(trace_id=None)

    assert ctx.parent_span_id is None
    assert ctx.depth == 0
    assert ctx.trace_id


def test_continue_trace_without_trace_id_ignores_parent_span_id():
    ctx = continue_trace(trace_id=None, parent_span_id="orphaned-parent")
    assert ctx.parent_span_id is None


def test_trace_context_is_frozen():
    ctx = new_trace()
    try:
        ctx.depth = 99
        assert False, "TraceContext should be immutable"
    except Exception:
        pass


def test_generated_ids_look_like_uuid_hex():
    ctx = new_trace()

    assert len(ctx.trace_id) == 32
    assert all(c in "0123456789abcdef" for c in ctx.trace_id)
