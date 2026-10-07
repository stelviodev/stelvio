#ifndef STELVIO_TUNNEL_INTERFACE_H
#define STELVIO_TUNNEL_INTERFACE_H

#include "protocol.h"
#include <net/if.h>

struct stlv_interface { int descriptor; unsigned index; char name[IFNAMSIZ]; };

/* Open asks the kernel for a new interface; never adopts a name or saved index.
 * Address/route effects require the current descriptor and trusted root image.
 * The service journals that newly created identity before configure/add.
 * Unscoped route deletion is refused: its kernel key cannot fence a concurrent
 * foreign replacement. Per-VPC detach teardown still needs implementation/proof.
 * Do not delegate the kernel descriptor until revocation is established. */
bool stlv_interface_open(struct stlv_interface *interface);
bool stlv_interface_current(const struct stlv_interface *interface);
bool stlv_interface_configure(const struct stlv_interface *interface, uint8_t slot);
bool stlv_interface_add_route(const struct stlv_interface *interface,
                              const struct stlv_range *range);
void stlv_interface_close(struct stlv_interface *interface);

#endif
