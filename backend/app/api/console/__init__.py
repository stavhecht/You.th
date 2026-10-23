"""Editorial console endpoints. JWT required on every route.

One module per job: auth, review, media, pipeline, contact, discovery and
research. Auth is applied at the router level
(``dependencies=[Depends(require_reviewer)]``) rather than per-handler. A
per-handler decorator is one forgotten line away from an unauthenticated
console endpoint; a router-level dependency cannot be forgotten by adding a
route, and ``test_console_auth_coverage.py`` catches a router that forgot it.

Two deliberate omissions from this surface:

* **No DELETE on articles.** Rejection is a state, and the audit trail is the
  product.
* **No endpoint that sets status directly.** ``approve`` and ``reject`` are the
  only transitions, and ``approve`` is the only path to ``published`` anywhere
  in the system.
"""
