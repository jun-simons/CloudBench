import logging

from cloudbench.gateway.__main__ import RedactTokens


def test_access_tokens_are_redacted_from_access_logs() -> None:
    record = logging.LogRecord(
        "uvicorn.access",
        logging.INFO,
        __file__,
        1,
        '%s - "%s %s HTTP/%s" %d',
        ("1.2.3.4", "GET", "/v1/events?access_token=cbt_secret&x=1", "1.1", 200),
        None,
    )
    RedactTokens().filter(record)
    assert "cbt_secret" not in record.getMessage()
    assert "access_token=[redacted]&x=1" in record.getMessage()
