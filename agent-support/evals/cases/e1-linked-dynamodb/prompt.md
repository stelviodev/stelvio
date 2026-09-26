# e1 — Linked DynamoDB

The app already exposes `GET /users/{id}`.

Add a DynamoDB table named `users` with partition key `id`.
Update the handler so it reads and returns the user item for the path parameter `id`.

Do not remove the existing route.
