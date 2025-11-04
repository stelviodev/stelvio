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

**macOS:** `~/Library/Logs/stelvio/`
**Linux:** `~/.local/state/stelvio/logs/`
**Windows:** `%LOCALAPPDATA%\stelvio\logs\`


## The .stelvio Directory

Each Stelvio project has a `.stelvio/` directory in the project root that contains important metadata:

### Project Identity Files

**`.stelvio/appname`**
- Contains your application name (e.g., "my-api")
- Created when you first deploy

**`.stelvio/userenv`**
- Contains your personal environment name
- Defaults to your computer username
- Can be customized (see [Environments guide](environments.md#customizing-your-personal-environment-name))

### Cache and Temporary Files

The `.stelvio/` directory may also contain:
- Lambda & Layers dependencies cache
- Build artifacts
- Temporary deployment files

You can safely delete cache files if you suspect corruption - they'll be regenerated on next deployment.

## Project Rename Detection

Stelvio tracks your project identity to prevent accidental infrastructure conflicts.

### How It Works

When you first deploy, Stelvio:
1. Creates `.stelvio/appname` with your application name
2. Uses this to identify your project in future deployments
3. Prevents accidentally deploying the same code as a different app

### Renaming Your Project

!!! warning
    Renaming the app does not rename deployed resources!

    Renaming creates new infrastructure. 
    Your old infrastructure will remain under the old name. 
    
    Use `stlv destroy` with the old app name to clean it up.

If you need to rename your project:

1. **Update your code:**
   ```python
   app = StelvioApp("new-name")  # Change this in stlv_app.py
   ```

2. **Update the identity file:**
   ```bash
   echo "new-name" > .stelvio/appname
   ```

3. **Deploy to create new infrastructure:**
   ```bash
   stlv deploy
   ```


If you only rename the app in `stlv_app.py` CLI will detect this and inform you.
You'll have option to confirm new name. Previously deployed infrastructure will stay in place.

## Common Issues and Solutions

### State Lock Errors

If you kill deploy/destroy operation while running or something crashes 
unexpectedly you might not be able to deploy. 

**Problem:** You get "Stack is currently being updated"

**Solution:**
```bash
stlv unlock [environment]
```

Only use this if you're certain no other deployment is actually running.

### AWS Credential Issues

**Problem:** "Unable to locate credentials" or "Invalid security token"

**Solution:**
Make sure your AWS credentials are setup properly. 

You have three options: 

1. Region and profile is set in stlv_app.py and profile exists. 
   ```bash title="stlv_app.py" hl_lines="5 6"
   @app.config
   def configuration(env: str) -> StelvioAppConfig:
    return StelvioAppConfig(
        aws=AwsConfig(
            region="us-east-1",
            profile="michal",
        ),
    )
   ```

2. Environment variable `AWS_PROFILE` is set and profile exists:
   ```bash
   export AWS_PROFILE=YOUR_PROFILE_NAME
   ```

3. Environment variables `AWS_ACCESS_KEY_ID` and `AWS_SECRET_ACCESS_KEY` are set:
   ```bash
   export AWS_ACCESS_KEY_ID="<YOUR_ACCESS_KEY_ID>"
   export AWS_SECRET_ACCESS_KEY="<YOUR_SECRET_ACCESS_KEY>"
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
# Clear all Stelvio caches
rm -rf .stelvio/cache
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

## Next Steps

- [State and Infrastructure](state-and-infrastructure.md) - Understand how Stelvio manages state
- [Using CLI](using-cli.md) - Master all CLI commands
- [Environments](environments.md) - Learn about environment management