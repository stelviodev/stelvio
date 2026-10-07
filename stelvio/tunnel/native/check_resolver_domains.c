/* Read-only DNS inventory/parser harness; no resolver modifications. */
#include "resolver_domains.h"
#include "resolver_inventory.h"
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

int main(int argc, char **argv) {
    if (argc == 2 && !strcmp(argv[1], "--system")) {
        uint8_t bytes[STLV_HEADER + STLV_MAX_BODY + 1];
        struct stlv_request request;
        size_t size = fread(bytes, 1, sizeof(bytes), stdin);
        if (ferror(stdin) || !stlv_decode(bytes, size, &request)) return 2;
        int conflict = stlv_system_dns_conflict(&request);
        return conflict < 0 ? 2 : conflict;
    }
    if (argc == 4 && !strcmp(argv[1], "--overlap")) {
        char first[STLV_DOMAIN_SIZE], second[STLV_DOMAIN_SIZE];
        if (!stlv_dns_domain(argv[2], strlen(argv[2]), first) ||
            !stlv_dns_domain(argv[3], strlen(argv[3]), second)) return 2;
        return stlv_dns_overlap(first, second) ? 0 : 1;
    }
    if (argc != 2) return 2;
    char bytes[STLV_RESOLVER_BYTES + 1], domain[STLV_DOMAIN_SIZE];
    size_t count = fread(bytes, 1, sizeof(bytes), stdin);
    memset(domain, 0xa5, sizeof(domain));
    if (ferror(stdin) || !stlv_resolver_domain(argv[1], bytes, count, domain)) {
        for (size_t i = 0; i < sizeof(domain); i++) if (domain[i]) return 2;
        return 1;
    }
    return printf("%s\n", domain) > 0 && !fflush(stdout) ? 0 : 2;
}
