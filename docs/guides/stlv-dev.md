# `stlv dev`: Run lambdas locally

Stelvio can be used to develop Lambda functions locally within the AWS infrastructure.

## Why you need it

When developing Lambda functions on AWS, every change has to be deployed. Stelvio uses a state managing backend to deploy only the differences when you change code, but you still end up with a deploy time of several seconds each time. Also, you do not have access to a local debugger, you cannot see `print` statements directly, and you do not directly see exceptions.

## What it does

Stelvio's solution is quite simple: Instead of `stlv deploy`, you run `stlv dev`. This command will deploy your entire app to the AWS infrastructure, but it will replace your Lambda functions with a special bridge function. This bridge function will take the incoming request, routes it to an AppSync instance in your AWS account, and starts the Stelvio dev server. This dev server will then run your Lambda function locally and pass the result back to the bridge Lambda function.

This way, you can access your function on something like `https://i6hifvb41g.execute-api.us-east-1.amazonaws.com/`, and the the result of it as it is executed locally.

While the dev server runs, you can make changes to your function code, and see the update on the next request without re-deploying your function.

If an Exception is thrown, the exception will appear in the console and in the lambda's result.

## How it works

No changes to your `stlv_app.py`, or any other project file is needed.

Let's assume your `stlv_app.py` looks like this:


```python
@app.run
def run() -> None:
    # Simple API exposed via API Gateway
    api = Api(
        "MyApi",
    )
    api.route('get', '/', 'functions/api.handler')
```

Running `stlv dev` (instead of `stlv deploy`) will allow you to access the function behind your API Gateway as ususal via `https://i6hifvb41g.execute-api.us-east-1.amazonaws.com/v1`, but the code for the `handler` function in `functions/api.py` will get executed locally.