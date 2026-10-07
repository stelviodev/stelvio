#ifndef STELVIO_TUNNEL_SERVICE_H
#define STELVIO_TUNNEL_SERVICE_H
#define STLV_HELPER_ABI 1
#define STLV_LAUNCH_LABEL "dev.stelvio.tunnel"
#define STLV_LAUNCH_SOCKET "Control"
#define STLV_PLIST "/Library/LaunchDaemons/dev.stelvio.tunnel.plist"
enum stlv_status { STLV_OK = 0, STLV_BUSY = 1, STLV_INVALID = 2,
                   STLV_UNCERTAIN = 3, STLV_UNAUTHORIZED = 4 };
int stlv_service(void);
#endif
