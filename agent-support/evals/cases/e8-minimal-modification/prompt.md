# e8 — Publish user_created after create

The app already creates users through `POST /users` into a linked DynamoDB table.

After a successful create, also publish a `user_created` event. Keep the existing
API, route, table, and handler entrypoint.
