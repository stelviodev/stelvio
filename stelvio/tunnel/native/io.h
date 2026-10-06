#ifndef STELVIO_TUNNEL_IO_H
#define STELVIO_TUNNEL_IO_H

#include "protocol.h"

#define STLV_MAX_REPLY 4096

bool stlv_io_supported(void);

/* A single complete independently validated frame, with a ten-second budget.
 * Caller-supplied descriptors are closed and rejected, never adopted. The
 * service closes a transaction connection afterwards; an acquire connection
 * may be retained solely as an EOF lease, never read for another transaction. */
bool stlv_receive(int connection, struct stlv_request *request);
/* Descriptor delivery is permitted only for the acquire reply. The service
 * chooses the payload/descriptor, never a caller-selected descriptor or path. */
bool stlv_reply(int connection, uint16_t status, const void *payload, size_t size, int descriptor);

#endif
