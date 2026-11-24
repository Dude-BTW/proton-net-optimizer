from __future__ import annotations

from pno.errors import PnoError


def test_describe_returns_the_plain_message_without_detail():
    assert PnoError("protonvpn connect failed").describe() == (
        "protonvpn connect failed"
    )


def test_describe_flattens_detail():
    error = PnoError("protonvpn connect failed", detail="line one\nline two")
    assert error.describe() == "protonvpn connect failed: line one line two"


def test_describe_truncates_long_detail():
    error = PnoError("protonvpn connect failed", detail="x" * 400)
    text = error.describe(limit=40)
    assert text.startswith("protonvpn connect failed: ")
    assert text.endswith("…")
    assert text.count("x") == 39
