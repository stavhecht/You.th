"""Process-wide logging for the API and the worker, with secrets kept out.

httpx logs every request URL at INFO, and NCBI takes its API key as a query
parameter, so every PubMed call wrote the key into the log in plain text. The
request lines are worth keeping — they are how a throttled or failing provider
is diagnosed — so the key is masked rather than the logger silenced.

The filter sits on the root handlers, not on a logger: a logger's filters do not
see records propagated up from its children, a handler's see everything it
writes.
"""

from __future__ import annotations

import logging
import re
from contextvars import ContextVar

#: The id of the HTTP request being served, set by the middleware in
#: ``main.py``. "-" outside a request: the worker, a script, startup.
request_id: ContextVar[str] = ContextVar("request_id", default="-")

FORMAT = "%(asctime)s %(levelname)-8s [%(request_id)s] %(name)s: %(message)s"

#: A credential in a query string. Named parameters only, so an ordinary
#: ``key=`` in prose or a DOI is left alone.
_SECRET_PARAM_RE = re.compile(
    r"\b(api_key|apikey|access_token|token)=[^&\s\"']+", re.IGNORECASE
)


class RedactSecrets(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        # Here because this filter already sits on every handler; the format
        # string needs the attribute on every record.
        record.request_id = request_id.get()
        message = record.getMessage()
        redacted = _SECRET_PARAM_RE.sub(r"\1=<redacted>", message)
        if redacted != message:
            # Replace the formatted message outright: the key usually sits in
            # ``args`` (httpx passes the URL object), not in ``msg``.
            record.msg, record.args = redacted, None
        return True


def configure_logging(level: int) -> None:
    logging.basicConfig(level=level, format=FORMAT)
    for handler in logging.getLogger().handlers:
        if not any(isinstance(f, RedactSecrets) for f in handler.filters):
            handler.addFilter(RedactSecrets())
