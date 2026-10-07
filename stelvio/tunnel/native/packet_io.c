#include "packet_io.h"
#include "io.h"
#include <errno.h>
#include <fcntl.h>
#include <string.h>
#include <sys/socket.h>
#include <unistd.h>

static uint64_t number(const uint8_t *bytes, size_t size) {
    uint64_t value = 0;
    for (size_t i = 0; i < size; i++) value = (value << 8) | bytes[i];
    return value;
}

int stlv_packet_receive(int descriptor, struct stlv_packet_frame *frame) {
    if (!frame) return -1;
    memset(frame, 0, sizeof(*frame));
    if (!stlv_io_supported()) return -1;
    uint8_t bytes[STLV_MAX_PACKET_FRAME];
    union { struct cmsghdr aligned; uint8_t bytes[STLV_CONTROL_BYTES]; } control = {0};
    struct iovec vector = {.iov_base=bytes, .iov_len=sizeof(bytes)};
    struct msghdr message = {.msg_iov=&vector, .msg_iovlen=1,
                            .msg_control=control.bytes, .msg_controllen=sizeof(control.bytes)};
    ssize_t count = recvmsg(descriptor, &message, MSG_DONTWAIT);
    if (count < 0) return errno == EAGAIN || errno == EINTR ? 0 : -1;
    /* Empty datagrams can carry rights: close controls before checking length. */
    if (!stlv_ancillary_free(&message, control.bytes, sizeof(control.bytes)) ||
        (message.msg_flags & MSG_TRUNC) || count < STLV_PACKET_HEADER + 24 ||
        count > STLV_MAX_PACKET_FRAME) return -1;
    frame->unit = (uint32_t)number(bytes, 4);
    frame->generation = number(bytes + 4, 8);
    if (!frame->unit || !frame->generation) { memset(frame, 0, sizeof(*frame)); return -1; }
    frame->size = (size_t)count - STLV_PACKET_HEADER;
    memcpy(frame->packet, bytes + STLV_PACKET_HEADER, frame->size);
    return 1;
}

int stlv_packet_send(int descriptor, const struct stlv_packet_frame *frame) {
    if (!stlv_io_supported() || !frame || !frame->unit || !frame->generation ||
        frame->size < 24 || frame->size > STLV_MAX_PACKET) return -1;
    uint8_t header[STLV_PACKET_HEADER];
    for (size_t i = 0; i < 4; i++) header[3 - i] = (uint8_t)(frame->unit >> (8 * i));
    for (size_t i = 0; i < 8; i++) header[11 - i] = (uint8_t)(frame->generation >> (8 * i));
    struct iovec vectors[] = {{.iov_base=header, .iov_len=sizeof(header)},
                             {.iov_base=(void *)frame->packet, .iov_len=frame->size}};
    struct msghdr message = {.msg_iov=vectors, .msg_iovlen=2};
    ssize_t count = sendmsg(descriptor, &message, MSG_DONTWAIT);
    if (count < 0 && (errno == EAGAIN || errno == EINTR)) return 0;
    return count == (ssize_t)(sizeof(header) + frame->size) ? 1 : -1;
}

bool stlv_packet_pair(int descriptors[2]) {
    if (!descriptors) return false;
    descriptors[0] = descriptors[1] = -1;
    if (!stlv_io_supported() || socketpair(AF_UNIX, SOCK_DGRAM, 0, descriptors)) return false;
    int capacity = 256 * 1024, no_sigpipe = 1;
    for (unsigned i = 0; i < 2; i++) {
        int flags = fcntl(descriptors[i], F_GETFL);
        if (flags < 0 || fcntl(descriptors[i], F_SETFL, flags | O_NONBLOCK) ||
            fcntl(descriptors[i], F_SETFD, FD_CLOEXEC) ||
            setsockopt(descriptors[i], SOL_SOCKET, SO_NOSIGPIPE, &no_sigpipe, sizeof(no_sigpipe)) ||
            setsockopt(descriptors[i], SOL_SOCKET, SO_SNDBUF, &capacity, sizeof(capacity)) ||
            setsockopt(descriptors[i], SOL_SOCKET, SO_RCVBUF, &capacity, sizeof(capacity))) {
            close(descriptors[0]); close(descriptors[1]);
            descriptors[0] = descriptors[1] = -1;
            return false;
        }
    }
    return true;
}
