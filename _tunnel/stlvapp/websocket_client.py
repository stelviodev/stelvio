#!/usr/bin/env python3
"""
WebSocket Client Simulation Script

This script simulates a WebSocket client that connects to a real-time messaging system.
In this implementation, it polls the DynamoDB table for new messages and echoes them to stdout,
simulating the behavior of a WebSocket connection receiving real-time messages.

Usage:
    python websocket_client.py
"""

import json
import time
import threading
import boto3
from datetime import datetime, timedelta
import sys


class WebSocketSimulator:
    """Simulates a WebSocket client by polling DynamoDB for new messages."""
    
    def __init__(self, table_name, poll_interval=2):
        self.table_name = table_name
        self.poll_interval = poll_interval
        self.dynamodb = boto3.client('dynamodb')
        self.running = False
        self.last_poll_time = datetime.utcnow()
        
    def start(self):
        """Start the WebSocket simulation thread."""
        print("🚀 Starting WebSocket client simulation...")
        print(f"📡 Connecting to real-time messaging system (Table: {self.table_name})")
        print("💬 Listening for messages... Press Ctrl+C to stop\n")
        
        self.running = True
        self.client_thread = threading.Thread(target=self._poll_messages, daemon=True)
        self.client_thread.start()
        
        try:
            # Keep the main thread alive
            while self.running:
                time.sleep(0.1)
        except KeyboardInterrupt:
            print("\n🛑 Stopping WebSocket client simulation...")
            self.stop()
    
    def stop(self):
        """Stop the WebSocket simulation."""
        self.running = False
        print("✅ WebSocket client simulation stopped.")
    
    def _poll_messages(self):
        """Poll DynamoDB table for new messages since last poll."""
        while self.running:
            try:
                # Query for messages newer than last poll time
                current_time = datetime.utcnow()
                
                # Use scan to get recent messages (in production, consider using GSI with timestamp)
                response = self.dynamodb.scan(
                    TableName=self.table_name,
                    FilterExpression='#timestamp > :last_poll',
                    ExpressionAttributeNames={
                        '#timestamp': 'timestamp'
                    },
                    ExpressionAttributeValues={
                        ':last_poll': {'S': self.last_poll_time.isoformat()}
                    }
                )
                
                messages = response.get('Items', [])
                
                # Sort messages by timestamp
                messages.sort(key=lambda x: x.get('timestamp', {}).get('S', ''))
                
                # Process each new message
                for item in messages:
                    message_id = item.get('id', {}).get('S', 'unknown')
                    timestamp = item.get('timestamp', {}).get('S', '')
                    message = item.get('message', {}).get('S', '')
                    sender = item.get('sender', {}).get('S', '')
                    
                    # Echo message to stdout (simulating WebSocket reception)
                    self._echo_message(timestamp, sender, message, message_id)
                
                # Update last poll time
                self.last_poll_time = current_time
                
                # Wait before next poll
                time.sleep(self.poll_interval)
                
            except Exception as e:
                print(f"❌ Error polling messages: {e}")
                time.sleep(self.poll_interval)
    
    def _echo_message(self, timestamp, sender, message, message_id):
        """Echo a received message to stdout."""
        # Format timestamp for display
        try:
            dt = datetime.fromisoformat(timestamp.replace('Z', '+00:00'))
            formatted_time = dt.strftime('%H:%M:%S')
        except:
            formatted_time = timestamp[:8] if len(timestamp) >= 8 else timestamp
        
        # Color coding for different senders
        color_codes = {
            'api-gateway-get': '\033[94m',      # Blue
            'api-gateway-post': '\033[92m',     # Green
            'echo-processor': '\033[93m',       # Yellow
        }
        reset_color = '\033[0m'
        
        sender_color = color_codes.get(sender, '\033[95m')  # Default magenta
        
        # Print the echoed message
        print(f"[{formatted_time}] {sender_color}{sender}{reset_color}: {message}")
        print(f"   └─ Message ID: {message_id[:8]}...")
        
        # Special handling for echo messages
        if sender == 'echo-processor':
            print("   🔄 Real-time echo received!")
        
        print()  # Empty line for readability


def main():
    """Main function to run the WebSocket client simulation."""
    # You'll need to replace this with your actual DynamoDB table name
    # In a real deployment, this could be passed as environment variable or argument
    table_name = "stlvapp-test-messages"  # This will match the Stelvio naming convention
    
    if len(sys.argv) > 1:
        table_name = sys.argv[1]
    
    print("WebSocket Client Simulation")
    print("==========================")
    print(f"Table: {table_name}")
    print(f"Poll Interval: 2 seconds")
    print()
    
    simulator = WebSocketSimulator(table_name)
    simulator.start()


if __name__ == "__main__":
    main()