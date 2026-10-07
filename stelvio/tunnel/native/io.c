#include "io.h"
#include <errno.h>
#include <limits.h>
#include <mach/machine.h>
#include <poll.h>
#include <string.h>
#include <sys/socket.h>
#include <sys/sysctl.h>
#include <time.h>
#include <unistd.h>

#define IO_MILLISECONDS 10000
/* The selected Darwin 24 arm64 kernel accepts one control mbuf no larger than
 * MCLBYTES=2048 (sockargs/unp_internalize). Reserve more than that full message,
 * never merely space for the number of descriptors this protocol expects.
 * Future kernel profiles must re-establish this bound before enabling service. */
#define CONTROL_BYTES STLV_CONTROL_BYTES

bool stlv_io_supported(void) {
    char release[64] = {0}, product[32] = {0};
    size_t release_size = sizeof(release), product_size = sizeof(product);
    cpu_type_t cpu;
    size_t cpu_size = sizeof(cpu);
    if (sysctlbyname("kern.osrelease", release, &release_size, NULL, 0) ||
        sysctlbyname("kern.osproductversion", product, &product_size, NULL, 0) ||
        sysctlbyname("hw.cputype", &cpu, &cpu_size, NULL, 0)) return false;
    return release_size && release_size <= sizeof(release) && !release[release_size - 1] &&
           product_size && product_size <= sizeof(product) && !product[product_size - 1] &&
           !strcmp(release, "24.6.0") && !strcmp(product, "15.7.5") &&
           cpu_size == sizeof(cpu) && cpu == CPU_TYPE_ARM64;
}

static uint64_t milliseconds(void) {
    struct timespec now;
    if (clock_gettime(CLOCK_MONOTONIC, &now)) return 0;
    return (uint64_t)now.tv_sec * 1000 + (uint64_t)now.tv_nsec / 1000000;
}

static bool ready(int connection, short events, uint64_t deadline) {
    while (true) {
        uint64_t now = milliseconds();
        if (!now || now >= deadline) return false;
        uint64_t remaining = deadline - now;
        struct pollfd socket = {.fd=connection, .events=events};
        int result = poll(&socket, 1, remaining < INT_MAX ? (int)remaining : INT_MAX);
        if (result < 0 && errno == EINTR) continue;
        return result > 0 && (socket.revents & events);
    }
}

bool stlv_ancillary_free(const struct msghdr *message, const void *control, size_t capacity) {
    const uint8_t *bytes = control;
    size_t copied = message->msg_controllen;
    bool ancillary = copied != 0 || (message->msg_flags & MSG_CTRUNC);
    if (copied > capacity) copied = capacity;
    for (size_t position = 0; copied - position >= sizeof(struct cmsghdr);) {
        struct cmsghdr header_value;
        memcpy(&header_value, bytes + position, sizeof(header_value));
        const struct cmsghdr *header = &header_value;
        size_t available = copied - position;
        if (header->cmsg_len < CMSG_LEN(0)) break;
        size_t length = header->cmsg_len < available ? header->cmsg_len : available;
        if (header->cmsg_level == SOL_SOCKET && header->cmsg_type == SCM_RIGHTS && length >= CMSG_LEN(0)) {
            size_t descriptors = (length - CMSG_LEN(0)) / sizeof(int);
            for (size_t i = 0; i < descriptors; i++) {
                int received;
                memcpy(&received, bytes + position + CMSG_LEN(0) + i * sizeof(int), sizeof(int));
                close(received);
            }
        }
        size_t padded = CMSG_SPACE(header->cmsg_len - CMSG_LEN(0));
        if (padded > available || !padded) break;
        position += padded;
    }
    return !ancillary;
}

static bool receive_bytes(int connection, uint8_t *packet, size_t size, uint64_t deadline) {
    for (size_t offset = 0; offset < size;) {
        if (!ready(connection, POLLIN, deadline)) return false;
        union { struct cmsghdr aligned; uint8_t bytes[CONTROL_BYTES]; } control = {0};
        struct iovec vector = {.iov_base=packet + offset, .iov_len=size - offset};
        struct msghdr message = {.msg_iov=&vector, .msg_iovlen=1,
                                .msg_control=control.bytes, .msg_controllen=sizeof(control.bytes)};
        ssize_t count = recvmsg(connection, &message, MSG_DONTWAIT);
        if (count < 0 && (errno == EINTR || errno == EAGAIN)) continue;
        if (count <= 0) return false;
        if (!stlv_ancillary_free(&message, control.bytes, sizeof(control.bytes))) return false;
        offset += (size_t)count;
    }
    return true;
}

bool stlv_receive(int connection, struct stlv_request *request) {
    if (!stlv_io_supported()) return false;
    uint8_t packet[STLV_HEADER + STLV_MAX_BODY];
    uint64_t began = milliseconds();
    if (!began || !receive_bytes(connection, packet, STLV_HEADER, began + IO_MILLISECONDS))
        return false;
    size_t body = ((uint32_t)packet[12] << 24) | ((uint32_t)packet[13] << 16) |
                  ((uint32_t)packet[14] << 8) | packet[15];
    if (body > STLV_MAX_BODY ||
        !receive_bytes(connection, packet + STLV_HEADER, body, began + IO_MILLISECONDS)) return false;
    return stlv_decode(packet, STLV_HEADER + body, request);
}

bool stlv_reply(int connection, uint16_t status, const void *payload, size_t size, int descriptor) {
    if (!stlv_io_supported() || size > STLV_MAX_REPLY || (size && !payload) || descriptor < -1) return false;
    int no_sigpipe = 1;
    if (setsockopt(connection, SOL_SOCKET, SO_NOSIGPIPE, &no_sigpipe, sizeof(no_sigpipe))) return false;
    uint8_t packet[16 + STLV_MAX_REPLY] = "STLVREP1";
    packet[8] = (uint8_t)(status >> 8);
    packet[9] = (uint8_t)status;
    packet[12] = (uint8_t)(size >> 24);
    packet[13] = (uint8_t)(size >> 16);
    packet[14] = (uint8_t)(size >> 8);
    packet[15] = (uint8_t)size;
    if (size) memcpy(packet + 16, payload, size);
    uint64_t began = milliseconds();
    if (!began) return false;
    for (size_t offset = 0; offset < size + 16;) {
        if (!ready(connection, POLLOUT, began + IO_MILLISECONDS)) return false;
        char control[CMSG_SPACE(sizeof(int))] = {0};
        struct iovec vector = {.iov_base=packet + offset, .iov_len=size + 16 - offset};
        struct msghdr message = {.msg_iov=&vector, .msg_iovlen=1};
        if (!offset && descriptor >= 0) {
            message.msg_control = control;
            message.msg_controllen = sizeof(control);
            struct cmsghdr *header = CMSG_FIRSTHDR(&message);
            header->cmsg_level = SOL_SOCKET;
            header->cmsg_type = SCM_RIGHTS;
            header->cmsg_len = CMSG_LEN(sizeof(int));
            memcpy(CMSG_DATA(header), &descriptor, sizeof(int));
        }
        ssize_t count = sendmsg(connection, &message, MSG_DONTWAIT);
        if (count < 0 && (errno == EINTR || errno == EAGAIN)) continue;
        if (count <= 0) return false;
        offset += (size_t)count;
    }
    return true;
}
