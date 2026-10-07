#ifndef STELVIO_TUNNEL_SNAPSHOT_H
#define STELVIO_TUNNEL_SNAPSHOT_H

#include "ownership.h"
#include "protocol.h"

enum stlv_phase {
    STLV_PREPARING = 1, STLV_ACTIVE, STLV_REMOVING, STLV_RETAINED, STLV_REMOVED
};

/* A receipt precedes publishing a staged inode into the resolver directory.
 * The public name is derived exclusively from the validated configuration.
 * Persisted interface indices are diagnostic data, never route ownership. */
struct stlv_file_receipt {
    uint8_t phase; /* 0 absent, 1 staged, 2 linked */
    uint64_t device, inode;
};

struct stlv_unit_snapshot {
    uint8_t phase;
    uint64_t generation;
    uint32_t interface_index; /* Historical diagnostics only after FD loss. */
    size_t packet_size;
    uint8_t packet[STLV_HEADER + STLV_MAX_BODY];
    struct stlv_request configuration;
    struct stlv_file_receipt files[STLV_MAX_DOMAINS];
};

struct stlv_snapshot {
    uint64_t revision;
    struct stlv_peer peer;
    uint8_t session[16], capability[16];
    uint32_t carrier_version;
    uint8_t unit_count;
    struct stlv_unit_snapshot units[STLV_MAX_UNITS];
};

/* Explicit endian encoding: never persist C padding, pointers or FD numbers.
 * Both paths validate the complete schema and cross-unit ownership constraints.
 * Decoding failure zeros the entire destination, preventing partial adoption. */
bool stlv_snapshot_decode(const uint8_t *bytes, size_t size, struct stlv_snapshot *state);
bool stlv_snapshot_encode(const struct stlv_snapshot *state, uint8_t *bytes,
                          size_t capacity, size_t *size);
/* One durable transition changes at most one unit. Existing slots are retained
 * as tombstones, and only a higher generation may begin another configuration. */
bool stlv_snapshot_successor(const struct stlv_snapshot *before,
                             const struct stlv_snapshot *after);

#endif
