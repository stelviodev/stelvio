/* Nonroot read-only ACL boundary harness. */
#include "acl.h"
#include <fcntl.h>
#include <stdio.h>
#include <unistd.h>
int main(int argc, char **argv) {
    if (argc != 2 || !geteuid()) return 2;
    int descriptor = open(argv[1], O_RDONLY | O_NONBLOCK | O_NOFOLLOW | O_CLOEXEC);
    if (descriptor < 0) return 2;
    bool file = stlv_no_acl_fd(descriptor), path = stlv_no_acl_path(argv[1]);
    close(descriptor);
    printf("%d %d\n", file, path);
    return 0;
}
