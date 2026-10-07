/* Temporary installed-native mechanism proof, excluded from product assets.
 * No arbitrary input, exec, AWS or resolver operations. Kernel FDs stay here. */
#include "interface.h"
#include "host.h"
#include "io.h"
#include "ownership.h"
#include <fcntl.h>
#include <stdio.h>
#include <string.h>
#include <sys/socket.h>
#include <time.h>
#include <unistd.h>

static bool absent(const char *name, const struct stlv_range *range) {
    for (unsigned attempt = 0; attempt < 300; attempt++) {
        if (!if_nametoindex(name) && stlv_route_status(range, 0) == STLV_ROUTE_ABSENT) return true;
        struct timespec pause = {.tv_nsec=10000000};
        nanosleep(&pause, NULL);
    }
    return false;
}

int main(int argc, char **argv) {
    if (argc == 2 && !strcmp(argv[1], "--version")) {
        puts("stelvio-interface-detach-proof/1");
        return 0;
    }
    if (argc != 1 || !stlv_io_supported() || !stlv_trusted_image()) return 1;
    int image = stlv_image_lock();
    if (image < 0) return 1;
    struct stlv_interface interfaces[2] = {{.descriptor=-1}, {.descriptor=-1}};
    struct stlv_range ranges[2] = {{.cidr="10.254.0.0/16"}, {.cidr="10.253.0.0/16"}};
    char names[2][IFNAMSIZ] = {{0}};
    int carrier[2] = {-1, -1}, retained = -1;
    int result = 1;
    for (unsigned i = 0; i < 2; i++) {
        if (!stlv_cidr(ranges[i].cidr, &ranges[i].network, &ranges[i].mask) ||
            stlv_route_conflict(ranges[i].network, ranges[i].mask, 0) != 0) goto done;
    }
    if (socketpair(AF_UNIX, SOCK_DGRAM, 0, carrier) || (retained = dup(carrier[1])) < 0) goto done;
    for (unsigned i = 0; i < 2; i++) {
        if (!stlv_interface_open(&interfaces[i])) goto done;
        strlcpy(names[i], interfaces[i].name, sizeof(names[i]));
        if (!stlv_interface_configure(&interfaces[i], (uint8_t)i) ||
            !stlv_interface_add_route(&interfaces[i], &ranges[i])) goto done;
    }
    stlv_interface_close(&interfaces[0]);
    if (!absent(names[0], &ranges[0]) || fcntl(retained, F_GETFD) < 0 ||
        !stlv_interface_current(&interfaces[1]) ||
        stlv_route_status(&ranges[1], interfaces[1].index) != STLV_ROUTE_OWNED) goto done;
    stlv_interface_close(&interfaces[1]);
    if (!absent(names[1], &ranges[1])) goto done;
    puts("two-unit interface detach PASS; retained packet descriptor cannot retain utun");
    result = 0;
done:
    /* No prefix-based delete. Only these freshly created kernel interfaces close. */
    for (unsigned i = 0; i < 2; i++) stlv_interface_close(&interfaces[i]);
    for (unsigned i = 0; i < 2; i++) {
        if (names[i][0] && !absent(names[i], &ranges[i])) {
            fprintf(stderr, "interface detach unverified: %s %s\n", names[i], ranges[i].cidr);
            result = 1;
        }
    }
    if (retained >= 0) close(retained);
    if (carrier[0] >= 0) close(carrier[0]);
    if (carrier[1] >= 0) close(carrier[1]);
    close(image);
    return result;
}
