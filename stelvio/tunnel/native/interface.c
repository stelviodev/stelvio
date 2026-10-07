#include "interface.h"
#include "host.h"
#include "io.h"
#include "ownership.h"
#include <arpa/inet.h>
#include <errno.h>
#include <fcntl.h>
#include <net/if_dl.h>
#include <net/route.h>
#include <poll.h>
#include <string.h>
#include <sys/ioctl.h>
#include <sys/kern_control.h>
#include <sys/socket.h>
#include <sys/sys_domain.h>
#include <time.h>
#include <unistd.h>

#define UTUN_NAME 2

bool stlv_interface_current(const struct stlv_interface *interface) {
    if (!interface || interface->descriptor < 0 || !interface->index) return false;
    char name[IFNAMSIZ] = {0};
    socklen_t length = sizeof(name);
    return !getsockopt(interface->descriptor, SYSPROTO_CONTROL, UTUN_NAME, name, &length) &&
        length > 1 && length <= sizeof(name) && !name[length - 1] &&
        !strcmp(name, interface->name) && if_nametoindex(name) == interface->index;
}

void stlv_interface_close(struct stlv_interface *interface) {
    if (!interface) return;
    if (interface->descriptor >= 0) close(interface->descriptor);
    memset(interface, 0, sizeof(*interface));
    interface->descriptor = -1;
}

bool stlv_interface_open(struct stlv_interface *interface) {
    if (!interface || interface->descriptor >= 0 || !stlv_trusted_image() || !stlv_io_supported()) return false;
    memset(interface, 0, sizeof(*interface));
    interface->descriptor = -1;
    int descriptor = socket(PF_SYSTEM, SOCK_DGRAM, SYSPROTO_CONTROL);
    if (descriptor < 0) return false;
    interface->descriptor = descriptor;
    int no_sigpipe = 1, flags = fcntl(descriptor, F_GETFL);
    if (flags < 0 || fcntl(descriptor, F_SETFL, flags | O_NONBLOCK) ||
        setsockopt(descriptor, SOL_SOCKET, SO_NOSIGPIPE, &no_sigpipe, sizeof(no_sigpipe))) goto failed;
    struct ctl_info control = {0};
    strlcpy(control.ctl_name, "com.apple.net.utun_control", sizeof(control.ctl_name));
    struct sockaddr_ctl address = {.sc_len=sizeof(address), .sc_family=AF_SYSTEM,
                                  .ss_sysaddr=AF_SYS_CONTROL, .sc_unit=0};
    if (fcntl(descriptor, F_SETFD, FD_CLOEXEC) || ioctl(descriptor, CTLIOCGINFO, &control)) goto failed;
    address.sc_id = control.ctl_id;
    if (connect(descriptor, (struct sockaddr *)&address, sizeof(address))) goto failed;
    socklen_t length = sizeof(interface->name);
    if (getsockopt(descriptor, SYSPROTO_CONTROL, UTUN_NAME, interface->name, &length) ||
        length < 2 || length > sizeof(interface->name) || interface->name[length - 1] ||
        strncmp(interface->name, "utun", 4)) goto failed;
    interface->index = if_nametoindex(interface->name);
    if (!stlv_interface_current(interface)) goto failed;
    return true;
failed:
    stlv_interface_close(interface);
    return false;
}

static struct sockaddr_in address(uint32_t ip) {
    struct sockaddr_in result = {.sin_len=sizeof(result), .sin_family=AF_INET};
    result.sin_addr.s_addr = htonl(ip);
    return result;
}

bool stlv_interface_configure(const struct stlv_interface *interface, uint8_t slot) {
    if (slot >= STLV_MAX_UNITS) return false;
    uint32_t local_address = 0xc0000201u + 2 * slot, peer_address = local_address + 1;
    if (!stlv_trusted_image() || !stlv_interface_current(interface) ||
        stlv_route_conflict(local_address, UINT32_MAX, interface->index) != 0 ||
        stlv_route_conflict(peer_address, UINT32_MAX, interface->index) != 0) return false;
    int socket_fd = socket(AF_INET, SOCK_DGRAM, 0);
    if (socket_fd < 0) return false;
    struct ifaliasreq alias = {0};
    strlcpy(alias.ifra_name, interface->name, sizeof(alias.ifra_name));
    struct sockaddr_in local = address(local_address), peer = address(peer_address), mask = address(UINT32_MAX);
    memcpy(&alias.ifra_addr, &local, sizeof(local));
    memcpy(&alias.ifra_broadaddr, &peer, sizeof(peer));
    memcpy(&alias.ifra_mask, &mask, sizeof(mask));
    bool result = !ioctl(socket_fd, SIOCAIFADDR, &alias);
    struct ifreq flags = {0};
    strlcpy(flags.ifr_name, interface->name, sizeof(flags.ifr_name));
    if (result) result = !ioctl(socket_fd, SIOCGIFFLAGS, &flags);
    flags.ifr_flags |= IFF_UP;
    if (result) result = !ioctl(socket_fd, SIOCSIFFLAGS, &flags);
    if (close(socket_fd)) result = false;
    return result && stlv_interface_current(interface);
}

