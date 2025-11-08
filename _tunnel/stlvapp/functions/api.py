import json
import random

MODULE_LEVEL_VARIABLE = random.randint(1, 100)


def handler(event: dict, context: any) -> dict:
    a = 1
    b = 2
    c = a + b
    random_value = random.randint(1, 100)
    return {
        "statusCode": 200,
        "body": json.dumps(
            {
                "message": "Hello from Stelvio API!",
                "data": {
                    "a": a,
                    "b": b,
                    "c": c,
                    # "d": str(uuid.uuid4()),
                    "randomValue": random_value,
                    "moduleLevelVariable": MODULE_LEVEL_VARIABLE,
                },
            }
        ),
    }
