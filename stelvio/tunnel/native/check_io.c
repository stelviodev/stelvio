/* Nonroot harness for real framed IO and descriptor rejection; no host changes. */
#include "io.h"
#include "packet_io.h"
#include <fcntl.h>
#include <inttypes.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <poll.h>
#include <sys/resource.h>
#include <unistd.h>

static unsigned count_fds(void) {
    unsigned count = 0;
    for (int descriptor = 0; descriptor < 1024; descriptor++)
        if (fcntl(descriptor, F_GETFD) >= 0) count++;
    return count;
}

int main(int argc, char **argv) {
    bool datagram = argc == 3 && !strcmp(argv[2], "--packet");
    if (geteuid() == 0 || (argc != 2 && !datagram)) return 1;
    char *end;
    long connection = strtol(argv[1], &end, 10);
    if (*end || connection < 3 || connection >= 1024) return 1;
    struct rlimit limit;
    if (getrlimit(RLIMIT_NOFILE, &limit) || limit.rlim_max < 1024) return 1;
    limit.rlim_cur = 1024;
    if (setrlimit(RLIMIT_NOFILE, &limit)) return 1;
    int sentinel = open("/dev/null", O_RDONLY | O_CLOEXEC);
    if (sentinel < 0) return 1;
    unsigned before = count_fds();
    puts("READY");
    fflush(stdout);
    struct stlv_request request;
    struct stlv_packet_frame frame = {0};
    bool valid;
    if (datagram) {
        struct pollfd ready = {.fd=(int)connection, .events=POLLIN};
        valid = poll(&ready, 1, 5000) > 0 && (ready.revents & POLLIN) &&
                stlv_packet_receive((int)connection, &frame) == 1;
    } else valid = stlv_receive((int)connection, &request);
    unsigned after = count_fds();
    bool intact = fcntl(sentinel, F_GETFD) >= 0 && fcntl((int)connection, F_GETFD) >= 0;
    printf("{\"valid\":%s,\"before\":%u,\"after\":%u,\"sentinels_intact\":%s",
           valid ? "true" : "false", before, after, intact ? "true" : "false");
    if (datagram) {
        printf(",\"unit\":%u,\"generation\":%" PRIu64 ",\"packet\":\"", frame.unit, frame.generation);
        for (size_t i = 0; i < frame.size; i++) printf("%02x", frame.packet[i]);
        putchar('"');
    }
    puts("}");
    close(sentinel);
    close((int)connection);
    return 0;
}
