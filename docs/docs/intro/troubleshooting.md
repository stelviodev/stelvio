# Troubleshooting

This guide helps you debug common issues and understand Stelvio's internal workings when things go wrong.

## Debugging with Verbose Output

When you encounter issues, use the verbose flags to get more detailed information:

```bash
# Show INFO level logs
stlv -v deploy

# Show DEBUG level logs (most detailed)
stlv -vv deploy
```

These logs display information about the locations and values that Stelvio works 
with, as well as the operations it performs.

## Understanding Log Files

Stelvio also writes logs to files to help diagnose issues. Log locations depend on your operating system:

- **macOS:** `~/Library/Logs/stelvio/`
- **Linux:** `~/.local/state/stelvio/logs/`
- **Windows:** `%LOCALAPPDATA%\stelvio\logs\`


## The .stelvio Directory

Each Stelvio project has a `.stelvio/` directory in the project root:

**`.stelvio/userenv`**

- Contains your personal environment name
- Defaults to your computer username
- Can be customized (see [Environments guide](../concepts/environments.md#customizing-your-personal-environment-name))

**`lambda_dependencies/`**

- Cached Lambda and Layer dependencies
- Safe to delete if you suspect corruption - regenerated on next deployment
- Caches no `stlv` run has used for 7 days are removed automatically

**`{timestamp}-{random}/`** (temporary working directory)

- Created when running commands that need state (`diff`, `deploy`, `refresh`, `destroy`, `outputs`, `state` commands)
- Contains `.pulumi/stacks/{app}/{env}.json` - state downloaded from S3
- Automatically deleted when command completes
- For ordinary application operations, leftover working directories can be deleted
  after the operation is stopped and its remote state is safe. VPC access recovery
  state is different: preserve local ownership records and remote access state
  until tunnel cleanup succeeds.

## Renaming Your App or Environment

See [State Management - Renaming](../concepts/state.md#renaming) for how to safely rename your app or environment.

## Common Issues and Solutions

### State Lock Errors

If you kill deploy/destroy operation while running or something crashes 
unexpectedly you might not be able to deploy. 

**Problem:** You get "Stack is currently being updated"

**Solution:**
```bash
stlv unlock
stlv unlock staging
```

Only use this if you're certain no other deployment is actually running.

### AWS Credential Issues

**Problem:** A `stlv` command stops with a short red message like this:

```
Unable to locate credentials
  Profile: default (AWS_PROFILE not set), region: us-east-1
  Check your AWS setup: 'aws configure', 'aws sso login', AWS_PROFILE, or AwsConfig(profile=...) in stlv_app.py.
```

The first line is the AWS error itself. The `Profile:` line tells you which profile
Stelvio used and where it came from: `AwsConfig(profile=...)` in `stlv_app.py`, the
`AWS_PROFILE` or `AWS_DEFAULT_PROFILE` environment variable, or the default profile when
neither is set. An expired SSO session, a missing profile, or a wrong access key shows up
the same way, with the AWS text on the first line.

To see the full Python traceback instead of the short message, run the command with
`STLV_DEBUG=1`.

**Solution:**
Make sure your AWS credentials are set up properly.

You have three options:

1. Environment variable `AWS_PROFILE` is set and profile exists:
   ```bash
   export AWS_PROFILE=YOUR_PROFILE_NAME
   ```

2. Environment variables `AWS_ACCESS_KEY_ID` and `AWS_SECRET_ACCESS_KEY` are set:
   ```bash
   export AWS_ACCESS_KEY_ID="<YOUR_ACCESS_KEY_ID>"
   export AWS_SECRET_ACCESS_KEY="<YOUR_SECRET_ACCESS_KEY>"
   ```
3. Set profile in `stlv_app.py`:
   ```python title="stlv_app.py" hl_lines="4"
   @app.config
   def configuration(env: str) -> StelvioAppConfig:
    return StelvioAppConfig(
        aws=AwsConfig(profile="your-profile"),
    )
   ```
   
#### How to check if AWS profile exists

1. If you have [AWS CLI](https://docs.aws.amazon.com/cli/latest/userguide/cli-chap-welcome.html) 
   installed, run:
   ```bash
   aws configure list-profiles
   ```
2. Alternatively, you can check the AWS configuration files directly. 
   Profiles are stored in `.aws/config` and `.aws/credentials` in your user directory.

    ??? info "Platform-specific user directory paths"
        - **Linux/macOS**: `~/.aws/`
        - **Windows**: `%USERPROFILE%\.aws\`

### Permission Denied Errors

**Problem:** "AccessDeniedException" when Stelvio reads its state, or "Access Denied" during a deploy

**Solutions:**

1. Verify IAM permissions for your AWS user/profile
2. Check you're deploying to the correct region
3. Stelvio keeps its state in SSM Parameter Store (parameters under `/stlv/`) and an S3
   bucket named `stlv-state-*`. Your profile needs read and write access to both.

### Deployment Failures

**Problem:** Deployment fails with unclear errors

**Solution:**
Run with `-vv` for detailed logs

### Cache Corruption

**Problem:** Strange build errors or outdated code being deployed

**Solution:**
```bash
rm -rf .stelvio/lambda_dependencies
```

### VPC dev networking

Start with `stlv tunnel inspect` and the networking output from `stlv dev`.
See the [full setup and recovery guide](../concepts/dev-mode.md#accessing-a-vpc-from-your-local-handler).

| Symptom | What to check |
| --- | --- |
| Unsupported platform | Managed VPC access needs macOS 15+ on arm64 or x86_64; Linux, WSL, and Windows backends are not implemented |
| Helper missing or version mismatch | Install from the active Stelvio environment; clean an older image with its matching old package before upgrading |
| Session Manager plugin missing | Install `session-manager-plugin` and put it on the `PATH` used to start `stlv` |
| Network still starting | Wait for EC2 and SSM startup; check the selected AWS credentials, region, and permissions |
| Network unavailable after a disconnect | Watch for reconnection; new dependent handlers are gated, but an in-flight database request may fail |
| Route or resolver conflict | Check VPNs, other dev sessions, overlapping CIDRs, and existing resolver files; do not delete foreign resources to bypass the refusal |
| Active owner or uncertain cleanup | Stop the original session, retain ownership records, and follow reconciliation and AWS recovery before retrying |
| `ModuleNotFoundError: pymongo` | Install PyMongo in the environment running `stlv`; Function requirements only control Lambda packaging |
| DocumentDB TLS or discovery failure | Use the linked hostname, port, and CA file; keep TLS verification and replica settings enabled |
| Secrets Manager access denied locally | Your local AWS credentials need access to the linked secret; the Lambda IAM role does not supply local credentials |

`stlv unlock` only addresses application deployment locks. It does not recover
host networking or delete a temporary VPC access stack. `stlv tunnel cleanup`
only removes the local helper and host resources; it is not AWS teardown.

## Getting Help

If you're still stuck:

1. Run your command with `-vv` and check the full output
2. Check the log files for detailed error information
3. Search [GitHub issues](https://github.com/stelviodev/stelvio/issues)
4. Create a new issue with:
    - Your Stelvio version (`stlv version`)
    - The command you ran
    - The error message
    - Relevant logs (with sensitive data removed)
5. Get in touch with us at [@stelviodev](http://x.com/stelviodev) on X (Twitter) or [@michal_stlv](http://x.com/michal_stlv) or [@bascodes](http://x.com/bascodes)
