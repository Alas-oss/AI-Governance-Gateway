from __future__ import annotations


def _auth_headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _ask(client, token: str, question: str, extra_headers: dict | None = None):
    headers = _auth_headers(token)
    if extra_headers:
        headers.update(extra_headers)
    return client.post(
        "/v1/chat/completions",
        json={"messages": [{"role": "user", "content": question}]},
        headers=headers,
    )


def test_response_carries_delegation_trace_headers(client, upstream, mint_token):
    upstream.set_response_content("ok")
    token = mint_token(user_id="trace-test-1", department="engineering", clearance="junior")

    resp = _ask(client, token, "a fresh root request, no trace headers sent")

    assert resp.status_code == 200
    assert "X-Delegation-Trace-Id" in resp.headers
    assert "X-Delegation-Span-Id" in resp.headers
    assert resp.headers["X-Delegation-Trace-Id"]
    assert resp.headers["X-Delegation-Span-Id"]


def test_continuing_a_trace_id_keeps_it_stable_across_steps(client, upstream, mint_token):
    upstream.set_response_content("ok")
    token = mint_token(user_id="trace-test-2", department="engineering", clearance="junior")

    supplied_trace_id = "test-trace-continuity-12345"
    resp = _ask(
        client,
        token,
        "a step that continues an existing chain",
        extra_headers={
            "X-Delegation-Trace-Id": supplied_trace_id,
            "X-Delegation-Parent-Span-Id": "some-parent-span",
        },
    )

    assert resp.status_code == 200
    assert resp.headers["X-Delegation-Trace-Id"] == supplied_trace_id
    assert resp.headers["X-Delegation-Span-Id"] != "some-parent-span" 


def test_two_independent_requests_get_different_trace_ids(client, upstream, mint_token):
    upstream.set_response_content("ok")
    token = mint_token(user_id="trace-test-3", department="engineering", clearance="junior")

    resp_a = _ask(client, token, "first independent request")
    resp_b = _ask(client, token, "second independent request")

    assert resp_a.headers["X-Delegation-Trace-Id"] != resp_b.headers["X-Delegation-Trace-Id"]


def test_delegation_depth_header_ceiling_is_enforced(client, upstream, mint_token):
    upstream.set_response_content("should never be reached")
    token = mint_token(user_id="trace-test-4", department="engineering", clearance="junior")

    resp = _ask(
        client,
        token,
        "a request claiming to already be very deep in a delegation chain",
        extra_headers={"X-Delegation-Depth": "999"},
    )

    assert resp.status_code == 403


def test_delegation_depth_within_limit_is_allowed(client, upstream, mint_token):
    upstream.set_response_content("ok")
    token = mint_token(user_id="trace-test-5", department="engineering", clearance="junior")

    resp = _ask(
        client,
        token,
        "a request at a shallow, permitted delegation depth",
        extra_headers={"X-Delegation-Depth": "1"},
    )

    assert resp.status_code == 200


def test_malformed_depth_header_does_not_crash_the_request(client, upstream, mint_token):
    upstream.set_response_content("ok")
    token = mint_token(user_id="trace-test-6", department="engineering", clearance="junior")

    resp = _ask(
        client,
        token,
        "a request with a garbage depth header",
        extra_headers={"X-Delegation-Depth": "not-a-number"},
    )

    assert resp.status_code == 200


def test_chain_rate_limit_blocks_rapid_requests_on_the_same_trace(client, upstream, mint_token, monkeypatch):
    import app.main as main_module
    from app.rate_limit.chain_limiter import ChainRateLimiter
    from app.rate_limit.limiter import SlidingWindowRateLimiter

    monkeypatch.setattr(
        main_module,
        "chain_rate_limiter",
        ChainRateLimiter(
            SlidingWindowRateLimiter(main_module.redis_manager.client, window_seconds=60, max_requests=2)
        ),
    )

    upstream.set_response_content("ok")
    token = mint_token(user_id="trace-test-7", department="engineering", clearance="junior")
    shared_trace_id = "test-chain-limit-trace"

    r1 = _ask(client, token, "step one", extra_headers={"X-Delegation-Trace-Id": shared_trace_id})
    r2 = _ask(client, token, "step two", extra_headers={"X-Delegation-Trace-Id": shared_trace_id})
    r3 = _ask(client, token, "step three", extra_headers={"X-Delegation-Trace-Id": shared_trace_id})

    assert r1.status_code == 200
    assert r2.status_code == 200
    assert r3.status_code == 429
