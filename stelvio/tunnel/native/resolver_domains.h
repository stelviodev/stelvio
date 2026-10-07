#ifndef STELVIO_TUNNEL_RESOLVER_DOMAINS_H
#define STELVIO_TUNNEL_RESOLVER_DOMAINS_H
#include "protocol.h"
#define STLV_RESOLVER_BYTES 8192
bool stlv_dns_domain(const char *input, size_t size, char output[STLV_DOMAIN_SIZE]);
bool stlv_dns_overlap(const char *a, const char *b);
/* Read a bounded resolver(5) file. Explicit domain overrides the filename.
 * Reject ambiguous or malformed contents rather than assuming no conflict. */
bool stlv_resolver_domain(const char *name, const char *bytes, size_t size,
                           char output[STLV_DOMAIN_SIZE]);
#endif
