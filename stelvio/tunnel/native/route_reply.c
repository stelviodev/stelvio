#include "route_reply.h"
#include <errno.h>
#include <net/route.h>
#include <poll.h>
#include <stdint.h>
#include <string.h>
#include <sys/socket.h>
#include <time.h>

static uint64_t milliseconds(void) {
    struct timespec now;
    if (clock_gettime(CLOCK_MONOTONIC, &now)) return 0;
    return (uint64_t)now.tv_sec * 1000 + (uint64_t)now.tv_nsec / 1000000;
}

bool stlv_route_add_reply(int descriptor, pid_t pid, int sequence) {
    uint64_t began = milliseconds();
    if (!began) return false;
    while (true) {
        uint64_t now = milliseconds();
        if (!now || now >= began + 3000) return false;
        struct pollfd wait = {.fd=descriptor, .events=POLLIN};
        int ready = poll(&wait, 1, (int)(began + 3000 - now));
        if (ready < 0 && errno == EINTR) continue;
        if (ready <= 0 || !(wait.revents & POLLIN)) return false;
        uint8_t reply[4096];
        ssize_t count = recv(descriptor, reply, sizeof(reply), MSG_DONTWAIT);
        if (count < 0 && (errno == EINTR || errno == EAGAIN)) continue;
        /* Interface/address notifications have a shorter header than RTM_ADD.
         * Validate their common envelope before selecting the ADD response. */
        if (count < 4) return false;
        uint16_t length;
        memcpy(&length, reply, sizeof(length));
        if (length != count || reply[2] != RTM_VERSION) return false;
        if (reply[3] != RTM_ADD) continue;
        if (count < (ssize_t)sizeof(struct rt_msghdr)) return false;
        struct rt_msghdr header;
        memcpy(&header, reply, sizeof(header));
        if (header.rtm_pid != pid || header.rtm_seq != sequence) continue;
        return !header.rtm_errno;
    }
}
