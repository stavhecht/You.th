"""Credentials in logged URLs are masked, and nothing else is touched."""

from __future__ import annotations

import logging

from app.logging_setup import RedactSecrets


def _record(msg: str, *args: object) -> logging.LogRecord:
    return logging.LogRecord("httpx", logging.INFO, __file__, 1, msg, args, None)


def test_a_key_in_the_url_args_is_masked() -> None:
    """httpx passes the URL as an argument, not in the message template."""
    record = _record(
        "HTTP Request: %s %s",
        "GET",
        "https://eutils.ncbi.nlm.nih.gov/esearch.fcgi?db=pubmed&api_key=60242d7a&retmode=json",
    )

    RedactSecrets().filter(record)

    message = record.getMessage()
    assert "60242d7a" not in message
    assert "api_key=<redacted>&retmode=json" in message


def test_a_record_without_a_secret_is_left_as_it_was() -> None:
    record = _record("claim %r: %d raw -> %d deduped", "reduces stress", 59, 48)

    RedactSecrets().filter(record)

    assert record.args == ("reduces stress", 59, 48)


def test_an_ordinary_key_word_is_not_a_secret() -> None:
    record = _record("sorted by key=relevance for monkey=1")

    RedactSecrets().filter(record)

    assert record.getMessage() == "sorted by key=relevance for monkey=1"
