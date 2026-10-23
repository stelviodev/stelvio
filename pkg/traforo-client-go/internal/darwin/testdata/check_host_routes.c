/* Nonprivileged syscall-boundary fixtures; never inspect/change host routes. */
#include "bindings.h"
#include <arpa/inet.h>
#include <errno.h>
#include <net/route.h>
#include <string.h>
#include <sys/sysctl.h>

static const char *scenario;
static unsigned copies;
static struct {
    struct rt_msghdr2 header;
    struct sockaddr_in destination;
    struct sockaddr_in mask;
} route;

int sysctl(int *name, unsigned count, void *old, size_t *size, void *new, size_t new_size) {
    if (count != 6 || name[0] != CTL_NET || name[1] != PF_ROUTE ||
        name[3] != AF_INET || name[4] != NET_RT_DUMP2 || new || new_size) {
        errno = EINVAL;
        return -1;
    }
    if (!old) {
        *size = !strcmp(scenario, "oversized") ? 16 * 1024 * 1024 + 1 : sizeof(route);
        return 0;
    }
    copies++;
    if (*size < sizeof(route)) { errno = ENOMEM; return -1; }
    memcpy(old, &route, sizeof(route));
    *size = sizeof(route);
    if ((!strcmp(scenario, "growth") || !strcmp(scenario, "growth-foreign") ||
         !strcmp(scenario, "growth-malformed")) && copies == 1) {
        /* Even a plausible partial buffer must never be parsed after failure. */
        errno = ENOMEM;
        return -1;
    }
    if (!strcmp(scenario, "always-growth")) { errno = ENOMEM; return -1; }
    if (!strcmp(scenario, "permission")) { errno = EPERM; return -1; }
    return 0;
}

int main(int argc, char **argv) {
    if (argc != 2) return 3;
    scenario = argv[1];
    route.header.rtm_msglen = sizeof(route);
    route.header.rtm_version = RTM_VERSION;
    route.header.rtm_type = RTM_GET2;
    route.header.rtm_index = !strcmp(scenario, "growth-foreign") ? 43 : 42;
    route.header.rtm_addrs = RTA_DST | RTA_NETMASK;
    route.destination.sin_len = sizeof(route.destination);
    route.destination.sin_family = AF_INET;
    route.destination.sin_addr.s_addr = htonl(0x0afe0000);
    route.mask.sin_len = sizeof(route.mask);
    route.mask.sin_addr.s_addr = htonl(0xffff0000);
    if (!strcmp(scenario, "growth-malformed")) route.header.rtm_msglen--;
    tf_route entries[8]; size_t count = 0;
    int loaded = tf_routes(entries, 8, &count);
    int result = loaded < 0 ? -1 :
        count == 1 && entries[0].network == 0x0afe0000 && entries[0].mask == 0xffff0000 ?
        (entries[0].index == 42 ? 1 : 2) : 0;
    if (copies > 4 || (!strcmp(scenario, "permission") && copies != 1)) return 3;
    if (result == 1) return 0;
    if (result == 2) return 1;
    return result == -1 ? 2 : 3;
}
