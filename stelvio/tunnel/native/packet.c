#include "packet.h"
#include <netinet/in.h>
#include <sys/socket.h>

static uint32_t word(const uint8_t *bytes) {
    return ((uint32_t)bytes[0] << 24) | ((uint32_t)bytes[1] << 16) |
           ((uint32_t)bytes[2] << 8) | bytes[3];
}

bool stlv_packet_allowed(const uint8_t *packet, size_t size,
                         const struct stlv_request *configuration,
                         uint32_t local_address, bool from_host) {
    if (!packet || !configuration || configuration->operation != STLV_CONFIGURE ||
        !configuration->range_count || configuration->range_count > STLV_MAX_RANGES ||
        !local_address || size < 24 || size > STLV_MAX_PACKET || word(packet) != AF_INET) return false;
    const uint8_t *ip = packet + 4;
    size_t header = (ip[0] & 15) * 4;
    size_t total = ((uint16_t)ip[2] << 8) | ip[3];
    /* Exclude IP options, including source routing. TCP options stay intact. */
    if ((ip[0] >> 4) != 4 || header != 20 || header > total ||
        total != size - 4 || ip[9] != IPPROTO_TCP) return false;
    uint32_t source = word(ip + 12), destination = word(ip + 16);
    if ((from_host ? source : destination) != local_address) return false;
    uint32_t remote = from_host ? destination : source;
    bool matched = false;
    for (uint8_t i = 0; i < configuration->range_count; i++) {
        const struct stlv_range *range = &configuration->ranges[i];
        uint32_t network, mask;
        /* Never rely on a caller-supplied cached mask to enlarge the grant. */
        if (!stlv_cidr(range->cidr, &network, &mask) ||
            network != range->network || mask != range->mask) return false;
        if ((remote & mask) == network) matched = true;
    }
    return matched;
}
