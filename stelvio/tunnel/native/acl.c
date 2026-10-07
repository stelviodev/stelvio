#include "acl.h"
#include <errno.h>
#include <sys/acl.h>
#include <sys/stat.h>

static bool empty(acl_t acl) {
    /* Darwin reports ENOENT for an existing object with no extended ACL.
     * Invalid descriptors/unsupported ACL queries have different errors. */
    if (!acl) return errno == ENOENT;
    acl_entry_t entry;
    bool result = false;
    if (!acl_valid(acl)) {
        errno = 0;
        result = acl_get_entry(acl, ACL_FIRST_ENTRY, &entry) == -1 && errno == EINVAL;
    }
    if (acl_free(acl)) result = false;
    return result;
}
bool stlv_no_acl_fd(int descriptor) { return empty(acl_get_fd_np(descriptor, ACL_TYPE_EXTENDED)); }
bool stlv_no_acl_path(const char *path) {
    struct stat before, after;
    if (!path || lstat(path, &before)) return false;
    errno = 0;
    bool result = empty(acl_get_link_np(path, ACL_TYPE_EXTENDED));
    return result && !lstat(path, &after) && before.st_dev == after.st_dev && before.st_ino == after.st_ino;
}
