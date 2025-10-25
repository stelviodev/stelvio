# Real-Time Messaging System with Stelvio

This implementation provides a real-time messaging system using DynamoDB Streams as an alternative to AppSync WebSocket API.

## Architecture

```
┌─────────────────┐    ┌──────────────────┐    ┌─────────────────────┐
│   API Gateway   │    │    DynamoDB      │    │   Stream Processor  │
│   (REST API)    │───►│   + Streams      │───►│   (Echo Handler)    │
└─────────────────┘    └──────────────────┘    └─────────────────────┘
        │                        │                        │
        │                        │                        │
   Receives               Real-time                  Processes
   HTTP Requests          Events                     & Echoes
                                                     Messages
```

## Components

### 1. DynamoDB Table (`messages`)
- **Purpose**: Stores messages with real-time stream processing
- **Stream**: Enabled with "new-and-old-images" 
- **Schema**:
  - `id` (partition key): Unique message identifier
  - `timestamp` (sort key): Message timestamp
  - `message`: Message content
  - `sender`: Source of the message

### 2. API Gateway Endpoints
- **GET `/`**: Sends a default message to the system
- **POST `/message`**: Accepts custom messages via JSON body

### 3. Stream Processor (`echo_processor.handler`)
- **Purpose**: Processes DynamoDB stream events and echoes messages back
- **Behavior**: Creates echo messages for all incoming messages
- **Anti-loop**: Prevents infinite loops by skipping its own echo messages

### 4. WebSocket Client Simulator (`websocket_client.py`)
- **Purpose**: Simulates WebSocket client behavior by polling DynamoDB
- **Features**: Real-time message display with color coding
- **Usage**: Echoes all messages to stdout as they arrive

## How It Works

1. **Message Sending**: HTTP requests to API Gateway endpoints write messages to DynamoDB
2. **Stream Trigger**: DynamoDB streams automatically trigger the echo processor function
3. **Echo Processing**: The processor creates echo messages and writes them back to the table
4. **Real-time Display**: The WebSocket client simulator polls for new messages and displays them

## Usage

### Deploy the System
```bash
# Deploy using Stelvio
pulumi up
```

### Run the WebSocket Client Simulator
```bash
# Replace with your actual table name (check AWS console or Pulumi output)
python websocket_client.py stlvapp-test-messages
```

### Send Messages

#### Via GET request:
```bash
curl https://your-api-url.amazonaws.com/v1/
```

#### Via POST request:
```bash
curl -X POST https://your-api-url.amazonaws.com/v1/message \
  -H "Content-Type: application/json" \
  -d '{"message": "Hello, real-time world!", "sender": "curl-client"}'
```

## Expected Flow

1. **Send Message**: API call writes message to DynamoDB
2. **Stream Event**: DynamoDB stream triggers echo processor
3. **Echo Response**: Processor writes echo message back to table
4. **Client Display**: WebSocket client displays both original and echo messages

## Advantages of This Approach

- ✅ **Real-time Processing**: DynamoDB streams provide near real-time event processing
- ✅ **Scalable**: Automatically scales with AWS managed services
- ✅ **Persistent**: Messages are stored in DynamoDB for history/replay
- ✅ **Cost Effective**: Uses existing AWS services without additional WebSocket infrastructure
- ✅ **Stelvio Native**: Uses Stelvio's built-in DynamoDB components and patterns

## Limitations

- 📝 **Not True WebSocket**: Uses polling instead of persistent connection
- 📝 **Latency**: Slight delay due to polling interval (configurable)
- 📝 **DynamoDB Costs**: Read/write operations on DynamoDB table

## Next Steps

To make this more WebSocket-like, you could:
1. Use API Gateway WebSocket API with Stelvio (when supported)
2. Implement Server-Sent Events (SSE) for browser clients
3. Add real WebSocket server using Lambda containers
4. Use AWS IoT Core for true publish/subscribe messaging