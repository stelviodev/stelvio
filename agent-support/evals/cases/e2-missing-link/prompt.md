# e2 — Missing link

The app already has a `users` DynamoDB table and a `GET /users/{id}` handler that
reads via `Resources.users`. The endpoint still cannot access the table.

Fix the access problem with the smallest correct change.
