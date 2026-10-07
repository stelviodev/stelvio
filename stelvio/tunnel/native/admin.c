/* Fixed native administrative operations. No caller paths, shell, Python, or
 * AWS operations. Ownership receipts live on the installed image itself and
 * survive partial publication/removal of the dedicated state directory. */
#include "admin.h"
#include "acl.h"
#include "io.h"
#include "ownership.h"
#include "service.h"
#include <dirent.h>
#include <errno.h>
#include <fcntl.h>
#include <signal.h>
#include <spawn.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/file.h>
#include <sys/stat.h>
#include <sys/wait.h>
#include <sys/xattr.h>
#include <time.h>
#include <unistd.h>

#define RECEIPT "dev.stelvio.tunnel.installation"
#define GATE STLV_STATE "/admission"
#define PENDING STLV_STATE "/daemon.pending"
static const char plist[] =
"<?xml version=\"1.0\" encoding=\"UTF-8\"?>\n"
"<!DOCTYPE plist PUBLIC \"-//Apple//DTD PLIST 1.0//EN\" \"http://www.apple.com/DTDs/PropertyList-1.0.dtd\">\n"
"<plist version=\"1.0\"><dict>"
"<key>Label</key><string>" STLV_LAUNCH_LABEL "</string>"
"<key>ProgramArguments</key><array><string>" STLV_INSTALL "</string><string>--serve</string></array>"
"<key>UserName</key><string>root</string><key>RunAtLoad</key><true/>"
"<key>KeepAlive</key><true/>"
"<key>EnvironmentVariables</key><dict><key>PATH</key><string>/usr/bin:/bin</string></dict>"
"<key>Sockets</key><dict><key>Control</key><dict>"
"<key>SockPathName</key><string>" STLV_SOCKET "</string>"
"<key>SockPathMode</key><integer>438</integer><key>SockType</key><string>stream</string>"
"</dict></dict></dict></plist>\n";
struct identity { uint64_t device, inode; };
struct receipt {
    char magic[8];
    struct identity image, directory, daemon, socket;
    char staging[64];
};
static bool directory(const char *path) {
    struct stat value;
    return !lstat(path, &value) && S_ISDIR(value.st_mode) && !value.st_uid &&
        !(value.st_mode & 022) && stlv_no_acl_path(path);
}
static struct identity identity(const struct stat *value) {
    return (struct identity){(uint64_t)value->st_dev, value->st_ino};
}
static bool matches(const struct stat *value, struct identity expected) {
    return expected.inode && (uint64_t)value->st_dev == expected.device && value->st_ino == expected.inode;
}
static bool sync_parent(const char *path) {
    int fd = open(path, O_RDONLY | O_DIRECTORY | O_NOFOLLOW | O_CLOEXEC);
    bool result = fd >= 0 && stlv_no_acl_fd(fd) && !fsync(fd);
    if (fd >= 0 && close(fd)) result = false;
    return result;
}
static bool write_receipt(int fd, const struct receipt *receipt) {
    return stlv_trusted_image() && !fsetxattr(fd, RECEIPT, receipt, sizeof(*receipt), 0, 0) && !fsync(fd);
}
static int read_receipt(int fd, struct receipt *receipt) {
    memset(receipt, 0, sizeof(*receipt));
    ssize_t size = fgetxattr(fd, RECEIPT, receipt, sizeof(*receipt), 0, 0);
    if (size < 0 && errno == ENOATTR) return 0;
    struct stat value;
    if (size != sizeof(*receipt) || memcmp(receipt->magic, "STLVINS1", 8) ||
        fstat(fd, &value) || !matches(&value, receipt->image) ||
        !memchr(receipt->staging, 0, sizeof(receipt->staging))) return -1;
    if (receipt->staging[0]) {
        if (strncmp(receipt->staging, ".tunnel.", 8)) return -1;
        for (size_t i = 8; receipt->staging[i]; i++)
            if (!((receipt->staging[i] >= 'a' && receipt->staging[i] <= 'z') ||
                  (receipt->staging[i] >= 'A' && receipt->staging[i] <= 'Z') ||
                  (receipt->staging[i] >= '0' && receipt->staging[i] <= '9'))) return -1;
    }
    return 1;
}
int stlv_gate_lock(int exclusive) {
    if (!stlv_trusted_image() || !directory(STLV_STATE_PARENT) || !directory(STLV_STATE)) return -1;
    int fd = open(GATE, O_RDWR | O_NOFOLLOW | O_NONBLOCK | O_CLOEXEC);
    struct stat opened, named;
    if (fd < 0) return -1;
    bool ok = !fstat(fd, &opened) && S_ISREG(opened.st_mode) && !opened.st_uid &&
        opened.st_nlink == 1 && (opened.st_mode & 0777) == 0600 && stlv_no_acl_fd(fd) &&
        !flock(fd, (exclusive ? LOCK_EX : LOCK_SH) | LOCK_NB) &&
        !lstat(GATE, &named) && opened.st_dev == named.st_dev && opened.st_ino == named.st_ino;
    if (!ok) { close(fd); return -1; }
    return fd;
}
static int launch(const char *operation) {
    char *const arguments[] = {"/bin/launchctl", (char *)operation, "system", STLV_PLIST, NULL};
    char *const environment[] = {"PATH=/usr/bin:/bin", NULL};
    pid_t child;
    if (posix_spawn(&child, arguments[0], NULL, NULL, arguments, environment)) return -1;
    struct timespec delay = {.tv_nsec=100000000};
    int status;
    for (unsigned i = 0; i < 350; i++) {
        pid_t result = waitpid(child, &status, WNOHANG);
        if (result == child) return WIFEXITED(status) ? WEXITSTATUS(status) : -1;
        if (result < 0 && errno != EINTR) return -1;
        nanosleep(&delay, NULL);
    }
    kill(child, SIGKILL); waitpid(child, &status, 0); return -1;
}
static bool exact_file(const char *path, struct identity expected, const char *bytes, size_t size) {
    int fd = open(path, O_RDONLY | O_NOFOLLOW | O_NONBLOCK | O_CLOEXEC);
    if (fd < 0) return false;
    struct stat value, named;
    char buffer[sizeof(plist)];
    bool ok = !fstat(fd, &value) && S_ISREG(value.st_mode) && !value.st_uid &&
        !(value.st_mode & 022) && value.st_nlink <= 2 && stlv_no_acl_fd(fd) && matches(&value, expected) &&
        value.st_size == (off_t)size && size <= sizeof(buffer) && read(fd, buffer, size) == (ssize_t)size &&
        !memcmp(buffer, bytes, size) && !lstat(path, &named) && matches(&named, expected);
    close(fd); return ok;
}
static bool prepare(int image, struct receipt *receipt) {
    if (!directory("/Library/Application Support") || !directory("/Library/LaunchDaemons")) return false;
    mode_t previous_mask = umask(0);
    bool created_parent = !mkdir(STLV_STATE_PARENT, 0755);
    int creation_error = errno; umask(previous_mask); errno = creation_error;
    if (!created_parent && errno != EEXIST) return false;
    if (created_parent && (chmod(STLV_STATE_PARENT, 0755) || !sync_parent("/Library/Application Support"))) return false;
    if (!directory(STLV_STATE_PARENT)) return false;
    struct stat value;
    if (!receipt->directory.inode) {
        /* Durable reserved-name intent precedes even the private mkdir. */
        char temporary[sizeof(STLV_STATE_PARENT) + 66];
        if (!receipt->staging[0]) {
            if (lstat(STLV_STATE, &value) == 0 || errno != ENOENT) return false;
            uint64_t random; arc4random_buf(&random, sizeof(random));
            snprintf(receipt->staging, sizeof(receipt->staging), ".tunnel.%016llx", (unsigned long long)random);
            if (!write_receipt(image, receipt)) return false;
        }
        snprintf(temporary, sizeof(temporary), "%s/%s", STLV_STATE_PARENT, receipt->staging);
        previous_mask = umask(0);
        bool created = !mkdir(temporary, 0711);
        creation_error = errno; umask(previous_mask); errno = creation_error;
        if (!created && errno != EEXIST) return false;
        if (created && chmod(temporary, 0711)) return false;
        if (!directory(temporary) || lstat(temporary, &value) || (value.st_mode & 0777) != 0711) return false;
        DIR *entries = opendir(temporary);
        if (!entries) return false;
        struct dirent *entry; bool empty = true;
        while ((entry = readdir(entries)))
            if (strcmp(entry->d_name, ".") && strcmp(entry->d_name, "..")) empty = false;
        closedir(entries); if (!empty) return false;
        receipt->directory = identity(&value);
        if (!write_receipt(image, receipt) || !sync_parent(STLV_STATE_PARENT)) return false;
    }
    if (receipt->staging[0]) {
        char temporary[sizeof(STLV_STATE_PARENT) + 66];
        snprintf(temporary, sizeof(temporary), "%s/%s", STLV_STATE_PARENT, receipt->staging);
        if (!lstat(temporary, &value)) {
            if (!matches(&value, receipt->directory) || !directory(temporary) ||
                renamex_np(temporary, STLV_STATE, RENAME_EXCL)) return false;
        } else if (errno != ENOENT) return false;
        if (!sync_parent(STLV_STATE_PARENT) || lstat(STLV_STATE, &value) ||
            !matches(&value, receipt->directory) || !directory(STLV_STATE)) return false;
        memset(receipt->staging, 0, sizeof(receipt->staging));
        if (!write_receipt(image, receipt)) return false;
    }
    if (lstat(STLV_STATE, &value) || !matches(&value, receipt->directory) || !directory(STLV_STATE)) return false;
    int gate = open(GATE, O_WRONLY | O_CREAT | O_EXCL | O_NOFOLLOW | O_CLOEXEC, 0600);
    if (gate >= 0) { if (fsync(gate)) { close(gate); return false; } close(gate); }
    else if (errno != EEXIST) return false;
    return sync_parent(STLV_STATE);
}
static bool publish(int image, struct receipt *receipt) {
    struct stat value;
    if (!receipt->daemon.inode) {
        int fd = open(PENDING, O_RDWR | O_CREAT | O_EXCL | O_NOFOLLOW | O_CLOEXEC, 0644);
        bool created = fd >= 0;
        if (!created && errno == EEXIST) fd = open(PENDING, O_RDWR | O_NOFOLLOW | O_NONBLOCK | O_CLOEXEC);
        if (fd < 0) return false;
        char prefix[sizeof(plist)];
        bool ok = !fstat(fd, &value) && S_ISREG(value.st_mode) && !value.st_uid && value.st_nlink == 1 &&
            !(value.st_mode & 077) && value.st_size >= 0 && value.st_size <= (off_t)(sizeof(plist)-1) && stlv_no_acl_fd(fd);
        /* Fresh stage starts0600 under umask. A crashed stage may already be
         *0644; it is private in our receipted root-only writable directory. */
        if (!ok && !created) ok = !fstat(fd, &value) && S_ISREG(value.st_mode) && !value.st_uid &&
            value.st_nlink == 1 && (value.st_mode & 0777) == 0644 && value.st_size >= 0 &&
            value.st_size <= (off_t)(sizeof(plist)-1) && stlv_no_acl_fd(fd);
        if (ok) ok = read(fd, prefix, value.st_size) == value.st_size && !memcmp(prefix, plist, value.st_size);
        if (ok) ok = write(fd, plist + value.st_size, sizeof(plist)-1-value.st_size) ==
            (ssize_t)(sizeof(plist)-1-value.st_size) && !fchmod(fd, 0644) && !fsync(fd) && !fstat(fd, &value);
        close(fd); if (!ok) return false;
        receipt->daemon = identity(&value);
        if (!write_receipt(image, receipt) || !sync_parent(STLV_STATE)) return false;
    }
    if (!lstat(PENDING, &value)) {
        if (!exact_file(PENDING, receipt->daemon, plist, sizeof(plist)-1)) return false;
        if (link(PENDING, STLV_PLIST) && errno != EEXIST) return false;
        if (!exact_file(STLV_PLIST, receipt->daemon, plist, sizeof(plist)-1) ||
            !sync_parent("/Library/LaunchDaemons") || unlink(PENDING) || !sync_parent(STLV_STATE)) return false;
    } else if (errno != ENOENT) return false;
    return exact_file(STLV_PLIST, receipt->daemon, plist, sizeof(plist)-1);
}
static bool socket_receipt(int image, struct receipt *receipt) {
    struct timespec delay = {.tv_nsec=100000000};
    for (unsigned i = 0; i < 50; i++) {
        struct stat value;
        if (!lstat(STLV_SOCKET, &value)) {
            if (!S_ISSOCK(value.st_mode) || value.st_uid || (value.st_mode & 0777) != 0666 ||
                !stlv_no_acl_path(STLV_SOCKET)) return false;
            if (receipt->socket.inode && !matches(&value, receipt->socket)) return false;
            receipt->socket = identity(&value);
            return write_receipt(image, receipt) && sync_parent(STLV_STATE);
        }
        if (errno != ENOENT) return false;
        nanosleep(&delay, NULL);
    }
    return false;
}
static bool dispose_file(const char *path, const char *retired, struct identity expected,
                         const char *bytes, size_t size, const char *parent) {
    struct stat value;
    if (lstat(retired, &value)) {
        if (errno != ENOENT) return false;
        if (lstat(path, &value)) return errno == ENOENT && sync_parent(parent);
        if (!exact_file(path, expected, bytes, size) || renamex_np(path, retired, RENAME_EXCL)) return false;
    }
    /* Validate the actual moved inode; never unlink a concurrent replacement. */
    if (!exact_file(retired, expected, bytes, size)) {
        renamex_np(retired, path, RENAME_EXCL); return false;
    }
    return !unlink(retired) && sync_parent(parent);
}
static bool dispose_identity(const char *path, const char *retired, struct identity expected,
                             mode_t type, const char *parent) {
    struct stat value;
    if (lstat(retired, &value)) {
        if (errno != ENOENT) return false;
        if (lstat(path, &value)) return errno == ENOENT && sync_parent(parent);
        if (!matches(&value, expected) || (value.st_mode & S_IFMT) != type || value.st_uid ||
            !stlv_no_acl_path(path) || renamex_np(path, retired, RENAME_EXCL)) return false;
    }
    if (!matches(&value, expected) || (value.st_mode & S_IFMT) != type || value.st_uid ||
        !stlv_no_acl_path(retired)) { renamex_np(retired, path, RENAME_EXCL); return false; }
    /* Rename may have raced: reread the actual moved entry, not the preflight. */
    if (lstat(retired, &value) || !matches(&value, expected) || (value.st_mode & S_IFMT) != type ||
        value.st_uid || !stlv_no_acl_path(retired)) { renamex_np(retired, path, RENAME_EXCL); return false; }
    return !unlink(retired) && sync_parent(parent);
}
static bool empty_private(const char *name) {
    char path[sizeof(STLV_STATE)+32]; snprintf(path, sizeof(path), "%s/%s", STLV_STATE, name);
    int fd = open(path, O_RDONLY | O_NOFOLLOW | O_NONBLOCK | O_CLOEXEC);
    if (fd < 0) return errno == ENOENT;
    struct stat value, named;
    bool ok = !fstat(fd, &value) && S_ISREG(value.st_mode) && !value.st_uid &&
        value.st_nlink == 1 && value.st_size == 0 && (value.st_mode & 0777) == 0600 && stlv_no_acl_fd(fd) &&
        !lstat(path, &named) && value.st_dev == named.st_dev && value.st_ino == named.st_ino;
    /* The root-only owned directory and stopped daemon exclude nonroot renames. */
    if (ok) ok = !unlink(path) && sync_parent(STLV_STATE);
    close(fd); return ok;
}
static int uninstall(int receipt_fd, struct receipt *receipt, int gate) {
    if (read_receipt(receipt_fd, receipt) != 1 || !receipt->directory.inode) return 1;
    struct stat value;
    bool absent = lstat(STLV_STATE, &value) && errno == ENOENT;
    if (!absent && (!matches(&value, receipt->directory) || !directory(STLV_STATE))) return 1;
    if (receipt->daemon.inode && !exact_file(STLV_PLIST, receipt->daemon, plist, sizeof(plist)-1)) {
        if (!lstat(STLV_PLIST, &value) || errno != ENOENT) return 1;
    }
    if (!absent && gate < 0) return 3;
    /* Exclusive admission is held before stopping launchd; an active session
     * holds the shared gate throughout its journal/host cleanup. */
    launch("bootout");
    int image = -1;
    struct timespec delay = {.tv_nsec=100000000};
    for (unsigned i = 0; i < 350 && image < 0; i++) {
        image = stlv_image_lock(); if (image < 0) nanosleep(&delay, NULL);
    }
    if (image < 0) return 1;
    if (!absent) {
        int lease = stlv_state_lock();
        if (lease < 0 || stlv_offline_cleanup(image, lease)) { if (lease >= 0) close(lease); close(image); return 1; }
        close(lease);
    }
    if (receipt->daemon.inode && !dispose_file(STLV_PLIST,
        "/Library/LaunchDaemons/.dev.stelvio.tunnel.retired", receipt->daemon, plist, sizeof(plist)-1,
        "/Library/LaunchDaemons")) { close(image); return 1; }
    if (!absent) {
        if (!lstat(PENDING, &value)) {
            if (!receipt->daemon.inode) {
                /* Complete private staging and record its ownership, without
                 * publishing a LaunchDaemon during cleanup. */
                int stage = open(PENDING, O_RDWR | O_NOFOLLOW | O_NONBLOCK | O_CLOEXEC);
                char bytes[sizeof(plist)];
                bool valid = stage >= 0 && !fstat(stage, &value) && S_ISREG(value.st_mode) && !value.st_uid &&
                    value.st_nlink == 1 && !(value.st_mode & 022) && stlv_no_acl_fd(stage) &&
                    value.st_size >= 0 && value.st_size <= (off_t)(sizeof(plist)-1) &&
                    read(stage, bytes, value.st_size) == value.st_size && !memcmp(bytes, plist, value.st_size);
                if (valid) valid = write(stage, plist+value.st_size, sizeof(plist)-1-value.st_size) ==
                    (ssize_t)(sizeof(plist)-1-value.st_size) && !fsync(stage);
                if (stage >= 0) close(stage);
                if (!valid) { close(image); return 1; }
                receipt->daemon = identity(&value);
                if (!write_receipt(receipt_fd, receipt)) { close(image); return 1; }
            }
        } else if (errno != ENOENT) { close(image); return 1; }
        if (receipt->daemon.inode && !dispose_file(PENDING, STLV_STATE "/daemon.retired", receipt->daemon,
            plist, sizeof(plist)-1, STLV_STATE)) { close(image); return 1; }
        if (!dispose_identity(STLV_SOCKET, STLV_STATE "/helper.retired", receipt->socket,
            S_IFSOCK, STLV_STATE)) { close(image); return 1; }
        if (!empty_private("lease") || !empty_private("admission") || !sync_parent(STLV_STATE)) { close(image); return 1; }
        snprintf(receipt->staging, sizeof(receipt->staging), ".tunnel.retired");
        if (!write_receipt(receipt_fd, receipt) ||
            renamex_np(STLV_STATE, STLV_STATE_PARENT "/.tunnel.retired", RENAME_EXCL)) { close(image); return 1; }
        if (lstat(STLV_STATE_PARENT "/.tunnel.retired", &value) || !matches(&value, receipt->directory) ||
            !directory(STLV_STATE_PARENT "/.tunnel.retired")) {
            renamex_np(STLV_STATE_PARENT "/.tunnel.retired", STLV_STATE, RENAME_EXCL); close(image); return 1;
        }
        if (rmdir(STLV_STATE_PARENT "/.tunnel.retired") || !sync_parent(STLV_STATE_PARENT)) { close(image); return 1; }
    }
    if (!stlv_trusted_image() || fstat(receipt_fd, &value) || !matches(&value, receipt->image) ||
        !dispose_identity(STLV_INSTALL, "/Library/PrivilegedHelperTools/.dev.stelvio.tunnel.retired",
            receipt->image, S_IFREG, "/Library/PrivilegedHelperTools")) { close(image); return 1; }
    close(image); return 0;
}
int stlv_admin(int install) {
    if (!stlv_trusted_image() || (install && !stlv_io_supported())) return 1;
    umask(077);
    int fd = open(STLV_INSTALL, O_RDONLY | O_NOFOLLOW | O_CLOEXEC);
    if (fd < 0) return 1;
    struct receipt receipt;
    int loaded = read_receipt(fd, &receipt), result = 1, gate = -1;
    if (loaded < 0) goto done;
    if (!install) {
        if (!loaded) {
            int image = stlv_image_lock();
            if (image < 0) { result = 3; goto done; }
            if (read_receipt(fd, &receipt) != 0) { close(image); result = 3; goto done; }
            struct stat value;
            if ((!lstat(STLV_STATE, &value) || errno != ENOENT) ||
                (!lstat(STLV_PLIST, &value) || errno != ENOENT) || fstat(fd, &value)) { close(image); goto done; }
            result = dispose_identity(STLV_INSTALL, "/Library/PrivilegedHelperTools/.dev.stelvio.tunnel.retired",
                identity(&value), S_IFREG, "/Library/PrivilegedHelperTools") ? 0 : 1;
            close(image); goto done;
        }
        gate = stlv_gate_lock(1);
        if (gate < 0) {
            struct stat named;
            if (lstat(GATE, &named) && errno == ENOENT) {
                int image = stlv_image_lock();
                if (image >= 0) {
                    bool prepared = read_receipt(fd, &receipt) == 1 && prepare(fd, &receipt); close(image);
                    if (prepared) gate = stlv_gate_lock(1);
                }
            }
        }
        result = uninstall(fd, &receipt, gate); goto done;
    }
    int image = stlv_image_lock();
    if (image < 0) { result = 3; goto done; }
    loaded = read_receipt(fd, &receipt);
    if (loaded < 0) { close(image); goto done; }
    struct stat gate_named;
    if (!lstat(GATE, &gate_named)) {
        gate = stlv_gate_lock(1);
        if (gate < 0) { close(image); result = 3; goto done; }
    } else if (errno != ENOENT) { close(image); goto done; }
    if (!loaded) {
        struct stat value;
        if (fstat(fd, &value)) { close(image); goto done; }
        memcpy(receipt.magic, "STLVINS1", 8); receipt.image = identity(&value);
        if (!write_receipt(fd, &receipt) || !sync_parent("/Library/PrivilegedHelperTools")) { close(image); goto done; }
    }
    if (!prepare(fd, &receipt) || (gate < 0 && (gate = stlv_gate_lock(1)) < 0) || !publish(fd, &receipt)) {
        close(image); goto done;
    }
    close(image);
    int launched = launch("bootstrap");
    /* EIO(5) is launchctl's already-bootstrapped response. The actual socket
     * ownership and a subsequent nonroot protocol inspection certify readiness. */
    if (launched != 0 && launched != 5) goto done;
    if (!socket_receipt(fd, &receipt)) goto done;
    result = 0;
done:
    if (gate >= 0) close(gate);
    close(fd);
    if (result == 3) fputs("Helper is active; close its session and retry.\n", stderr);
    else if (result) fputs("Helper administration is incomplete; owned recovery records retained.\n", stderr);
    return result;
}
