/* Read-only diagnostic harness for the same parser used by the root helper. */
#include "protocol.h"
#include <stdio.h>
#include <inttypes.h>
#include <string.h>

int main(void) {
    uint8_t packet[STLV_HEADER + STLV_MAX_BODY + 1];
    size_t size = fread(packet, 1, sizeof(packet), stdin);
    struct stlv_request request;
    if (ferror(stdin) || !stlv_decode(packet, size, &request)) {
        fputs("invalid native helper request\n", stderr);
        return 1;
    }
    /* Compare the public deterministic fixture capability without printing any
     * received capability. This diagnostic is not the service's authentication. */
    uint8_t expected[16] = {0};
    if (request.operation == STLV_CONFIGURE || request.operation == STLV_REMOVE ||
        request.operation == STLV_RELEASE) memcpy(expected, "opaque-lease-key", 16);
    printf("{\"operation\":%u,\"unit\":%u,\"generation\":%" PRIu64
           ",\"keep_dns\":%s,\"vpc\":\"%s\",\"session\":\"",
           request.operation, request.unit, request.generation,
           request.keep_dns ? "true" : "false", request.vpc);
    for (size_t i = 0; i < 16; i++) printf("%02x", request.session[i]);
    printf("\",\"capability_matches_fixture\":%s,\"ranges\":[",
           memcmp(expected, request.capability, 16) ? "false" : "true");
    for (size_t i = 0; i < request.range_count; i++) {
        struct stlv_range *range = &request.ranges[i];
        printf("%s{\"cidr\":\"%s\",\"network\":%u,\"mask\":%u}",
               i ? "," : "", range->cidr, range->network, range->mask);
    }
    fputs("],\"domains\":[", stdout);
    for (size_t i = 0; i < request.resolver_count; i++)
        printf("%s{\"domain\":\"%s\",\"port\":%u}", i ? "," : "",
               request.resolvers[i].domain, request.resolvers[i].port);
    puts("]}");
    return 0;
}
