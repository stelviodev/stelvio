#ifndef STELVIO_TUNNEL_IO_H
#define STELVIO_TUNNEL_IO_H

#include "protocol.h"
#include <sys/socket.h>

#define STLV_MAX_REPLY 4096
#define STLV_CONTROL_BYTES 4096

bool stlv_io_supported(void);
/* Close every copied incoming rights descriptor; return false for any control.
 * Call after successful recvmsg (including an empty datagram), using its actual
 * initialized control buffer/capacity. Never call on a failed syscall. */
bool stlv_ancillary_free(const struct msghdr *message, const void *control, size_t capacity);

/* A single complete independently validated frame, with a ten-second budget.
 * Caller-supplied descriptors are closed and rejected, never adopted. The
 * service closes a transaction connection afterwards; an acquire connection
 * may be retained solely as an EOF lease, never read for another transaction. */
bool stlv_receive(int connection, struct stlv_request *request);
/* Incremental service input: one nonblocking receive per call, preserving the
 * exact validated frame for durable CONFIGURE intent. Caller enforces deadline.
 * Return 1 complete, 0 incomplete/EAGAIN, -1 invalid/EOF/error. */
struct stlv_input {
    size_t size, target;
    uint8_t packet[STLV_HEADER + STLV_MAX_BODY];
};
int stlv_receive_step(int connection, struct stlv_input *input, struct stlv_request *request);
/* Descriptor delivery is permitted only for the acquire reply. The service
 * chooses the payload/descriptor, never a caller-selected descriptor or path. */
bool stlv_reply(int connection, uint16_t status, const void *payload, size_t size, int descriptor);

/* Service replies are small and use one nonblocking send. Partial delivery
 * fails closed; caller closes the socket/revokes an uncertain acquisition. */
bool stlv_reply_once(int connection, uint16_t status, const void *payload, size_t size, int descriptor);
#endif
