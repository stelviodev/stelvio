# State Management

Stelvio stores your infrastructure state in S3, so multiple team members can work on the same application without sharing files or syncing manually.

## Where State Lives

When you first deploy, Stelvio creates an S3 bucket named `stlv-state-{id}` in your AWS account. Each app and environment has separate files:

```
stlv-state-{id}/
├── state/{app}/{env}.json           # Current resource state
├── lock/{app}/{env}.json            # Active lock (who's deploying)
├── update/{app}/{env}/{id}.json     # Operation history & errors
└── snapshot/{app}/{env}/{id}.json   # Saved after successful deploys
```

## Locking

Stelvio locks state during operations that modify it: `deploy`, `refresh`, `destroy`, `state rm`, `state repair`.

If someone else is running one of these, you'll see:

```
✗ State is locked
  Environment 'staging' is locked by 'deploy' since 2025-12-15 10:30:00
```

If a command was interrupted (Ctrl+C, crash, network issue), the lock may remain:

```bash
stlv unlock
stlv unlock staging
```

!!! warning
    Only unlock if you're certain no deployment is actually running.

## Crash Recovery

Stelvio saves state to S3 continuously during operations - not just at the end. If a deployment crashes:

1. Resources that completed are already saved
2. Run `stlv unlock` to release the lock
3. Run `stlv deploy` to continue where you left off

## Renaming

Changing the app name or environment name creates new infrastructure - it doesn't rename existing resources.

**To rename your app:**

1. `stlv destroy` - destroy the old app
2. Change the name in `stlv_app.py`
3. `stlv deploy` - deploy with new name

**To rename an environment:**

1. `stlv destroy staging` - destroy old environment
2. `stlv deploy stage` - deploy with new name

!!! warning
    If you rename without destroying first, you'll have two sets of resources both running in AWS.

## State Commands

See [Using CLI - state](using-cli.md#state) for `stlv state list`, `stlv state rm`, and `stlv state repair`.

## What Else Gets Stored

Stelvio stores encryption passphrases for state secrets in AWS Parameter Store at `/stlv/passphrase/{app}/{env}`, and bootstrap info (bucket name, version) at `/stlv/bootstrap`.

During operations, Stelvio downloads state from S3 to a temporary folder `.stelvio/{id}/` in your project. This is cleaned up automatically when the command completes.
