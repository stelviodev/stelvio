# `stlv dev`: run Lambda functions locally

`stlv dev` is like `stlv deploy`, but your Lambda handler code runs on your machine while the rest of your infrastructure stays in AWS.

## Why you'd use it

- No redeploy on every code change
- See `print()` output immediately
- Exceptions show up in your terminal (and in the Lambda response)
- Easy to attach a local debugger

## How it works

When you run `stlv dev`, Stelvio deploys your app in **dev mode**:

- Your Lambdas get replaced with a small stub Lambda.
- The stub forwards each invocation over an AppSync Events channel.
- The `stlv dev` process runs a local dev server, executes your real handler, then sends the result back to AWS.

Your public entrypoint (API Gateway URL / Function URL) stays the same — you hit it like normal.

## Using it

No changes to `stlv_app.py` are needed.

```python
@app.run
def run() -> None:
    api = Api("MyApi")
    api.route("get", "/", "functions/api.handler")
```

Start dev mode:

```bash
stlv dev            # uses your personal environment
stlv dev staging    # explicit environment
```

Now call your API like you normally would (e.g. `https://...execute-api.../v1/`). Edit your function code and refresh — the next request picks it up.

To stop the local server: `Ctrl+C`. 

!!! warning
    Stopping the dev server won't change the infrastructure in AWS. In particular, the replacement Lambda function that handles the dev mode ("stub Lambda") will remain deployed.

    You need to re-deploy without dev mode using `stlv deploy` again to switch back to your real Lambda code being deployed to AWS.

!!! warning
    `stlv dev` still deploys real AWS resources (so it needs AWS credentials and it can cost money). It will also create an AppSync API named `stelvio` in your account if it's not already present. The AppSync API is a purely usage-based cost model.


## Limitations

### Target Architecture

Stelvio's dev mode will execute your local Lambda functions natively, i.e. the Python interpreter is used on your workstation's operating system and CPU architecture, no matter your Lambda function definition. There is no emulator in place that would match the os/architecture on AWS with the one on your workstation.

Since your workstation is likely equipped with more RAM than your Lambda function, this also mean that you're less likely to run into memory limits on your workstation.

### Request/Response Size Limits

Dev mode supports the same payload limits as [AWS Lambda](https://docs.aws.amazon.com/lambda/latest/dg/gettingstarted-limits.html):

- **6 MB** for request and response (synchronous invocations)
- **1 MB** for asynchronous invocations
