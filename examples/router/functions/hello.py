def handler(_event: any, _context: any) -> dict[str, str]:
    return {"statusCode": 200, "body": "Hello, World (from API Gateway)!"}
