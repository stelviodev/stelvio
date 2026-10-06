/* Native validation is independent of the client and runs before host effects. */
#include "protocol.h"
#include <arpa/inet.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

static uint32_t u32(const uint8_t *value) {
    return ((uint32_t)value[0] << 24) | ((uint32_t)value[1] << 16) |
           ((uint32_t)value[2] << 8) | value[3];
}

static bool nonzero(const uint8_t *value, size_t size) {
    uint8_t result = 0;
    for (size_t i = 0; i < size; i++) result |= value[i];
    return result != 0;
}

static bool hex(const char *value) {
    for (; *value; value++)
        if (!((*value >= '0' && *value <= '9') || (*value >= 'a' && *value <= 'f')))
            return false;
    return true;
}

bool stlv_cidr(const char *text, uint32_t *network, uint32_t *mask) {
    const char *slash = strchr(text, '/');
    char address[INET_ADDRSTRLEN], canonical[STLV_CIDR_SIZE];
    if (!slash || slash == text || (size_t)(slash - text) >= sizeof(address)) return false;
    size_t length = (size_t)(slash - text);
    memcpy(address, text, length);
    address[length] = 0;
    char *end;
    long prefix = strtol(slash + 1, &end, 10);
    if (*end || prefix < 16 || prefix > 28) return false;
    struct in_addr ip;
    if (inet_pton(AF_INET, address, &ip) != 1) return false;
    uint32_t value = ntohl(ip.s_addr);
    *mask = UINT32_MAX << (32 - prefix);
    *network = value & *mask;
    uint32_t last = value | ~*mask;
    bool private = (value >= 0x0a000000 && last <= 0x0affffff) ||
                   (value >= 0xac100000 && last <= 0xac1fffff) ||
                   (value >= 0xc0a80000 && last <= 0xc0a8ffff);
    snprintf(canonical, sizeof(canonical), "%s/%ld", address, prefix);
    return private && value == *network && strcmp(text, canonical) == 0;
}

static bool domain(const char *value) {
    size_t length = strlen(value), label = 0;
    struct in_addr ip;
    if (!length || length >= STLV_DOMAIN_SIZE || inet_pton(AF_INET, value, &ip) == 1)
        return false;
    for (size_t i = 0; i < length; i++) {
        char byte = value[i];
        if (byte == '.') {
            if (!label || value[i - 1] == '-') return false;
            label = 0;
            continue;
        }
        if (!((byte >= 'a' && byte <= 'z') || (byte >= '0' && byte <= '9') || byte == '-'))
            return false;
        if ((!label && byte == '-') || ++label > 63) return false;
    }
    return label && value[length - 1] != '-';
}

struct cursor { const uint8_t *data; size_t size, offset; };

static bool text(struct cursor *cursor, char *destination, size_t limit) {
    if (cursor->offset == cursor->size) return false;
    size_t length = cursor->data[cursor->offset++];
    if (!length || length >= limit || length > cursor->size - cursor->offset) return false;
    for (size_t i = 0; i < length; i++) {
        uint8_t byte = cursor->data[cursor->offset + i];
        if (!byte || byte > 127) return false;
    }
    memcpy(destination, cursor->data + cursor->offset, length);
    destination[length] = 0;
    cursor->offset += length;
    return true;
}

static bool configuration(struct cursor *cursor, struct stlv_request *request) {
    if (!text(cursor, request->vpc, sizeof(request->vpc))) return false;
    size_t length = strlen(request->vpc);
    if ((length != 12 && length != 21) || strncmp(request->vpc, "vpc-", 4) ||
        !hex(request->vpc + 4) || cursor->offset == cursor->size) return false;
    request->range_count = cursor->data[cursor->offset++];
    if (!request->range_count || request->range_count > STLV_MAX_RANGES) return false;
    for (uint8_t i = 0; i < request->range_count; i++) {
        struct stlv_range *range = &request->ranges[i];
        if (!text(cursor, range->cidr, sizeof(range->cidr)) ||
            !stlv_cidr(range->cidr, &range->network, &range->mask)) return false;
        for (uint8_t j = 0; j < i; j++) {
            struct stlv_range *other = &request->ranges[j];
            uint32_t common = range->mask & other->mask;
            if ((range->network & common) == (other->network & common)) return false;
        }
    }
    if (cursor->offset == cursor->size) return false;
    request->resolver_count = cursor->data[cursor->offset++];
    if (request->resolver_count > STLV_MAX_DOMAINS) return false;
    for (uint8_t i = 0; i < request->resolver_count; i++) {
        struct stlv_resolver *resolver = &request->resolvers[i];
        if (!text(cursor, resolver->domain, sizeof(resolver->domain)) ||
            !domain(resolver->domain) || cursor->size - cursor->offset < 2) return false;
        resolver->port = (uint16_t)((cursor->data[cursor->offset] << 8) |
                                   cursor->data[cursor->offset + 1]);
        cursor->offset += 2;
        if (resolver->port < 1024) return false;
        for (uint8_t j = 0; j < i; j++)
            if (!strcmp(resolver->domain, request->resolvers[j].domain)) return false;
    }
    return true;
}

bool stlv_decode(const uint8_t *packet, size_t size, struct stlv_request *request) {
    memset(request, 0, sizeof(*request));
    if (size < STLV_HEADER || size > STLV_HEADER + STLV_MAX_BODY ||
        memcmp(packet, "STLVTUN1", 8) || packet[10] || packet[11] ||
        u32(packet + 12) != size - STLV_HEADER) return false;
    request->operation = packet[8];
    if (request->operation < STLV_INSPECT || request->operation > STLV_RECONCILE ||
        packet[9] > 1 || (packet[9] && request->operation != STLV_REMOVE)) return false;
    request->keep_dns = packet[9] != 0;
    memcpy(request->session, packet + 16, 16);
    memcpy(request->capability, packet + 32, 16);
    request->generation = ((uint64_t)u32(packet + 48) << 32) | u32(packet + 52);
    bool anonymous = request->operation == STLV_INSPECT || request->operation == STLV_RECONCILE;
    bool acquiring = request->operation == STLV_ACQUIRE;
    bool scoped = request->operation == STLV_CONFIGURE || request->operation == STLV_REMOVE;
    if (nonzero(request->session, 16) == anonymous ||
        nonzero(request->capability, 16) != (!anonymous && !acquiring) ||
        (request->generation != 0) != scoped) return false;
    struct cursor cursor = {.data=packet + STLV_HEADER, .size=size - STLV_HEADER};
    if (scoped) {
        if (cursor.size < 4 || !(request->unit = u32(cursor.data))) return false;
        cursor.offset = 4;
    }
    if (request->operation == STLV_CONFIGURE && !configuration(&cursor, request)) return false;
    return cursor.offset == cursor.size;
}
