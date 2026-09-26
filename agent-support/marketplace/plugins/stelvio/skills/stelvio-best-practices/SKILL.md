---
name: stelvio-best-practices
description: >-
  Stelvio idioms for building AWS apps with components, links, Resources,
  subscribe/notify_*, and customize. Use when writing or modifying stlv_app.py,
  Lambda handlers, DynamoDB/S3/SQS/SNS wiring, or when the user asks for Stelvio
  best practices.
disable-model-invocation: true
---

# Stelvio best practices

Build applications **with** Stelvio components and idioms. Prefer the smallest
change that satisfies the task. Do not invent a second infrastructure style
beside Stelvio when a component already covers the need.

`stlv_resources.py` is generated on `stlv diff` / `stlv deploy`. Do not require
it to exist on disk before you link; still write handlers against `Resources`.

---

## 1. Link instead of manual IAM or env

When a Lambda or API route must use another resource, pass it in `links=[...]`
on the route or function. Linking grants IAM and injects `STLV_*` properties.

```python
users = DynamoTable("users", fields={"id": "string"}, partition_key="id")
api = HttpApi("users-api")
api.route("GET", "/users/{id}", "functions/users.get", links=[users])
```

Do **not** hand-write IAM policies, invent `environment={...}` with resource
names, or hard-code generated AWS names to “fix” access. If the handler
already uses `Resources` but access fails, the fix is almost always the missing
link—not a handler rewrite.

---

## 2. Read linked names from `Resources`

```python
from stlv_resources import Resources
import boto3

table = boto3.resource("dynamodb").Table(Resources.users.table_name)
```

Hyphenated component names become snake_case attributes
(`Topic("user-created")` → `Resources.user_created`). Prefer
`Resources.<name>.<property>` over string literals for table names, queue URLs,
topic ARNs, and similar linked properties.

---

## 3. Decouple slow work with `Queue` + `subscribe`

When an HTTP accept path must return immediately and work must retry:

1. Create a `Queue`.
2. Link it on the accept handler; enqueue from that handler.
3. Consume with `queue.subscribe("worker", "functions/....handler")`.

Do not keep long work inline in the request path. Prefer Stelvio `Queue` /
`subscribe` over raw SQS wiring.

---

## 4. Object storage events: `notify_function` or `notify_queue`

Direct Lambda on object create:

```python
bucket.notify_function(
    "process-image",
    events=["s3:ObjectCreated:*"],
    filter_prefix="incoming/",
    filter_suffix=".jpg",
    function="functions/process_image.handler",
)
```

Use real kwargs: `filter_prefix` / `filter_suffix` / `function` (not inventing
`prefix=` / `handler=`).

When bursts, temporary failure, or back-pressure matter, buffer through a queue:

```python
bucket.notify_queue("to-queue", events=["s3:ObjectCreated:*"], queue=queue)
queue.subscribe("worker", "functions/worker.handler")
```

Do not use raw `BucketNotification` / Pulumi S3 notification resources when
`notify_*` applies. Prefer `notify_queue` plus a subscribed worker when the
workload needs buffering. A DLQ is optional unless asked.

---

## 5. Independent consumers: one `Topic`, then `subscribe`

When several consumers must each react to the same event and evolve separately,
use one `Topic`, link it on the producer, publish from the handler, and
`topic.subscribe(...)` (or `subscribe_queue`) per consumer.

Do **not** put competing consumers on a single shared queue for fan-out—that
is competing-consumer, not independent fan-out.

---

## 6. Escape hatches: `customize` on the same component

To set provider-level fields (for example KMS on a queue), keep the existing
component and pass `customize`:

```python
Queue(
    "orders",
    customize={"queue": {"kms_master_key_id": "alias/orders"}},
)
```

Do not replace the component with a new name or a raw Pulumi resource when
`customize` can express the change.

---

## 7. Minimal modification

When the app already has an API, route, table, queue, or handler entrypoint,
keep those names. Add the missing link, topic, or `customize` beside them.
Do not rebuild the API, rename handlers, or recreate tables to attach a small
feature (for example publishing `user_created` after create).

---

## Anti-patterns (avoid)

| Avoid | Prefer |
|---|---|
| Manual IAM / manual resource env vars | `links=[...]` |
| Hard-coded table/queue/topic names | `Resources.<n>.…` |
| Raw Pulumi for covered resources | Stelvio component |
| Inline slow work in HTTP | `Queue` + `subscribe` |
| Raw S3 notifications | `notify_function` / `notify_queue` |
| One queue, many competing workers for fan-out | One `Topic` + subscriptions |
| New component for a Pulumi-only field | `customize={...}` on the existing one |
| Rewriting working handlers/routes for a link fix | Smallest wiring change |

---

## Also worth following

- Prefer public Stelvio docs and examples over guessing unfamiliar APIs.
- Drop to raw Pulumi only when no Stelvio component exists for the need.
- Prefer `HttpApi` for new HTTP APIs unless REST-specific behavior is required.
- Iterate on app definitions without deploying unless the task needs live AWS.
