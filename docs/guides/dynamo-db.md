# Working with DynamoDB in Stelvio

This guide explains how to create and manage DynamoDB tables with Stelvio. You can create tables with partition keys, sort keys, and secondary indexes for efficient querying.

## Creating a DynamoDB table

Creating a DynamoDB table in Stelvio is straightforward. To define fields used as keys or in indexes you can use either friendly field type names or the enum:

```python
from stelvio.aws.dynamo_db import DynamoTable, FieldType

# Using friendly field type strings
orders_table = DynamoTable(
    name="orders",
    fields={
        "customer_id": "string",
        "order_date": "string", 
        "status": "string",
        "total": "number",
    },
    partition_key="customer_id",
    sort_key="order_date"
)

# Or using the FieldType enum
orders_table = DynamoTable(
    name="orders",
    fields={
        "customer_id": FieldType.STRING,
        "order_date": FieldType.STRING,
        "status": FieldType.STRING,
        "total": FieldType.NUMBER,
    },
    partition_key="customer_id",
    sort_key="order_date"
)
```

## Secondary Indexes

DynamoDB supports two types of secondary indexes to improve query performance. You can add them when creating your table:

```python
from stelvio.aws.dynamo_db import DynamoTable, LocalIndex, GlobalIndex

orders_table = DynamoTable(
    name="orders",
    fields={
        "customer_id": "string",
        "order_date": "string", 
        "status": "string",
        "total": "number",
    },
    partition_key="customer_id",
    sort_key="order_date",
    local_indexes={
        "status-index": LocalIndex(sort_key="status")
    },
    global_indexes={
        "status-total-index": GlobalIndex(
            partition_key="status",
            sort_key="total"
        )
    }
)
```

You can also use dictionary syntax instead of dataclasses:

```python
orders_table = DynamoTable(
    name="orders",
    fields={
        "customer_id": "string",
        "order_date": "string",
        "status": "string",
        "total": "number",
    },
    partition_key="customer_id",
    sort_key="order_date",
    local_indexes={
        "status-index": {
            "sort_key": "status",
            "projections": ["total"]
        }
    },
    global_indexes={
        "status-total-index": {
            "partition_key": "status",
            "sort_key": "total",
            "projections": "all"
        }
    }
)
```

### Attribute Projections

You can control which attributes are copied to your indexes using projections:

```python
# Keys only (default)
LocalIndex(sort_key="status")

# All attributes  
LocalIndex(sort_key="status", projections="all")

# Specific attributes
LocalIndex(sort_key="status", projections=["total", "customer_id"])
```

!!! tip "Choosing Projections"
    - Use **"keys-only"** when you only need to retrieve keys for further queries
    - Use **specific attributes** when you know exactly which fields your queries need
    - Use **"all"** when you need to retrieve complete items from index queries (costs more storage)

!!! info "Index Types"
    **Local Secondary Index (LSI)**: Same partition key as your table, different sort key. Queries within the same partition.
    
    **Global Secondary Index (GSI)**: Different partition key (and optionally sort key). Queries across the entire table.

!!! warning "Index Limitations"
    - **Local indexes** can only be created when you create the table - you cannot add or remove them later
    - **Global indexes** can be added or removed from existing tables, but this is a background operation that takes time  
    - Each table can have up to 5 local indexes and 20 global indexes

## DynamoDB Streams

DynamoDB Streams capture changes to your table data in real-time. Enable streams to react to creates, updates, and deletes:

```python
from stelvio.aws.dynamo_db import DynamoTable, StreamView

# Enable streams with string literals
orders_table = DynamoTable(
    name="orders",
    fields={
        "customer_id": "string",
        "order_date": "string",
    },
    partition_key="customer_id",
    sort_key="order_date",
    stream="new-and-old-images"  # Capture before and after data
)

# Or use the StreamView enum
orders_table = DynamoTable(
    name="orders",
    fields={
        "customer_id": "string",
        "order_date": "string",
    },
    partition_key="customer_id",
    sort_key="order_date",
    stream=StreamView.NEW_AND_OLD_IMAGES
)
```

### Stream View Types

Choose what data to capture when items change:

| View Type | String Literal | What's Captured |
|-----------|----------------|----------------|
| **Keys Only** | `"keys-only"` | Only the key attributes of changed items |
| **New Image** | `"new-image"` | The entire item after modification |
| **Old Image** | `"old-image"` | The entire item before modification |
| **New and Old Images** | `"new-and-old-images"` | Both before and after images |

!!! tip "Choosing Stream Types"
    - **Keys Only**: Minimal data, useful when you just need to know what changed
    - **New Image**: Great for replication or downstream processing of current state
    - **Old Image**: Useful for audit trails or rollback scenarios
    - **New and Old Images**: Complete change tracking, but uses more bandwidth

### Accessing Stream Properties

Once streams are enabled, you can access the stream ARN programmatically:

```python
# Check if streams are enabled
if orders_table._config.stream_enabled:
    stream_arn = orders_table.stream_arn  # Output[str] | None
    # Use stream_arn in other Pulumi resources that need it
```

!!! warning "Stream Retention"
    DynamoDB streams retain data for **24 hours only**. After that, the data is automatically deleted. Plan your stream processing accordingly.

## Linking to Lambda Functions

When you link a DynamoDB table to a Lambda function, Stelvio automatically configures the necessary IAM permissions:

```python
from stelvio.aws.dynamo_db import DynamoTable
from stelvio.aws.function import Function

# Create table
users_table = DynamoTable(
    name="users",
    fields={"user_id": "string", "name": "string"},
    partition_key="user_id"
)

# Create function with automatic table access
process_user = Function(
    name="process-user",
    handler="functions/users.handler",
    links=[users_table]  # Automatically gets table + index permissions
)
```

The generated permissions include:

- **Table operations**: `Scan`, `Query`, `GetItem`, `PutItem`, `UpdateItem`, `DeleteItem`
- **Index operations**: `Query`, `Scan` (read-only access to all indexes)

!!! info "Index Permissions"
    DynamoDB indexes only support `Query` and `Scan` operations. Write operations (`PutItem`, `UpdateItem`, `DeleteItem`) are only performed on the main table, with DynamoDB automatically maintaining the indexes.

## Field Types

Stelvio supports both friendly string names and enum values for field types:

| Friendly String | Enum | DynamoDB Type | Use For |
|----------------|------|---------------|---------|
| `"string"` | `FieldType.STRING` | `S` | Text data, IDs |
| `"number"` | `FieldType.NUMBER` | `N` | Numeric data |
| `"binary"` | `FieldType.BINARY` | `B` | Binary data |

You can also use the DynamoDB type directly (`"S"`, `"N"`, `"B"`) if you prefer.

## Next Steps

Now that you understand DynamoDB basics, you might want to explore:

- [Working with Lambda Functions](lambda.md) - Learn how to process DynamoDB data
- [Working with API Gateway](api-gateway.md) - Build APIs that interact with your tables
- [Linking](linking.md) - Understand how Stelvio automates IAM permissions and environment variables
- [Project Structure](project-structure.md) - Discover patterns for organizing your Stelvio applications