#ifndef STELVIO_TUNNEL_RESOLVER_INVENTORY_H
#define STELVIO_TUNNEL_RESOLVER_INVENTORY_H
#include "protocol.h"
/* SystemConfiguration supplemental/service DNS domains. 0 no overlap,
 * 1 conflict, -1 unreadable/ambiguous. Recheck before granting readiness. */
int stlv_system_dns_conflict(const struct stlv_request *configuration);
#endif
