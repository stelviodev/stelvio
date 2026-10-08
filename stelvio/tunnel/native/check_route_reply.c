/* Nonprivileged socket fixtures for the production routing ACK receiver. */
#include "route_reply.h"
#include <errno.h>
#include <net/if.h>
#include <net/route.h>
#include <string.h>
#include <sys/socket.h>
#include <unistd.h>

int main(int argc, char **argv) {
    if (argc != 2) return 2;
    int pair[2];
    if (socketpair(AF_UNIX, SOCK_DGRAM, 0, pair)) return 2;
    struct rt_msghdr ack = {.rtm_msglen=sizeof(ack), .rtm_version=RTM_VERSION,
                           .rtm_type=RTM_ADD, .rtm_pid=getpid(), .rtm_seq=1};
    if (!strcmp(argv[1], "notifications")) {
        struct ifa_msghdr address = {.ifam_msglen=sizeof(address),
                                   .ifam_version=RTM_VERSION, .ifam_type=RTM_NEWADDR};
        struct ifma_msghdr multicast = {.ifmam_msglen=sizeof(multicast),
                                      .ifmam_version=RTM_VERSION, .ifmam_type=RTM_NEWMADDR};
        if (send(pair[0], &address, sizeof(address), 0) != sizeof(address) ||
            send(pair[0], &multicast, sizeof(multicast), 0) != sizeof(multicast)) return 2;
    } else if (!strcmp(argv[1], "foreign")) {
        struct rt_msghdr foreign = ack;
        foreign.rtm_pid++;
        foreign.rtm_errno = EEXIST;
        if (send(pair[0], &foreign, sizeof(foreign), 0) != sizeof(foreign)) return 2;
        foreign = ack;
        foreign.rtm_seq++;
        if (send(pair[0], &foreign, sizeof(foreign), 0) != sizeof(foreign)) return 2;
    } else if (!strcmp(argv[1], "length")) ack.rtm_msglen--;
    else if (!strcmp(argv[1], "version")) ack.rtm_version++;
    else if (!strcmp(argv[1], "error")) ack.rtm_errno = EEXIST;
    else if (!strcmp(argv[1], "foreign-only")) ack.rtm_pid++;
    else if (!strcmp(argv[1], "sequence-only")) ack.rtm_seq++;
    else if (!strcmp(argv[1], "short-add")) ack.rtm_msglen = 4;
    else if (!strcmp(argv[1], "truncated")) {
        if (send(pair[0], &ack, 3, 0) != 3) return 2;
    } else if (strcmp(argv[1], "missing") && strcmp(argv[1], "success")) return 2;
    if (strcmp(argv[1], "missing") && strcmp(argv[1], "truncated") &&
        send(pair[0], &ack, ack.rtm_msglen == 4 ? 4 : sizeof(ack), 0) < 0) return 2;
    bool accepted = stlv_route_add_reply(pair[1], getpid(), 1);
    if (close(pair[0]) || close(pair[1])) return 2;
    return accepted ? 0 : 1;
}
