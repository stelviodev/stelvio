# SST Tunnel Feature - Developer Onboarding Guide

## Overview

The SST tunnel feature is a sophisticated development tool that enables local debugging of serverless applications by creating secure connections between AWS infrastructure and the local development environment. This guide provides comprehensive documentation for developers working on or with the tunnel feature implementation.

## Table of Contents

1. [Conceptual Overview](#conceptual-overview)
2. [Architecture Components](#architecture-components) 
3. [Technical Implementation](#technical-implementation)
4. [Code Organization](#code-organization)
5. [Development Workflow](#development-workflow)
6. [Testing and Debugging](#testing-and-debugging)
7. [Platform Differences](#platform-differences)

---

## Conceptual Overview

### What is the SST Tunnel Feature?

SST's tunnel feature consists of two distinct but related functionalities:

#### 1. **Live Lambda Development** 
- Allows Lambda functions to run locally while being invoked from AWS
- Uses AWS AppSync Events for real-time communication
- Proxies function requests to local development environment
- Enables breakpoint debugging and instant code reloads (< 10ms)

#### 2. **VPC Network Tunnel**
- Creates secure network tunnel to VPC resources
- Uses SSH tunnel with SOCKS5 proxy
- Enables local access to private VPC resources (RDS, Redis, etc.)
- Requires bastion host in VPC

### Key Benefits

- **Instant feedback**: Changes appear in < 10ms without redeployment
- **Real debugging**: Set breakpoints and inspect variables in your IDE
- **Authentic environment**: Uses real IAM permissions and AWS infrastructure
- **VPC access**: Connect to private databases and services from local machine
- **Webhook testing**: External services can invoke your local functions

### When to Use

The tunnel feature is automatically activated during `sst dev` when:
- Your app contains Lambda functions (Live development)
- Your app has a VPC with `bastion: true` enabled (Network tunnel)

---

## Architecture Components

### Live Lambda Development Architecture

```
┌─────────────────┐    ┌──────────────────┐    ┌─────────────────────┐
│   AWS Lambda    │    │  AWS AppSync     │    │  Local Machine     │
│   (Stub/Bridge) │◄──►│  Events API      │◄──►│  (Real Function)    │
└─────────────────┘    └──────────────────┘    └─────────────────────┘
        │                        │                        │
        │                        │                        │
    Receives               Real-time                 Executes
   Invocation            WebSocket                  Function
                        Connection                   Locally
```

**Flow:**
1. External request hits AWS Lambda endpoint
2. Stub Lambda (bridge) publishes request to AppSync Events
3. Local WebSocket client receives event and executes function locally
4. Response is published back through AppSync Events
5. Stub Lambda returns response to original caller

### VPC Network Tunnel Architecture

```
┌─────────────────┐    ┌──────────────────┐    ┌─────────────────────┐
│ Local Machine   │    │   Bastion Host   │    │   VPC Resources     │
│                 │    │   (EC2)          │    │   (RDS/Redis/etc.)  │
│ ┌─────────────┐ │    │                  │    │                     │
│ │ tun2socks   │ │    │                  │    │                     │
│ │ TUN/TAP     │◄┼────┼──SSH Tunnel──────┼────┤                     │
│ │ Interface   │ │    │                  │    │                     │
│ └─────────────┘ │    │ ┌──────────────┐ │    │                     │
│ ┌─────────────┐ │    │ │ SOCKS5 Proxy │ │    │                     │
│ │SOCKS5 Client│◄┼────┼►│              │ │    │                     │
│ └─────────────┘ │    │ └──────────────┘ │    │                     │
└─────────────────┘    └──────────────────┘    └─────────────────────┘
```

**Flow:**
1. Network requests to VPC CIDR ranges (10.0.x.x) are captured by TUN interface
2. Traffic is routed through SOCKS5 proxy over SSH tunnel
3. Bastion host forwards traffic to VPC resources
4. Responses travel back through the same tunnel

---

## Technical Implementation

### Core Components

#### 1. Bridge Function (`platform/functions/bridge/bridge.go`)

The bridge is a lightweight Lambda function that replaces your actual function during development:

- **Purpose**: Proxy requests between AWS and your local environment
- **Communication**: Uses AWS AppSync Events API for real-time messaging
- **Environment**: Filters and forwards environment variables
- **Error Handling**: Manages timeouts and connection failures

**Key Responsibilities:**
- Subscribe to AppSync Events channel for responses
- Publish incoming Lambda requests to AppSync
- Handle initialization and environment setup
- Manage request/response correlation via request IDs

#### 2. AppSync Events Connection (`cmd/sst/mosaic/aws/appsync/appsync.go`)

Manages the WebSocket connection to AWS AppSync Events:

- **Authentication**: Uses AWS Signature V4 for secure connections
- **Reconnection**: Automatic reconnection on connection failures
- **Proxy Support**: Respects HTTP_PROXY and HTTPS_PROXY environment variables
- **Subscription Management**: Handles multiple channel subscriptions

**Key Features:**
- Keep-alive mechanism with configurable timeouts
- Message ordering and reassembly for large payloads
- Automatic resubscription after reconnection
- Error handling and retry logic

#### 3. Bridge Client (`cmd/sst/mosaic/aws/bridge/bridge.go`)

Provides the communication protocol between local environment and Lambda:

- **Message Types**: Init, Ping, Next, Response, Error, Reboot
- **Streaming**: Supports large message payloads via chunked streaming
- **Ordering**: Ensures message order via sequence numbers
- **Correlation**: Tracks request/response pairs via unique IDs

#### 4. Tunnel Implementation (`pkg/tunnel/`)

Platform-specific implementations for network tunneling:

**Common Interface (`tunnel.go`):**
```go
type tunnelPlatform interface {
    destroy() error
    start(routes ...string) error
    install() error
}
```

**Platform Implementations:**
- `tunnel_darwin.go` - macOS using `route` and `ifconfig`
- `tunnel_linux.go` - Linux using `ip` commands
- `tunnel_windows.go` - Windows using `route` commands

#### 5. Node.js Runtime (`platform/functions/nodejs-runtime/index.ts`)

Local Lambda runtime that mimics AWS Lambda environment:

- **Context**: Provides Lambda context object with AWS metadata
- **Environment**: Mirrors Lambda environment variables
- **Error Handling**: Captures and formats errors for AWS compatibility
- **Event Processing**: Handles various Lambda trigger types

### Message Flow Details

#### Live Function Invocation Sequence

1. **Initialization**
   ```go
   // Bridge sends init message with function metadata
   init := bridge.InitBody{
       FunctionID:  SST_FUNCTION_ID,
       Environment: filteredEnvVars,
   }
   ```

2. **Request Processing**
   ```go
   // Bridge receives Lambda invocation
   resp := http.Get("http://" + LAMBDA_RUNTIME_API + "/2018-06-01/runtime/invocation/next")
   requestID := resp.Header.Get("lambda-runtime-aws-request-id")
   
   // Forwards to AppSync Events
   writer := client.NewWriter(bridge.MessageNext, prefix+"/in")
   resp.Write(writer)
   ```

3. **Local Execution**
   ```typescript
   // Local runtime processes request
   const context = {
     awsRequestId: requestId,
     functionName: process.env.AWS_LAMBDA_FUNCTION_NAME!,
     // ... other context properties
   };
   
   const result = await handler(event, context);
   ```

4. **Response Return**
   ```go
   // Local client sends response back
   if msg.Type == bridge.MessageResponse && msg.ID == requestID {
       http.Post("http://"+LAMBDA_RUNTIME_API+"/2018-06-01/runtime/invocation/"+requestID+"/response", 
                "application/json", msg.Body)
   }
   ```

#### Tunnel Connection Sequence

1. **Installation** (One-time setup)
   ```bash
   sudo sst tunnel install  # Creates TUN/TAP interface
   ```

2. **Tunnel Startup**
   ```go
   // Start platform-specific networking
   err := tunnel.Start(subnets...)
   
   // Start tun2socks bridge
   key := &engine.Key{
       Device: name,
       Proxy:  "socks5://127.0.0.1:1080",
   }
   engine.Start()
   ```

3. **SSH Connection**
   ```go
   // Establish SSH tunnel to bastion
   config := &ssh.ClientConfig{
       User: username,
       Auth: []ssh.AuthMethod{ssh.PublicKeys(signer)},
   }
   sshClient, _ := ssh.Dial("tcp", host, config)
   ```

4. **SOCKS5 Proxy**
   ```go
   // Route traffic through SSH tunnel
   server := socks5.New(&socks5.Config{
       Dial: func(ctx context.Context, network, addr string) (net.Conn, error) {
           return sshClient.Dial(network, addr)
       },
   })
   ```

---

## Code Organization

### Directory Structure

```
sst/
├── cmd/sst/
│   ├── tunnel.go                    # CLI tunnel command
│   └── mosaic/
│       ├── aws/
│       │   ├── appsync/appsync.go  # AppSync Events client
│       │   └── bridge/bridge.go    # Bridge protocol implementation
│       └── dev/dev.go              # Development server
├── pkg/
│   ├── tunnel/                     # Network tunnel implementation
│   │   ├── tunnel.go              # Common tunnel interface
│   │   ├── tunnel_darwin.go       # macOS implementation
│   │   ├── tunnel_linux.go        # Linux implementation
│   │   ├── tunnel_windows.go      # Windows implementation
│   │   └── proxy.go               # SOCKS5 proxy over SSH
│   └── server/                    # Local development server
└── platform/
    └── functions/
        ├── bridge/                # AWS Lambda bridge function
        └── nodejs-runtime/        # Local Lambda runtime
```

### Key Files and Their Purpose

#### Core Tunnel Files

- **`cmd/sst/tunnel.go`**: CLI interface for tunnel commands
  - Handles `sst tunnel` and `sst tunnel install` commands
  - Manages tunnel lifecycle and error reporting
  - Interfaces with platform-specific implementations

- **`pkg/tunnel/tunnel.go`**: Common tunnel functionality
  - Defines cross-platform interface
  - Manages tun2socks integration
  - Provides lifecycle management (start/stop/install)

- **`pkg/tunnel/proxy.go`**: SSH tunnel and SOCKS5 proxy
  - Establishes SSH connection to bastion host
  - Creates SOCKS5 proxy server for traffic routing
  - Handles authentication and error scenarios

#### Live Development Files

- **`platform/functions/bridge/bridge.go`**: Lambda bridge function
  - Deployed as replacement for actual Lambda functions
  - Handles AppSync Events communication
  - Manages request/response correlation and timeouts

- **`cmd/sst/mosaic/aws/appsync/appsync.go`**: AppSync Events client
  - WebSocket connection management
  - Message publishing and subscription
  - Authentication and reconnection logic

- **`cmd/sst/mosaic/aws/bridge/bridge.go`**: Bridge protocol
  - Message serialization and chunking
  - Request/response correlation
  - Error handling and retries

- **`platform/functions/nodejs-runtime/index.ts`**: Local Lambda runtime
  - Mimics AWS Lambda execution environment
  - Provides Lambda context and utilities
  - Handles various event sources (API Gateway, S3, etc.)

#### Development Server

- **`cmd/sst/mosaic/dev/dev.go`**: Development mode coordination
  - Manages live function execution
  - Provides API endpoints for environment variables
  - Streams deployment events to UI

---

## Development Workflow

### Setting Up Development Environment

1. **Prerequisites**
   ```bash
   # Install dependencies
   bun install
   go mod tidy
   cd platform && bun run build
   ```

2. **Running Locally**
   ```bash
   # Test with example app
   cd examples/aws-api
   go run ../../cmd/sst dev
   ```

3. **Building CLI**
   ```bash
   # Create binary
   go build ./cmd/sst
   ```

### Testing Tunnel Feature

#### VPC Tunnel Testing

1. **Create Test VPC**
   ```typescript
   // In sst.config.ts
   const vpc = new sst.aws.Vpc("TestVpc", { 
     bastion: true,
     nat: "managed" 
   });
   ```

2. **Install Tunnel Interface**
   ```bash
   sudo sst tunnel install
   ```

3. **Test Connection**
   ```bash
   sst dev  # Should show tunnel tab
   # In another terminal, test VPC connectivity
   ```

#### Live Function Testing

1. **Create Test Function**
   ```typescript
   // In sst.config.ts
   new sst.aws.Function("TestFunction", {
     handler: "src/lambda.handler",
     url: true
   });
   ```

2. **Enable Live Development**
   ```bash
   sst dev  # Functions run locally by default
   ```

3. **Test Function Invocation**
   ```bash
   # Function URL will be available in output
   curl https://[function-url]/
   ```

### Common Development Tasks

#### Adding New Platform Support

1. **Create Platform File**
   ```go
   // pkg/tunnel/tunnel_newplatform.go
   //go:build newplatform

   package tunnel

   func init() {
       impl = &newPlatformTunnel{}
   }

   type newPlatformTunnel struct{}

   func (t *newPlatformTunnel) install() error {
       // Platform-specific installation
   }

   func (t *newPlatformTunnel) start(routes ...string) error {
       // Platform-specific startup
   }

   func (t *newPlatformTunnel) destroy() error {
       // Platform-specific cleanup
   }
   ```

#### Modifying Bridge Protocol

1. **Add New Message Type**
   ```go
   // In bridge.go
   const (
       MessageInit MessageType = iota
       // ... existing types
       MessageNewType  // Add new type
   )
   ```

2. **Handle in Bridge Function**
   ```go
   // In platform/functions/bridge/bridge.go
   if msg.Type == bridge.MessageNewType {
       // Handle new message type
   }
   ```

3. **Handle in Local Client**
   ```go
   // In client code
   case msg := <-client.Read():
       if msg.Type == bridge.MessageNewType {
           // Process new message type
       }
   ```

#### Debugging Connection Issues

1. **Enable Debug Logging**
   ```bash
   SST_LOG=debug sst dev
   ```

2. **Check AppSync Events**
   ```bash
   # Verify AWS credentials and region
   aws appsync list-api-keys --region [region]
   ```

3. **Test SSH Connection**
   ```bash
   # Manual SSH test to bastion
   ssh -i [private-key] [user]@[bastion-ip]
   ```

---

## Testing and Debugging

### Debugging Live Functions

#### Local Debugging Setup

1. **VS Code Configuration**
   ```json
   // .vscode/launch.json
   {
     "type": "node",
     "request": "attach",
     "name": "Attach to SST",
     "port": 9229,
     "skipFiles": ["<node_internals>/**"]
   }
   ```

2. **Enable Debug Mode**
   ```bash
   # Start with Node.js debugging
   SST_DEBUG=true sst dev
   ```

#### Common Debug Scenarios

1. **Function Not Responding**
   - Check AppSync Events connection status
   - Verify WebSocket connectivity
   - Check local function syntax errors

2. **Environment Variables Missing**
   - Verify environment variable filtering in bridge
   - Check linking configuration
   - Validate AWS credentials

3. **Timeout Issues**
   - Check Lambda timeout configuration
   - Verify AppSync Events keep-alive
   - Monitor network latency

### Debugging Tunnel Issues

#### Network Connectivity

1. **Test Route Configuration**
   ```bash
   # Check routing table
   netstat -rn | grep 10.0
   
   # Test VPC connectivity
   ping [vpc-resource-ip]
   ```

2. **SSH Connection Debugging**
   ```bash
   # Test SSH connection manually
   ssh -v -i [private-key] [user]@[bastion-ip]
   
   # Check SSH key permissions
   chmod 600 [private-key]
   ```

#### Platform-Specific Issues

1. **macOS Issues**
   ```bash
   # Check TUN/TAP interface
   ifconfig | grep utun
   
   # Verify route commands
   sudo route -n get 10.0.0.0
   ```

2. **Linux Issues**
   ```bash
   # Check TUN interface
   ip link show | grep tun
   
   # Verify iptables rules
   sudo iptables -L -n
   ```

### Automated Testing

#### Integration Tests

```bash
# Run tunnel integration tests
go test ./pkg/tunnel/... -integration

# Run bridge protocol tests  
go test ./cmd/sst/mosaic/aws/bridge/... -v
```

#### Manual Test Scenarios

1. **End-to-End Function Test**
   - Deploy function with `sst deploy`
   - Test with `sst dev`
   - Compare responses for consistency

2. **VPC Access Test**
   - Create RDS instance in VPC
   - Connect from local application
   - Verify data access through tunnel

---

## Platform Differences

### macOS Implementation (`tunnel_darwin.go`)

- **TUN Interface**: Uses `utun` devices provided by macOS
- **Route Management**: Uses `route` command for routing table manipulation
- **Permissions**: Requires root for network interface creation
- **Dependencies**: No external dependencies, uses built-in tools

**Key Commands:**
```bash
# Create TUN interface
sudo ifconfig utun[N] 10.0.0.1 10.0.0.2 up

# Add routes
sudo route -n add -net 10.0.0.0/22 10.0.0.2
```

### Linux Implementation (`tunnel_linux.go`)

- **TUN Interface**: Uses Linux TUN/TAP interface
- **Route Management**: Uses `ip` command from iproute2 package
- **Permissions**: Requires CAP_NET_ADMIN capability
- **Dependencies**: Requires iproute2 package

**Key Commands:**
```bash
# Create TUN interface  
sudo ip tuntap add dev tun[N] mode tun

# Configure interface
sudo ip addr add 10.0.0.1/32 dev tun[N]
sudo ip link set tun[N] up

# Add routes
sudo ip route add 10.0.0.0/22 dev tun[N]
```

### Windows Implementation (`tunnel_windows.go`)

- **TUN Interface**: Uses TAP-Windows adapter
- **Route Management**: Uses Windows `route` command
- **Permissions**: Requires administrator privileges
- **Dependencies**: Requires TAP-Windows driver installation

**Key Commands:**
```cmd
# Configure interface (via netsh)
netsh interface ip set address "TAP-Windows" static 10.0.0.1 255.255.252.0

# Add routes
route add 10.0.0.0 mask 255.255.252.0 10.0.0.2
```

### Cross-Platform Considerations

#### Networking Differences

- **Interface Names**: Different naming conventions (utun vs tun vs TAP)
- **Command Syntax**: Different tools and argument formats
- **Permissions**: Different privilege requirements and mechanisms
- **Error Handling**: Platform-specific error codes and messages

#### Build Tags

Each platform implementation uses Go build tags:

```go
//go:build darwin
package tunnel

//go:build linux  
package tunnel

//go:build windows
package tunnel
```

#### Testing Considerations

- **CI/CD**: Different test environments for each platform
- **Virtualization**: Platform-specific virtualization requirements
- **Permissions**: Automated testing requires elevated privileges
- **Dependencies**: Platform-specific external tool requirements

---

## Troubleshooting Guide

### Common Issues and Solutions

#### 1. "Tunnel needs to be installed"
```bash
# Solution: Install tunnel interface
sudo sst tunnel install

# Verify installation
ls -la /opt/sst/tunnel
```

#### 2. "No tunnels found for stage"
- Ensure VPC has `bastion: true` enabled
- Verify VPC deployment completed successfully  
- Check stage name matches deployed stage

#### 3. "Connection timeout" in AppSync Events
- Check AWS credentials and region
- Verify internet connectivity
- Check for proxy/firewall restrictions

#### 4. "Permission denied" during tunnel start
- Ensure tunnel was installed with sudo
- Verify user has proper permissions
- Check if tunnel binary exists and is executable

#### 5. Functions not responding in live mode
- Check local function syntax and imports
- Verify WebSocket connection to AppSync Events
- Check for network connectivity issues
- Review SST_DEBUG logs for detailed error information

### Performance Optimization

#### Network Tunnel Performance

1. **Reduce Latency**
   - Use bastion host in same AZ as resources
   - Optimize SSH cipher selection
   - Consider connection pooling for high-frequency access

2. **Improve Throughput**
   - Adjust SOCKS5 buffer sizes
   - Use SSH compression for large data transfers
   - Monitor and adjust TCP window sizes

#### Live Function Performance

1. **Reduce Cold Start Time**
   - Keep WebSocket connections warm
   - Pre-initialize function dependencies
   - Use connection pooling for external services

2. **Optimize Message Size**
   - Implement efficient serialization
   - Use compression for large payloads
   - Batch small requests when possible

This comprehensive guide should provide developers with the necessary understanding to work effectively with SST's tunnel feature, whether they're debugging issues, adding new functionality, or optimizing performance.