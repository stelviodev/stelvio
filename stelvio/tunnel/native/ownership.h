#ifndef STELVIO_TUNNEL_OWNERSHIP_H
#define STELVIO_TUNNEL_OWNERSHIP_H

#include <stdbool.h>
#include <stdint.h>

#define STLV_INSTALL "/Library/PrivilegedHelperTools/dev.stelvio.tunnel"
#define STLV_STATE "/private/var/run/stelvio-tunnel"
#define STLV_SOCKET STLV_STATE "/helper.sock"
#define STLV_LEASE STLV_STATE "/lease"
#define STLV_JOURNAL STLV_STATE "/journal"

struct stlv_peer {
    uint32_t uid;
    int32_t pid;
    uint64_t birth_seconds, birth_microseconds;
};

bool stlv_trusted_image(void);
bool stlv_peer_read(int connection, struct stlv_peer *peer);
bool stlv_peer_alive(const struct stlv_peer *peer);
/* Hold both installation and state locks through a session/admin transaction. */
int stlv_image_lock(void);
int stlv_state_lock(void);

#endif
