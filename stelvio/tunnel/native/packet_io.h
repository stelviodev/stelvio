#ifndef STELVIO_TUNNEL_PACKET_IO_H
#define STELVIO_TUNNEL_PACKET_IO_H

#include "packet.h"

#define STLV_PACKET_HEADER 12
#define STLV_MAX_PACKET_FRAME (STLV_PACKET_HEADER + STLV_MAX_PACKET)

struct stlv_packet_frame {
    uint32_t unit;
    uint64_t generation;
    size_t size;
    uint8_t packet[STLV_MAX_PACKET];
};

/* Bounded nonblocking datagrams: 1 accepted, 0 no available data, -1 rejected.
 * A lease owns the carrier; per-unit generations fence delayed packets. */
int stlv_packet_receive(int descriptor, struct stlv_packet_frame *frame);
int stlv_packet_send(int descriptor, const struct stlv_packet_frame *frame);
bool stlv_packet_pair(int descriptors[2]);

#endif
