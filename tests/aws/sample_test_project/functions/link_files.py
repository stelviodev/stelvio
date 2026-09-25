from pathlib import Path


def handler(event, context):
    return {"cwd": str(Path.cwd()), "contents": Path(event["path"]).read_text()}
