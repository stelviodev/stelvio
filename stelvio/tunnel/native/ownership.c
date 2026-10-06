/* Kernel-authenticated peers and loaded-image/lock identity fences, macOS only. */
#include "ownership.h"
#include <errno.h>
#include <bsm/libbsm.h>
#include <fcntl.h>
#include <libproc.h>
#include <string.h>
#include <sys/file.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <sys/un.h>
#include <unistd.h>

static bool directory(const char *path) {
    struct stat state;
    return !lstat(path, &state) && S_ISDIR(state.st_mode) && !state.st_uid &&
           !(state.st_mode & 022);
}

bool stlv_trusted_image(void) {
    char path[PROC_PIDPATHINFO_MAXSIZE];
    struct stat current;
    if (geteuid() || proc_pidpath(getpid(), path, sizeof(path)) <= 0 ||
        strcmp(path, STLV_INSTALL) || lstat(STLV_INSTALL, &current) ||
        !S_ISREG(current.st_mode) || current.st_uid || current.st_nlink != 1 ||
        (current.st_mode & 022) || !directory("/Library/PrivilegedHelperTools")) return false;
    struct proc_regionwithpathinfo loaded;
    int size = proc_pidinfo(getpid(), PROC_PIDREGIONPATHINFO,
                           (uint64_t)(uintptr_t)&stlv_trusted_image, &loaded, sizeof(loaded));
    return size == sizeof(loaded) && loaded.prp_vip.vip_vi.vi_stat.vst_ino == current.st_ino &&
           loaded.prp_vip.vip_vi.vi_stat.vst_dev == (uint32_t)current.st_dev;
}

static bool process(int32_t pid, struct proc_bsdinfo *info) {
    memset(info, 0, sizeof(*info));
    return pid > 0 && proc_pidinfo(pid, PROC_PIDTBSDINFO, 0, info, sizeof(*info)) == sizeof(*info);
}

static bool token_live(const audit_token_t *token) {
    audit_token_t copy = *token;
    char path[PROC_PIDPATHINFO_MAXSIZE];
    /* The kernel checks the audit token's PID version, closing reuse between
     * the socket connection and a later birth lookup. No path is executed. */
    return proc_pidpath_audittoken(&copy, path, sizeof(path)) > 0;
}

bool stlv_peer_read(int connection, struct stlv_peer *peer) {
    memset(peer, 0, sizeof(*peer));
    uid_t uid;
    gid_t gid;
    pid_t pid;
    socklen_t size = sizeof(pid);
    audit_token_t token;
    socklen_t token_size = sizeof(token);
    struct proc_bsdinfo info;
    if (getpeereid(connection, &uid, &gid) || !uid ||
        getsockopt(connection, SOL_LOCAL, LOCAL_PEERPID, &pid, &size) || size != sizeof(pid) ||
        getsockopt(connection, SOL_LOCAL, LOCAL_PEERTOKEN, &token, &token_size) ||
        token_size != sizeof(token) || audit_token_to_pid(token) != pid ||
        audit_token_to_euid(token) != uid || audit_token_to_ruid(token) != uid ||
        !token_live(&token) ||
        !process(pid, &info) || info.pbi_uid != uid || info.pbi_ruid != uid ||
        info.pbi_status == 5 || !token_live(&token)) return false;
    peer->uid = uid;
    peer->pid = pid;
    peer->birth_seconds = info.pbi_start_tvsec;
    peer->birth_microseconds = info.pbi_start_tvusec;
    peer->token = token;
    return true;
}

bool stlv_peer_alive(const struct stlv_peer *peer) {
    struct proc_bsdinfo info;
    return peer->uid && audit_token_to_pid(peer->token) == peer->pid &&
           audit_token_to_euid(peer->token) == peer->uid && audit_token_to_ruid(peer->token) == peer->uid &&
           token_live(&peer->token) && process(peer->pid, &info) && info.pbi_status != 5 &&
           info.pbi_uid == peer->uid && info.pbi_ruid == peer->uid &&
           info.pbi_start_tvsec == peer->birth_seconds &&
           info.pbi_start_tvusec == peer->birth_microseconds && token_live(&peer->token);
}

int stlv_image_lock(void) {
    if (!stlv_trusted_image()) return -1;
    int descriptor = open(STLV_INSTALL, O_RDONLY | O_NOFOLLOW | O_CLOEXEC);
    struct stat opened, current;
    if (descriptor < 0) return -1;
    if (flock(descriptor, LOCK_EX | LOCK_NB) || fstat(descriptor, &opened) ||
        lstat(STLV_INSTALL, &current) || opened.st_dev != current.st_dev ||
        opened.st_ino != current.st_ino || !stlv_trusted_image()) {
        close(descriptor);
        return -1;
    }
    return descriptor;
}

int stlv_state_lock(void) {
    if (!stlv_trusted_image()) return -1;
    if (mkdir(STLV_STATE, 0711) && errno != EEXIST) return -1;
    if (!directory(STLV_STATE)) return -1;
    int directory_fd = open(STLV_STATE, O_RDONLY | O_DIRECTORY | O_NOFOLLOW | O_CLOEXEC);
    if (directory_fd < 0) return -1;
    int descriptor = openat(directory_fd, "lease", O_RDWR | O_NONBLOCK | O_CREAT | O_NOFOLLOW | O_CLOEXEC, 0600);
    struct stat opened, current, parent, named_parent;
    if (descriptor < 0) { close(directory_fd); return -1; }
    bool valid = !fstat(descriptor, &opened) && S_ISREG(opened.st_mode) &&
        !opened.st_uid && opened.st_nlink == 1 && !(opened.st_mode & 077) &&
        !flock(descriptor, LOCK_EX | LOCK_NB) &&
        !fstatat(directory_fd, "lease", &current, AT_SYMLINK_NOFOLLOW) &&
        opened.st_dev == current.st_dev && opened.st_ino == current.st_ino &&
        !fstat(directory_fd, &parent) && !lstat(STLV_STATE, &named_parent) &&
        parent.st_dev == named_parent.st_dev && parent.st_ino == named_parent.st_ino;
    close(directory_fd);
    if (!valid) { close(descriptor); return -1; }
    return descriptor;
}
