#ifndef STELVIO_TUNNEL_HOST_H
#define STELVIO_TUNNEL_HOST_H

#include "protocol.h"

enum stlv_route_status {
    STLV_ROUTE_ERROR = -1, STLV_ROUTE_ABSENT = 0,
    STLV_ROUTE_OWNED = 1, STLV_ROUTE_FOREIGN = 2
};

/* Read the actual kernel table. A parse/sysctl failure never means no conflict. */
int stlv_route_conflict(uint32_t network, uint32_t mask, unsigned owned_interface);
enum stlv_route_status stlv_route_status(const struct stlv_range *range, unsigned owned_interface);

#endif
