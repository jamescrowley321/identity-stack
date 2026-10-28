"""The one place the backend obtains a logger.

Every module in ``app/`` gets its logger with ``logger = logbook(__name__)`` at
module level (docs/conventions.md §7–§9); nothing else calls
``logging.getLogger``, which Ruff's ``banned-api`` rule (TID251) enforces.
Handlers, formatters and levels are configured once, by
``app.logging_config.setup_logging``, never by the module that logs.

The record is structured by the formatter, so the message carries the facts:

* Pass values as ``%``-style arguments — ``logger.info("synced %s", user_id)`` —
  never pre-formatted with an f-string (Ruff ``G004``). The record carries the
  template and its arguments separately until a handler formats it, so nothing
  is formatted for a record that is filtered out.
* Log identifiers (user id, tenant id, provider name, HTTP status), never
  credentials: no bearer tokens, management keys, webhook or flow secrets, and
  no raw ``Authorization`` header. Upstream response bodies are truncated
  (``[:500]``) before they are logged.
"""

from __future__ import annotations

import logging


def logbook(name: str) -> logging.Logger:
    """Return the logger for module ``name``; call it as ``logbook(__name__)``.

    Returns the standard-library logger itself, so ``caplog``, ``patch(...logger)``
    and the OTel logging instrumentor behave exactly as they do for
    ``logging.getLogger``.
    """
    return logging.getLogger(name)