static uint64_t milliseconds(void) {
    struct timespec now;
    if (clock_gettime(CLOCK_MONOTONIC, &now)) return 0;
    return (uint64_t)now.tv_sec * 1000 + (uint64_t)now.tv_nsec / 1000000;
}

bool stlv_interface_add_route(const struct stlv_interface *interface, const struct stlv_range *range) {
    /* XNU's unscoped RTM_DELETE selects only destination/mask. The supplied
     * gateway/index is not a deletion precondition, so a concurrent foreign
     * replacement could be removed. Never issue that unsafe operation. The
     * service must use an independently proven interface-detach teardown. */
    uint32_t network, mask;
    if (!range || !stlv_trusted_image() || !stlv_interface_current(interface) ||
        !stlv_cidr(range->cidr, &network, &mask) || network != range->network || mask != range->mask) return false;
    enum stlv_route_status existing = stlv_route_status(range, interface->index);
    if (existing == STLV_ROUTE_ERROR || existing == STLV_ROUTE_FOREIGN) return false;
    if (existing == STLV_ROUTE_OWNED) return true;
    if (stlv_route_conflict(network, mask, interface->index) != 0) return false;
    int descriptor = socket(PF_ROUTE, SOCK_RAW, 0);
    if (descriptor < 0) return false;
    struct {
        struct rt_msghdr header;
        struct sockaddr_in destination;
        struct sockaddr_dl gateway;
        struct sockaddr_in netmask;
    } message = {0};
    message.header.rtm_msglen = sizeof(message);
    message.header.rtm_version = RTM_VERSION;
    message.header.rtm_type = RTM_ADD;
    message.header.rtm_flags = RTF_UP | RTF_STATIC;
    message.header.rtm_addrs = RTA_DST | RTA_GATEWAY | RTA_NETMASK;
    message.header.rtm_index = (unsigned short)interface->index;
    message.header.rtm_pid = getpid();
    message.header.rtm_seq = 1;
    message.destination = address(network);
    message.netmask = address(mask);
    message.gateway.sdl_len = sizeof(message.gateway);
    message.gateway.sdl_family = AF_LINK;
    message.gateway.sdl_index = (unsigned short)interface->index;
    uint64_t began = milliseconds();
    bool result = false;
    if (!began || interface->index > UINT16_MAX ||
        send(descriptor, &message, sizeof(message), MSG_DONTWAIT) != sizeof(message)) goto done;
    while (true) {
        uint64_t now = milliseconds();
        if (!now || now >= began + 3000) break;
        struct pollfd wait = {.fd=descriptor, .events=POLLIN};
        int ready = poll(&wait, 1, (int)(began + 3000 - now));
        if (ready < 0 && errno == EINTR) continue;
        if (ready <= 0 || !(wait.revents & POLLIN)) break;
        uint8_t reply[4096];
        ssize_t count = recv(descriptor, reply, sizeof(reply), MSG_DONTWAIT);
        if (count < 0 && (errno == EINTR || errno == EAGAIN)) continue;
        if (count < (ssize_t)sizeof(struct rt_msghdr)) break;
        struct rt_msghdr header;
        memcpy(&header, reply, sizeof(header));
        if (header.rtm_pid != getpid() || header.rtm_seq != 1) continue;
        result = header.rtm_version == RTM_VERSION && header.rtm_msglen == count &&
                 header.rtm_type == message.header.rtm_type && !header.rtm_errno;
        break;
    }
done:
    if (close(descriptor)) result = false;
    return result && stlv_interface_current(interface) &&
        stlv_route_status(range, interface->index) == STLV_ROUTE_OWNED;
}
