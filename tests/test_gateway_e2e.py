from __future__ import annotations

TEST_CARD_NUMBER = "4111 1111 1111 1111"  # Luhn-valid test Visa number
TEST_SALARY = "$185,000"


def _auth_headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _ask(client, token: str, question: str):
    return client.post(
        "/v1/chat/completions",
        json={"messages": [{"role": "user", "content": question}]},
        headers=_auth_headers(token),
    )


def test_hr_senior_sees_unmasked_card_number_live(client, upstream, mint_token):
    upstream.set_response_content(
        f"On file: card {TEST_CARD_NUMBER}, annual salary {TEST_SALARY}."
    )
    token = mint_token(user_id="hr-senior-1", department="hr", clearance="senior")

    resp = _ask(client, token, "What card and salary do we have on file (query A)?")

    assert resp.status_code == 200, resp.text
    content = resp.json()["choices"][0]["message"]["content"]
    assert TEST_CARD_NUMBER in content, f"card number was masked when it should be exempt: {content!r}"
    assert "MASKED" not in content, f"unexpected masking in live view: {content!r}"


def test_hr_junior_gets_card_number_masked_live(client, upstream, mint_token):
    upstream.set_response_content(f"On file: card {TEST_CARD_NUMBER}.")
    token = mint_token(user_id="hr-junior-1", department="hr", clearance="junior")

    resp = _ask(client, token, "What card do we have on file (query B)?")

    assert resp.status_code == 200, resp.text
    content = resp.json()["choices"][0]["message"]["content"]
    assert TEST_CARD_NUMBER not in content, f"card number leaked to an unauthorized user: {content!r}"
    assert "MASKED" in content


def test_non_hr_senior_gets_card_number_masked_live(client, upstream, mint_token):
    upstream.set_response_content(f"On file: card {TEST_CARD_NUMBER}.")
    token = mint_token(user_id="eng-senior-1", department="eng", clearance="senior")

    resp = _ask(client, token, "What card do we have on file (query C)?")

    assert resp.status_code == 200, resp.text
    content = resp.json()["choices"][0]["message"]["content"]
    assert TEST_CARD_NUMBER not in content
    assert "MASKED" in content


def test_persisted_audit_log_masks_even_the_exempt_card_number(client, upstream, mint_token, monkeypatch):
    import app.main as main_module

    captured = {}

    def _fake_log_call(**kwargs):
        captured.update(kwargs)

    monkeypatch.setattr(main_module.audit_logger, "log_call", _fake_log_call)
    monkeypatch.setattr(main_module, "semantic_cache", None)

    upstream.set_response_content(f"On file: card {TEST_CARD_NUMBER}, salary {TEST_SALARY}.")
    token = mint_token(user_id="hr-senior-2", department="hr", clearance="senior")

    resp = _ask(client, token, "What card and salary do we have on file (query D)?")

    assert resp.status_code == 200, resp.text
    live_content = resp.json()["choices"][0]["message"]["content"]
    assert TEST_CARD_NUMBER in live_content

    persisted_response = captured["masked_response"]
    persisted_content = persisted_response["choices"][0]["message"]["content"]
    assert TEST_CARD_NUMBER not in persisted_content, (
        f"real card number leaked into the persisted/audit view: {persisted_content!r}"
    )
    assert TEST_SALARY not in persisted_content, (
        f"salary figure leaked into the persisted/audit view: {persisted_content!r}"
    )


def test_missing_auth_header_is_rejected(client):
    resp = client.post("/v1/chat/completions", json={"messages": []})
    assert resp.status_code == 401


def test_health_endpoint_is_open(client):
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"
