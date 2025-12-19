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
- Can be customized (see [Environments guide](environments.md#customizing-your-personal-environment-name))

**`lambda_dependencies/`**

- Cached Lambda and Layer dependencies
- Safe to delete if you suspect corruption - regenerated on next deployment

**`{timestamp}-{random}/`** (temporary working directory)

- Created when running commands that need state (`diff`, `deploy`, `refresh`, `destroy`, `outputs`, `state` commands)
- Contains `.pulumi/stacks/{app}/{env}.json` - state downloaded from S3
- Automatically deleted when command completes
- If a command crashes, leftover directories can be safely deleted

## Renaming Your App or Environment

See [State Management - Renaming](state.md#renaming) for how to safely rename your app or environment.

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

**Problem:** "Unable to locate credentials" or "Invalid security token"

**Solution:**
Make sure your AWS credentials are setup properly. 

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

**Problem:** "Access Denied" when accessing AWS resources

**Solutions:**

1. Verify IAM permissions for your AWS user/profile
2. Check you're deploying to the correct region
3. Ensure Parameter Store access is allowed for passphrases

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
