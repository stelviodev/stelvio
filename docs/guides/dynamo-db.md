# Working with DynamoDB in Stelvio

This guide explains how to create and manage DynamoDB tables with Stelvio. Currently
support is limited and all you can do is to create table and define partition and sort
keys.

## Creating a DynamoDB table

Creating a DynamoDB table in Stelvio is straightforward.

```python
from stelvio.aws.dynamo_db import AttributeType, DynamoTable

todos_dynamo = DynamoTable(
    name="orders",
    fields={
        "customer_id": AttributeType.STRING,
        "order_date": AttributeType.STRING,
    },
    partition_key="customer_id",
    sort_key='order_date'
)
```

That's it!




## Indexes

Support for indexes is coming soon. 

TBD

## Next Steps

Now that you understand DynamoDB basics, you might want to explore:

- [Working with Lambda Functions](lambda.md) - Learn more about how to work with Lambda functions
- [Working with API Gateway](api-gateway.md) - Learn how to create APIs
- [Linking](linking.md) - Learn how linking automates IAM, permissions, envars and more
- [Project Structure](project-structure.md) - Discover patterns for organizing your Stelvio applications