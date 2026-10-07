#ifndef STELVIO_TUNNEL_PACKET_H
#define STELVIO_TUNNEL_PACKET_H

#include "protocol.h"

#define STLV_MAX_PACKET (4 + 65535)

/* Validate option-free TCP/IPv4, Darwin AF framing and unit address direction.
 * This parses no TCP state and owns no AWS/transport credentials. A future
 * revocable Unix packet endpoint must leave every kernel utun FD solely in the
 * native helper; this validator is not a completed packet pump/proof. */
bool stlv_packet_allowed(const uint8_t *packet, size_t size,
                         const struct stlv_request *configuration,
                         uint32_t local_address, bool from_host);

#endif
