#ifndef STELVIO_TUNNEL_UNIT_H
#define STELVIO_TUNNEL_UNIT_H
#include "interface.h"
#include "snapshot.h"

/* Internal service transactions, never a second authorization boundary. Caller
 * authenticates the request's kernel peer and owns the global image/lease locks
 * throughout. All kernel descriptors stay exclusively inside the root service.
 * On failure reload committed state; retained PREPARING/REMOVING is recoverable. */
int stlv_unit_configure(int image, int lease, struct stlv_snapshot *state,
                        struct stlv_interface interfaces[STLV_MAX_UNITS],
                        const uint8_t *packet, size_t size, uint8_t *failure_stage);
int stlv_unit_remove(int image, int lease, struct stlv_snapshot *state,
                     struct stlv_interface interfaces[STLV_MAX_UNITS],
                     uint32_t unit, uint64_t generation, bool keep_dns);
#endif
