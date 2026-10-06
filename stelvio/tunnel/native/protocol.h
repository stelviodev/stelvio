#ifndef STELVIO_TUNNEL_PROTOCOL_H
#define STELVIO_TUNNEL_PROTOCOL_H

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#define STLV_HEADER 56
#define STLV_MAX_BODY 32768
#define STLV_MAX_RANGES 8
#define STLV_MAX_DOMAINS 64
#define STLV_MAX_UNITS 8
#define STLV_CIDR_SIZE 20
#define STLV_DOMAIN_SIZE 254

enum stlv_operation {
    STLV_INSPECT = 1, STLV_ACQUIRE, STLV_CONFIGURE,
    STLV_REMOVE, STLV_RELEASE, STLV_RECONCILE
};

struct stlv_range {
    char cidr[STLV_CIDR_SIZE];
    uint32_t network, mask;
};

struct stlv_resolver {
    char domain[STLV_DOMAIN_SIZE];
    uint16_t port;
};

struct stlv_request {
    uint8_t operation;
    bool keep_dns;
    uint8_t session[16], capability[16];
    uint64_t generation;
    uint32_t unit;
    char vpc[22];
    uint8_t range_count, resolver_count;
    struct stlv_range ranges[STLV_MAX_RANGES];
    struct stlv_resolver resolvers[STLV_MAX_DOMAINS];
};

/* A rejected packet never permits host changes. No string points into caller memory. */
bool stlv_decode(const uint8_t *packet, size_t size, struct stlv_request *request);
bool stlv_cidr(const char *text, uint32_t *network, uint32_t *mask);

#endif
