#ifndef STELVIO_TUNNEL_ACL_H
#define STELVIO_TUNNEL_ACL_H
#include <stdbool.h>
/* Conservatively reject extended ACL entries, including inheritance rules.
 * Never rewrite an existing ACL to make a foreign setting appear trusted. */
bool stlv_no_acl_fd(int descriptor);
bool stlv_no_acl_path(const char *path);
#endif
