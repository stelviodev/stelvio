/* Nonroot, read-only kernel authentication diagnostic, never an installed service. */
#include "ownership.h"
#include <inttypes.h>
#include <poll.h>
#include <stdio.h>
#include <string.h>
#include <sys/socket.h>
#include <sys/un.h>
#include <unistd.h>

int main(int argc, char **argv) {
    if (geteuid() == 0 || argc != 2) return 1;
    struct sockaddr_un address = {.sun_family=AF_UNIX};
    if (strlen(argv[1]) >= sizeof(address.sun_path)) return 1;
    strlcpy(address.sun_path, argv[1], sizeof(address.sun_path));
    int listener = socket(AF_UNIX, SOCK_STREAM, 0);
    if (listener < 0) return 1;
    if (bind(listener, (struct sockaddr *)&address, sizeof(address))) { close(listener); return 1; }
    int result = 1, client = -1;
    if (listen(listener, 1)) goto done;
    puts("READY");
    fflush(stdout);
    struct pollfd ready = {.fd=listener, .events=POLLIN};
    if (poll(&ready, 1, 5000) <= 0) goto done;
    client = accept(listener, NULL, NULL);
    struct stlv_peer peer;
    if (client < 0 || !stlv_peer_read(client, &peer)) goto done;
    bool alive = stlv_peer_alive(&peer);
    struct stlv_peer stale = peer;
    stale.birth_microseconds++;
    bool stale_rejected = !stlv_peer_alive(&stale);
    stale = peer;
    /* XNU's proc_pidpath_audittoken compares token.val[7] as PID idversion. */
    stale.token.val[7] ^= 1;
    bool audit_rejected = !stlv_peer_alive(&stale);
    bool uninstalled_rejected = !stlv_trusted_image() && stlv_image_lock() < 0 && stlv_state_lock() < 0;
    printf("{\"uid\":%u,\"pid\":%d,\"birth_seconds\":%" PRIu64
           ",\"alive\":%s,\"stale_rejected\":%s,\"audit_rejected\":%s,\"uninstalled_rejected\":%s}\n",
           peer.uid, peer.pid, peer.birth_seconds, alive ? "true" : "false",
           stale_rejected ? "true" : "false", audit_rejected ? "true" : "false",
           uninstalled_rejected ? "true" : "false");
    result = 0;
done:
    if (client >= 0) close(client);
    close(listener);
    if (unlink(argv[1])) result = 1;
    return result;
}
