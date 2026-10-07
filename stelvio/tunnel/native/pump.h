#ifndef STELVIO_TUNNEL_PUMP_H
#define STELVIO_TUNNEL_PUMP_H

#include "interface.h"
#include "packet_io.h"

enum stlv_pump_result { STLV_PUMP_ERROR = -1, STLV_PUMP_IDLE = 0,
                        STLV_PUMP_MOVED = 1, STLV_PUMP_DROPPED = 2 };

/* At most one datagram per call, no TCP parsing/SSH/credentials. The service
 * supplies an ACTIVE configuration from its authenticated lease, its own live
 * kernel descriptor, and the fixed address for that unit's bounded slot. */
enum stlv_pump_result stlv_pump_host(const struct stlv_interface *interface,
                                    const struct stlv_request *configuration,
                                    uint32_t local_address, int carrier);
enum stlv_pump_result stlv_pump_return(const struct stlv_packet_frame *frame,
                                      const struct stlv_interface *interface,
                                      const struct stlv_request *configuration,
                                      uint32_t local_address);

#endif
