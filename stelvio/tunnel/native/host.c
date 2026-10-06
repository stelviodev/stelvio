/* macOS kernel route inspection shared by admission and identity-fenced cleanup. */
#include "host.h"
#include <arpa/inet.h>
#include <net/route.h>
#include <stdlib.h>
#include <string.h>
#include <sys/sysctl.h>

#define MAX_TABLE (16 * 1024 * 1024)

struct route_entry {
    uint32_t destination, mask;
    unsigned interface;
    bool ipv4;
};

static bool decode(const struct rt_msghdr2 *message, struct route_entry *entry) {
    memset(entry, 0, sizeof(*entry));
    entry->interface = message->rtm_index;
    const uint8_t *bytes = (const uint8_t *)message;
    size_t offset = sizeof(*message), size = message->rtm_msglen;
    for (unsigned index = 0; index < RTAX_MAX; index++) {
        if (!(message->rtm_addrs & (1 << index))) continue;
        if (size - offset < 2) return false;
        const struct sockaddr *address = (const struct sockaddr *)(bytes + offset);
        size_t length = address->sa_len;
        size_t padded = length ? (length + sizeof(uint32_t) - 1) & ~(sizeof(uint32_t) - 1) : sizeof(uint32_t);
        if (padded > size - offset || (length && length < 2)) return false;
        struct sockaddr_in ip = {0};
        memcpy(&ip, address, length < sizeof(ip) ? length : sizeof(ip));
        if (index == RTAX_DST && address->sa_family == AF_INET) {
            if (length < offsetof(struct sockaddr_in, sin_addr) + sizeof(ip.sin_addr)) return false;
            entry->ipv4 = true;
            entry->destination = ntohl(ip.sin_addr.s_addr);
        }
        if (index == RTAX_NETMASK) entry->mask = ntohl(ip.sin_addr.s_addr);
        offset += padded;
    }
    if (message->rtm_flags & RTF_HOST) entry->mask = UINT32_MAX;
    return true;
}

static int inspect(uint32_t network, uint32_t mask, unsigned owned_interface, bool exact) {
    int mib[] = {CTL_NET, PF_ROUTE, 0, AF_INET, NET_RT_DUMP2, 0};
    size_t size = 0;
    if (sysctl(mib, 6, NULL, &size, NULL, 0) || size > MAX_TABLE) return STLV_ROUTE_ERROR;
    if (!size) return STLV_ROUTE_ABSENT;
    uint8_t *table = malloc(size);
    if (!table) return STLV_ROUTE_ERROR;
    if (sysctl(mib, 6, table, &size, NULL, 0)) { free(table); return STLV_ROUTE_ERROR; }
    int result = STLV_ROUTE_ABSENT;
    for (size_t offset = 0; offset < size;) {
        if (size - offset < sizeof(struct rt_msghdr2)) { result = STLV_ROUTE_ERROR; break; }
        const struct rt_msghdr2 *message = (const struct rt_msghdr2 *)(table + offset);
        if (message->rtm_msglen < sizeof(*message) || message->rtm_msglen > size - offset ||
            message->rtm_version != RTM_VERSION) { result = STLV_ROUTE_ERROR; break; }
        struct route_entry route;
        if (!decode(message, &route)) { result = STLV_ROUTE_ERROR; break; }
        if (route.ipv4 && route.mask) {
            uint32_t common = mask & route.mask;
            bool match = exact ? (network == route.destination && mask == route.mask) :
                ((network & common) == (route.destination & common));
            if (match) {
                bool owned = owned_interface && route.interface == owned_interface;
                if (exact) {
                    /* Multiple exact entries, or a foreign replacement, never
                     * certify a route as this journal's deletable route. */
                    if (result != STLV_ROUTE_ABSENT || !owned) {
                        result = STLV_ROUTE_FOREIGN;
                        break;
                    }
                    result = STLV_ROUTE_OWNED;
                } else if (!owned) {
                    result = STLV_ROUTE_FOREIGN;
                    break;
                }
            }
        }
        offset += message->rtm_msglen;
    }
    free(table);
    return result;
}

int stlv_route_conflict(uint32_t network, uint32_t mask, unsigned owned_interface) {
    int result = inspect(network, mask, owned_interface, false);
    return result == STLV_ROUTE_ERROR ? -1 : result != STLV_ROUTE_ABSENT;
}

enum stlv_route_status stlv_route_status(const struct stlv_range *range, unsigned owned_interface) {
    return inspect(range->network, range->mask, owned_interface, true);
}
