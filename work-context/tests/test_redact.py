"""derive/meetings/redact.py — deterministic PII masking for shareable exports.

All fixtures use OBVIOUSLY-FAKE identifiers (never real customer/account data) —
the test must break if the regex drifts, independently of any real value.
"""

from __future__ import annotations

from derive.meetings import redact


def test_masks_email_phone_and_account():
    text = "ping me at fake.user@example.com or 9876543210, acct 123456789012"
    out, rep = redact.redact_text(text)
    assert "fake.user@example.com" not in out
    assert "9876543210" not in out
    assert "123456789012" not in out
    assert "[email]" in out and "[phone]" in out and "[account]" in out
    assert rep == {"email": 1, "phone": 1, "account": 1}


def test_masks_pan_ifsc_aadhaar_card():
    text = "PAN ABCDE1234F IFSC HDFC0001234 uid 1234 5678 9012 card 4111 1111 1111 1111"
    out, rep = redact.redact_text(text)
    assert "ABCDE1234F" not in out and "[pan]" in out
    assert "HDFC0001234" not in out and "[ifsc]" in out
    assert "1234 5678 9012" not in out and "[aadhaar]" in out
    assert "4111 1111 1111 1111" not in out and "[card]" in out


def test_preserves_shareable_context():
    # Jira keys, transcript offsets, ISO dates, amounts and URLs must survive —
    # masking them would gut a MoM and they carry no personal identifier.
    text = "[12:34] CBS-4521 due 2026-07-21, cost ₹1200, see https://example.com/x"
    out, _ = redact.redact_text(text)
    assert out == text


def test_names_off_by_default_and_on_with_flag(monkeypatch):
    monkeypatch.setattr(redact, "_roster", lambda: ("Alexander Example", "Alexander"))
    text = "Alexander led the review; Alexander Example will follow up."
    off, rep_off = redact.redact_text(text)
    assert off == text and "name" not in rep_off
    on, rep_on = redact.redact_text(text, mask_names=True)
    assert "Alexander" not in on and on.count("[name]") == 2
    assert rep_on["name"] == 2


def test_idempotent_no_double_masking():
    text = "reach fake.user@example.com / 9876543210"
    once, _ = redact.redact_text(text)
    twice, rep2 = redact.redact_text(once)
    assert once == twice and rep2 == {}


def test_summarize_pluralizes():
    assert redact.summarize({"email": 1, "phone": 2}) == "1 email, 2 phones"
    assert redact.summarize({}) == "nothing"
