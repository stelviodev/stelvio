#ifndef STELVIO_TUNNEL_RESOLVER_PLAN_H
#define STELVIO_TUNNEL_RESOLVER_PLAN_H

#include "snapshot.h"

struct stlv_resolver_spec {
    char private_name[128]; /* Contains lease capability: never report/log. */
    char public_name[96];
    char contents[512];
    size_t size;
};

/* Derive names exclusively from bounded authenticated state. Public files use
 * a session namespace plus an explicit domain directive; never overwrite the
 * customary domain-named files another application may own. No effects here. */
bool stlv_resolver_spec(const struct stlv_snapshot *state, uint8_t unit,
                        uint8_t domain, struct stlv_resolver_spec *spec);

#endif
