import socket

# A public IP so no DNS is involved. Inside a VPC without NAT the connect times out.
PROBE = ("1.1.1.1", 443)
TIMEOUT = 2


def main(event, context):
    try:
        socket.create_connection(PROBE, timeout=TIMEOUT).close()
    except OSError:
        return {"egress": False}
    return {"egress": True}
