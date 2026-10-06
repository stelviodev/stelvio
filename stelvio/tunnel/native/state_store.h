#ifndef STELVIO_TUNNEL_STATE_STORE_H
#define STELVIO_TUNNEL_STATE_STORE_H

#include "snapshot.h"

/* These are the service's typed persistence boundary. Exclusion locks must
 * remain held from load through host effects and the corresponding save. */
int stlv_state_load(int image, int lease, struct stlv_snapshot *state);
int stlv_state_save(int image, int lease, const struct stlv_snapshot *state);

#endif
