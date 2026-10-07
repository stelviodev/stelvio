#ifndef STELVIO_TUNNEL_ADMIN_H
#define STELVIO_TUNNEL_ADMIN_H
/* Independent admission gate: shared for an entire host session, exclusive
 * throughout install/uninstall. Never unlink it while a daemon can acquire. */
int stlv_gate_lock(int exclusive);
int stlv_admin(int install);
int stlv_offline_cleanup(int image, int lease);
#endif
