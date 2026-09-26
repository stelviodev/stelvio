# e3 — Async order processing

`POST /orders` currently does heavy processing inside the request.

Change the design so the accept path returns immediately, and failed processing
can be retried. Keep the existing HTTP route.
