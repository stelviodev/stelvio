#ifndef TRAFORO_BINDINGS_H
#define TRAFORO_BINDINGS_H
#include <stdint.h>
#include <stddef.h>
typedef struct { uint32_t uid; int32_t pid; uint64_t seconds, micros; uint32_t token[8]; } tf_peer;
typedef struct { uint32_t network, mask, index; } tf_route;
int tf_no_acl(int fd);
int tf_no_acl_path(const char *path);
int tf_peer_read(int fd, tf_peer *peer);
int tf_peer_live(const tf_peer *peer);
int tf_image(uint64_t *device,uint64_t *inode,char *path,size_t capacity);
int tf_listener(void);
int tf_utun(char *name,size_t capacity);
int tf_if_current(int fd,const char *name,uint32_t index);
int tf_configure(const char *name,uint32_t local,uint32_t peer);
int tf_route_ack(int descriptor,int pid,int sequence);
int tf_route_add(uint32_t index,uint32_t network,uint32_t mask);
int tf_routes(tf_route *entries,size_t capacity,size_t *count);
int tf_dns(char *buffer,size_t capacity,size_t *size);
int tf_rename_excl(int from,const char *source,int to,const char *target);
#endif
