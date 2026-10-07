#include "pump.h"
#include "io.h"
#include "ownership.h"
#include <errno.h>
#include <sys/socket.h>

enum stlv_pump_result stlv_pump_host(const struct stlv_interface *interface,
                                    const struct stlv_request *configuration,
                                    uint32_t local_address, int carrier) {
    if (!configuration || !stlv_io_supported() || !stlv_trusted_image() ||
        !stlv_interface_current(interface)) return STLV_PUMP_ERROR;
    struct stlv_packet_frame frame = {.unit=configuration->unit,
                                      .generation=configuration->generation};
    union { struct cmsghdr aligned; uint8_t bytes[STLV_CONTROL_BYTES]; } control = {0};
    struct iovec vector = {.iov_base=frame.packet, .iov_len=sizeof(frame.packet)};
    struct msghdr message = {.msg_iov=&vector, .msg_iovlen=1,
                            .msg_control=control.bytes, .msg_controllen=sizeof(control.bytes)};
    ssize_t count = recvmsg(interface->descriptor, &message, MSG_DONTWAIT);
    if (count < 0) return errno == EAGAIN || errno == EINTR ? STLV_PUMP_IDLE : STLV_PUMP_ERROR;
    if (!stlv_ancillary_free(&message, control.bytes, sizeof(control.bytes)) ||
        (message.msg_flags & MSG_TRUNC) || count > STLV_MAX_PACKET) return STLV_PUMP_ERROR;
    frame.size = (size_t)count;
    if (!stlv_packet_allowed(frame.packet, frame.size, configuration, local_address, true))
        return STLV_PUMP_DROPPED;
    int sent = stlv_packet_send(carrier, &frame);
    return sent < 0 ? STLV_PUMP_ERROR : sent ? STLV_PUMP_MOVED : STLV_PUMP_DROPPED;
}

enum stlv_pump_result stlv_pump_return(const struct stlv_packet_frame *frame,
                                      const struct stlv_interface *interface,
                                      const struct stlv_request *configuration,
                                      uint32_t local_address) {
    if (!frame || !configuration || !stlv_io_supported() || !stlv_trusted_image() ||
        !stlv_interface_current(interface)) return STLV_PUMP_ERROR;
    if (frame->unit != configuration->unit || frame->generation != configuration->generation ||
        !stlv_packet_allowed(frame->packet, frame->size, configuration, local_address, false))
        return STLV_PUMP_DROPPED;
    ssize_t count = send(interface->descriptor, frame->packet, frame->size, MSG_DONTWAIT);
    if (count < 0 && (errno == EAGAIN || errno == EINTR)) return STLV_PUMP_DROPPED;
    return count == (ssize_t)frame->size ? STLV_PUMP_MOVED : STLV_PUMP_ERROR;
}
