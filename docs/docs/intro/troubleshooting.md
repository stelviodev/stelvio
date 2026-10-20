# Troubleshooting

This guide helps you debug common issues and understand Stelvio's internal workings when things go wrong.

## Debugging with Verbose Output

When you encounter issues, use the verbose flags to get more detailed information:

```bash
# Show INFO level logs
stlv deploy -v

# Show DEBUG level logs (most detailed)
stlv deploy -vv
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
- If a command crashes, leftover directories can be safely deleted

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

### Native VPC helper issues

The `stlv tunnel` administration commands run on macOS. `install` requires macOS
15 or later and asks for administrator authorization. Run Stelvio as your normal
user; only the helper runs with elevated privileges.

If installation reports a missing or invalid Session Manager plugin, fix the
download or cache error and run `stlv tunnel install` again. The command reports
plugin errors directly and stops before it authorizes or changes the helper.
`status`, `cleanup`, and `uninstall` do not download the plugin.

If `status` reports uncertain state, run `stlv tunnel cleanup` after active
networking sessions have stopped. The helper refuses to remove routes or resolver
files that it cannot prove it owns. Keep that evidence and ask for help; do not
delete files under `/Library/Application Support/Stelvio/tunnel` or `/etc/resolver`.

If installation reports an active helper, close the networking session and retry.
Use `stlv tunnel status` to see local helper ownership and whether any VPC unit
remains active. This does not verify AWS or database connectivity.

### Managed VPC transport identity and authorization

Managed VPC access stops when the captured AWS account or region differs from the
deployment metadata, the bastion or its security groups are no longer owned by the
deployment, or inbound SSH is enabled.
Check `AwsConfig`, the selected profile, and the deployment's account and region;
redeploy when the access resources or metadata have changed. Do not bypass a host
key or ownership refusal.

The AWS identity used by Stelvio needs permission to inspect the VPC and its
subnets, bastion, security groups, and DocumentDB resources; use Systems Manager
sessions; and send an EC2 Instance Connect key. Check the selected
profile's IAM policy and confirm the bastion is running and its SSM agent is
online. Stelvio does not create or replace a bastion during a connection attempt.

SSH host-key changes are a separate refusal. The error names the instance and
shows the expected and presented fingerprints. First check whether the change
was authorized. Restore the original host key when the change was unexpected.
For an intentional rotation, stop all tunnels for that instance and reset only
its trust entry:

```bash
stlv tunnel trust reset --partition aws --account 123456789012 \
  --region us-east-1 --instance-id i-0123456789abcdef0
```

The command shows the stored fingerprint and asks for confirmation. The next
connection establishes a new pin through SSM. Reset does not change AWS resources,
install the helper, or remove other pins. A new instance ID does not require
resetting the old instance's pin.

If the error says the private Session Manager plugin is missing, corrupt, or
unsafe, run `stlv tunnel install` from the active Stelvio environment to repair
the verified cache entry. A local plugin artifact error is terminal for that
attempt; it is not an AWS connectivity retry. Authentication details are not
included in transport errors.

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

### Dev mode bridge does not connect

The bridge waits up to 10 seconds for each AppSync connection or subscription acknowledgement. If it times out, check network access and the AppSync API key, then restart `stlv dev`.

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
