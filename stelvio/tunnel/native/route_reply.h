#ifndef STLV_ROUTE_REPLY_H
#define STLV_ROUTE_REPLY_H
#include <stdbool.h>
#include <sys/types.h>

/* Select only this request's successful ACK within one total deadline. */
bool stlv_route_add_reply(int descriptor, pid_t pid, int sequence);
#endif
