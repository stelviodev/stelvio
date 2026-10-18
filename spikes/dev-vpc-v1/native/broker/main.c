/* P0 only: root owns utun/routes; TCP, SSH and credentials stay in the client.
 * No shell, executable path, arbitrary resolver suffix, or AWS input is accepted.
 */
#include <arpa/inet.h>
#include <errno.h>
#include <fcntl.h>
#include <net/if.h>
#include <net/route.h>
#include <poll.h>
#include <signal.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/file.h>
#include <sys/ioctl.h>
#include <sys/kern_control.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <sys/sysctl.h>
#include <sys/sys_domain.h>
#include <sys/wait.h>
#include <sys/un.h>
#include <time.h>
#include <unistd.h>
#include <libproc.h>

#define INSTALL "/Library/PrivilegedHelperTools/dev.stelvio.vpc-proof"
#define STATE "/private/var/run/stelvio-vpc-proof"
#define SOCKET STATE "/broker.sock"
#define LOCK STATE "/lease"
#define JOURNAL STATE "/journal"
#define MAX_RANGES 8
#define CIDR_SIZE 20
#define VERSION "stelvio-vpc-proof/1"
#define RESOLVERS "/private/etc/resolver"

struct resolver_identity { bool owned; bool ambiguous; dev_t device; ino_t inode; };

static bool valid_owner(const char *owner) {
    if (strlen(owner) != 36) return false;
    for (int i = 0; i < 36; i++) {
        if (i == 8 || i == 13 || i == 18 || i == 23) {
            if (owner[i] != '-') return false;
        } else if (!((owner[i] >= '0' && owner[i] <= '9') ||
                     (owner[i] >= 'a' && owner[i] <= 'f'))) return false;
    }
    return true;
}

static int resolver_directory(void) {
    int fd = open(RESOLVERS, O_RDONLY | O_DIRECTORY | O_NOFOLLOW | O_CLOEXEC);
    struct stat st;
    if (fd < 0) return -1;
    if (fstat(fd, &st) || !S_ISDIR(st.st_mode) || st.st_uid || (st.st_mode & 022)) {
        close(fd); return -1;
    }
    return fd;
}

static void resolver_text(const char *owner, unsigned index, char *name, char *content) {
    snprintf(name, 128, "vpc%u.%s.stelvio-proof.test", index, owner);
    snprintf(content, 256, "# stelvio-vpc-proof owner=%s\nnameserver 127.0.0.1\nport %u\ntimeout 2\n",
             owner, 10890 + index);
}

/* Inode and expected contents fence cleanup from a replacement resolver file.
 * The empty file is journaled before writing; an unrecorded crash-window file
 * is ambiguous and is retained for inspection rather than adopted. */
static int resolver_remove(const char *owner, unsigned index, struct resolver_identity identity) {
    int dir = resolver_directory();
    if (dir < 0) return -1;
    char name[128], expected[256], content[256];
    resolver_text(owner, index, name, expected);
    int fd = openat(dir, name, O_RDONLY | O_NOFOLLOW | O_CLOEXEC);
    if (fd < 0) { int absent = errno == ENOENT; close(dir); return absent ? 0 : -1; }
    struct stat st;
    ssize_t length = read(fd, content, sizeof(content));
    bool valid = identity.owned && !fstat(fd, &st) && S_ISREG(st.st_mode) &&
        st.st_uid == 0 && !(st.st_mode & 022) && st.st_nlink == 1 &&
        st.st_dev == identity.device && st.st_ino == identity.inode &&
        (length == 0 || (length == (ssize_t)strlen(expected) && !memcmp(content, expected, (size_t)length)));
    close(fd);
    int result = valid ? unlinkat(dir, name, 0) : -1;
    if (!result) result = fsync(dir);
    close(dir);
    return result;
}

static int resolver_create(const char *owner, unsigned index, FILE *record,
                           struct resolver_identity *identity) {
    int dir = resolver_directory();
    if (dir < 0) return -1;
    char name[128], content[256];
    resolver_text(owner, index, name, content);
    int fd = openat(dir, name, O_CREAT | O_EXCL | O_WRONLY | O_NOFOLLOW | O_CLOEXEC, 0644);
    if (fd < 0) { close(dir); return -1; }
    identity->ambiguous = true;
    struct stat st;
    int result = -1;
    if (fstat(fd, &st)) goto done;
    *identity = (struct resolver_identity){.owned=true, .device=st.st_dev, .inode=st.st_ino};
    fprintf(record, "resolver %u %llu %llu\n", index,
            (unsigned long long)st.st_dev, (unsigned long long)st.st_ino);
    if (fflush(record) || fsync(fileno(record))) goto done;
    size_t length = strlen(content);
    if (write(fd, content, length) != (ssize_t)length || fsync(fd) || fsync(dir)) goto done;
    result = 0;
done:
    close(fd); close(dir);
    return result;
}

static volatile sig_atomic_t stopping = 0;
static void stop_signal(int sig) { (void)sig; stopping = 1; }

static int fail(const char *message) {
    fprintf(stderr, "%s: %s\n", VERSION, message);
    return 1;
}

/* Canonical RFC1918 only: a caller can never replace the host default route. */
static bool valid_cidr(const char *text, uint32_t *network, uint32_t *mask) {
    char address[INET_ADDRSTRLEN], canonical[CIDR_SIZE];
    const char *slash = strchr(text, '/');
    if (!slash || slash == text || (size_t)(slash - text) >= sizeof(address)) return false;
    memcpy(address, text, (size_t)(slash - text));
    address[slash - text] = '\0';
    char *end;
    long prefix = strtol(slash + 1, &end, 10);
    if (*end || prefix < 16 || prefix > 28) return false;
    struct in_addr ip;
    if (inet_pton(AF_INET, address, &ip) != 1) return false;
    uint32_t value = ntohl(ip.s_addr);
    *mask = UINT32_MAX << (32 - prefix);
    *network = value & *mask;
    if (*network != value) return false;
    uint32_t last = value | ~*mask;
    bool private = (value >= 0x0a000000 && last <= 0x0affffff) ||
                   (value >= 0xac100000 && last <= 0xac1fffff) ||
                   (value >= 0xc0a80000 && last <= 0xc0a8ffff);
    snprintf(canonical, sizeof(canonical), "%s/%ld", address, prefix);
    return private && strcmp(text, canonical) == 0;
}

/* Inspect the kernel table, including broad VPN routes and narrower LAN/host
 * routes. An identical-route EEXIST check alone misses both forms of overlap. */
static bool route_conflicts(uint32_t network, uint32_t mask) {
    int mib[] = {CTL_NET, PF_ROUTE, 0, AF_INET, NET_RT_DUMP2, 0};
    size_t length = 0;
    if (sysctl(mib, 6, NULL, &length, NULL, 0) || length > 16 * 1024 * 1024) return true;
    char *table = malloc(length);
    if (!table) return true;
    if (sysctl(mib, 6, table, &length, NULL, 0)) { free(table); return true; }
    bool conflict = false;
    for (char *cursor = table; cursor < table + length;) {
        struct rt_msghdr2 *message = (struct rt_msghdr2 *)cursor;
        if ((size_t)(table + length - cursor) < sizeof(*message) ||
            message->rtm_msglen < sizeof(*message) || cursor + message->rtm_msglen > table + length) {
            conflict = true; break;
        }
        char *address = (char *)(message + 1);
        uint32_t destination = 0, route_mask = 0;
        bool have_destination = false;
        for (unsigned index = 0; index < RTAX_MAX; index++) {
            if (!(message->rtm_addrs & (1 << index))) continue;
            if (address + 2 > cursor + message->rtm_msglen) { conflict = true; break; }
            struct sockaddr *sa = (struct sockaddr *)address;
            size_t size = sa->sa_len ? (sa->sa_len + sizeof(uint32_t)-1) & ~(sizeof(uint32_t)-1) : sizeof(uint32_t);
            if (address + size > cursor + message->rtm_msglen) { conflict = true; break; }
            struct sockaddr_in ip = {0};
            memcpy(&ip, sa, sa->sa_len < sizeof(ip) ? sa->sa_len : sizeof(ip));
            if (index == RTAX_DST && sa->sa_family == AF_INET) {
                destination = ntohl(ip.sin_addr.s_addr); have_destination = true;
            }
            if (index == RTAX_NETMASK) route_mask = ntohl(ip.sin_addr.s_addr);
            address += size;
        }
        if (conflict) break;
        if (message->rtm_flags & RTF_HOST) route_mask = UINT32_MAX;
        uint32_t common = mask & route_mask;
        if (have_destination && route_mask && (network & common) == (destination & common)) {
            conflict = true; break;
        }
        cursor += message->rtm_msglen;
    }
    free(table);
    return conflict;
}

static bool trusted_self(void) {
    char path[PROC_PIDPATHINFO_MAXSIZE];
    struct stat st;
    if (proc_pidpath(getpid(), path, sizeof(path)) <= 0 || strcmp(path, INSTALL) != 0)
        return false;
    if (lstat(INSTALL, &st) || !S_ISREG(st.st_mode) || st.st_uid || (st.st_mode & 022))
        return false;
    /* A process loaded before an atomic reinstall must not act as the new image.
     * Inspect the vnode backing our own code mapping, not only the current path. */
    struct proc_regionwithpathinfo image;
    int size = proc_pidinfo(getpid(), PROC_PIDREGIONPATHINFO,
                           (uint64_t)(uintptr_t)&trusted_self, &image, sizeof(image));
    if (size != sizeof(image) || image.prp_vip.vip_vi.vi_stat.vst_ino != st.st_ino ||
        image.prp_vip.vip_vi.vi_stat.vst_dev != (uint32_t)st.st_dev) return false;
    if (lstat("/Library/PrivilegedHelperTools", &st) || !S_ISDIR(st.st_mode) ||
        st.st_uid || (st.st_mode & 022)) return false;
    return true;
}

/* Absolute fixed executables, sanitized environment, finite operation budget. */
static int run(char *const argv[]) {
    pid_t child = fork();
    if (child < 0) return -1;
    if (child == 0) {
        char *const env[] = {"PATH=/usr/bin:/bin:/usr/sbin:/sbin", "LANG=C", NULL};
        execve(argv[0], argv, env);
        _exit(127);
    }
    int status;
    for (unsigned i = 0; i < 100; i++) {
        if (waitpid(child, &status, WNOHANG) == child)
            return WIFEXITED(status) ? WEXITSTATUS(status) : -1;
        usleep(100000);
    }
    kill(child, SIGKILL);
    waitpid(child, &status, 0);
    return -1;
}

static int route(const char *operation, char *cidr, char *interface) {
    char *argv[] = {"/sbin/route", "-n", (char *)operation, "-net", cidr,
                    "-interface", interface, NULL};
    return run(argv);
}

static int open_utun(char *name) {
    int fd = socket(PF_SYSTEM, SOCK_DGRAM, SYSPROTO_CONTROL);
    if (fd < 0) return -1;
    fcntl(fd, F_SETFD, FD_CLOEXEC);
    struct ctl_info info = {0};
    strlcpy(info.ctl_name, "com.apple.net.utun_control", sizeof(info.ctl_name));
    struct sockaddr_ctl address = {0};
    address.sc_len = sizeof(address);
    address.sc_family = AF_SYSTEM;
    address.ss_sysaddr = AF_SYS_CONTROL;
    if (ioctl(fd, CTLIOCGINFO, &info)) { close(fd); return -1; }
    address.sc_id = info.ctl_id;
    /* Unit zero asks the kernel for an available interface, never adopts one. */
    if (connect(fd, (struct sockaddr *)&address, sizeof(address))) { close(fd); return -1; }
    socklen_t length = IFNAMSIZ;
    if (getsockopt(fd, SYSPROTO_CONTROL, 2 /* UTUN_OPT_IFNAME */, name, &length)) {
        close(fd); return -1;
    }
    return fd;
}

static int send_descriptor(int client, int fd, const char *name) {
    char control[CMSG_SPACE(sizeof(int))] = {0};
    char body[64];
    snprintf(body, sizeof(body), "%s %s\n", VERSION, name);
    struct iovec iov = {.iov_base = body, .iov_len = strlen(body)};
    struct msghdr message = {0};
    message.msg_iov = &iov;
    message.msg_iovlen = 1;
    message.msg_control = control;
    message.msg_controllen = sizeof(control);
    struct cmsghdr *header = CMSG_FIRSTHDR(&message);
    header->cmsg_level = SOL_SOCKET;
    header->cmsg_type = SCM_RIGHTS;
    header->cmsg_len = CMSG_LEN(sizeof(int));
    memcpy(CMSG_DATA(header), &fd, sizeof(int));
    return sendmsg(client, &message, 0) == (ssize_t)iov.iov_len ? 0 : -1;
}

static int lease(void) {
    struct stat st;
    if (mkdir(STATE, 0711) && errno != EEXIST) return -1;
    if (lstat(STATE, &st) || !S_ISDIR(st.st_mode) || st.st_uid || (st.st_mode & 022))
        return -1;
    int fd = open(LOCK, O_CREAT | O_RDWR | O_NOFOLLOW | O_CLOEXEC, 0600);
    if (fd < 0) return -1;
    if (fstat(fd, &st) || !S_ISREG(st.st_mode) || st.st_uid || st.st_nlink != 1 ||
        (st.st_mode & 077) || flock(fd, LOCK_EX | LOCK_NB)) {
        close(fd); return -1;
    }
    return fd;
}

/* Serialize acquisitions/removal and recheck the actual loaded image after
 * taking the current inode lock. The installer must also honor this lock. */
static int installation_lock(void) {
    int fd = open(INSTALL, O_RDONLY | O_NOFOLLOW | O_CLOEXEC);
    struct stat opened, current;
    if (fd < 0) return -1;
    if (flock(fd, LOCK_EX | LOCK_NB) || fstat(fd, &opened) || lstat(INSTALL, &current) ||
        opened.st_dev != current.st_dev || opened.st_ino != current.st_ino || !trusted_self()) {
        close(fd); return -1;
    }
    return fd;
}

/* Kernel-held descriptors determine liveness, not a reusable PID. With no
 * lease holder, a surviving/reused interface is ambiguous: refuse to alter it.
 * P0 routes disappear with their utun; no foreign route is deleted on recovery. */
static int reconcile(void) {
    int fd = open(JOURNAL, O_RDONLY | O_NOFOLLOW | O_CLOEXEC);
    if (fd < 0) return errno == ENOENT ? 0 : -1;
    struct stat st;
    if (fstat(fd, &st) || !S_ISREG(st.st_mode) || st.st_uid || st.st_nlink != 1 ||
        (st.st_mode & 077) || st.st_size > 4096) { close(fd); return -1; }
    FILE *file = fdopen(fd, "r");
    if (!file) { close(fd); return -1; }
    char line[256], interface[IFNAMSIZ] = {0}, owner[37] = {0};
    struct resolver_identity resolvers[2] = {0};
    unsigned uid = 0;
    int pid = 0;
    bool valid = fgets(line, sizeof(line), file) &&
        sscanf(line, VERSION " uid=%u pid=%d", &uid, &pid) == 2 && uid && pid > 0;
    while (valid && fgets(line, sizeof(line), file)) {
        if (strncmp(line, "interface ", 10) == 0) {
            if (interface[0] || sscanf(line+10, "%15s", interface) != 1 ||
                strncmp(interface, "utun", 4) || !interface[4]) { valid = false; break; }
            for (size_t i = 4; interface[i]; i++) {
                if (interface[i] < '0' || interface[i] > '9') valid = false;
            }
        } else if (strncmp(line, "dns-owner ", 10) == 0) {
            if (owner[0] || sscanf(line+10, "%36s", owner) != 1 || !valid_owner(owner)) valid = false;
        } else if (strncmp(line, "resolver ", 9) == 0) {
            unsigned index;
            unsigned long long device, inode;
            if (!owner[0] || sscanf(line+9, "%u %llu %llu", &index, &device, &inode) != 3 ||
                index >= 2 || resolvers[index].owned) valid = false;
            else resolvers[index] = (struct resolver_identity){.owned=true, .device=(dev_t)device, .inode=(ino_t)inode};
        } else {
            char cidr[CIDR_SIZE];
            uint32_t network, mask;
            if ((sscanf(line, "intent %19s", cidr) != 1 &&
                 sscanf(line, "applying %19s", cidr) != 1) ||
                !valid_cidr(cidr, &network, &mask)) valid = false;
        }
    }
    if (ferror(file)) valid = false;
    fclose(file);
    if (!valid || (interface[0] && if_nametoindex(interface))) return -1;
    if (owner[0]) {
        for (unsigned i = 0; i < 2; i++) {
            if (resolver_remove(owner, i, resolvers[i])) return -1;
        }
    }
    if (lstat(SOCKET, &st) == 0) {
        if (!S_ISSOCK(st.st_mode) || st.st_uid != uid || unlink(SOCKET)) return -1;
    } else if (errno != ENOENT) return -1;
    return unlink(JOURNAL);
}

static int lifecycle(const char *operation) {
    if (geteuid() != 0 || !trusted_self())
        return fail("lifecycle operations require the root-owned installed binary");
    int installed = installation_lock();
    if (installed < 0) return fail("active session or installation generation changed; refused");
    int lock = lease();
    if (lock < 0) { close(installed); return fail("active session or unsafe state; refused"); }
    int result = 0;
    if (reconcile()) result = fail("recovery requires inspection; retained owned journal");
    else if (strcmp(operation, "--uninstall") == 0) {
        /* Unlink the executable first to exclude all new acquisitions. */
        struct stat st;
        if (lstat(SOCKET, &st) == 0 || errno != ENOENT)
            result = fail("unexpected socket; uninstall refused");
        else if (unlink(INSTALL)) result = fail("could not remove installed artifact");
        else if (unlink(LOCK) || rmdir(STATE))
            result = fail("artifact removed; state cleanup incomplete");
        else puts("UNINSTALLED: native artifact and local proof state removed; AWS unchanged");
    } else puts("RECONCILED: no unfinished local proof state");
    close(lock);
    close(installed);
    return result;
}

int main(int argc, char **argv) {
    if (argc == 2 && strcmp(argv[1], "--wait-generation-check") == 0) {
        /* Read-only P0 diagnostic: leave an old image loaded during reinstall.
         * Accept only a release byte, never an operation/path from stdin. */
        struct pollfd input = {.fd = STDIN_FILENO, .events = POLLIN};
        char release;
        if (poll(&input, 1, 300000) <= 0 || read(STDIN_FILENO, &release, 1) != 1 || release != 'G')
            return fail("generation diagnostic release deadline/input refused");
        if (!trusted_self()) return fail("loaded executable generation differs from installation; refused");
        puts("GENERATION PASS: loaded executable matches the trusted installation");
        return 0;
    }
    if (argc == 2 && strcmp(argv[1], "--version") == 0) {
        printf("%s uid=%u\n", VERSION, getuid());
        return 0;
    }
    if (argc == 2 && (strcmp(argv[1], "--reconcile") == 0 ||
                      strcmp(argv[1], "--uninstall") == 0)) return lifecycle(argv[1]);
    bool check = argc > 1 && strcmp(argv[1], "--check") == 0;
    const char *dns_owner = NULL;
    if (argc >= 5 && strcmp(argv[argc-2], "--dns-owner") == 0) {
        dns_owner = argv[argc-1];
        if (!valid_owner(dns_owner)) return fail("DNS proof owner must be a canonical UUID");
        argc -= 2;
        if (check || argc != 4 || strcmp(argv[2], "10.254.0.0/16") ||
            strcmp(argv[3], "10.253.0.0/16")) return fail("DNS requires the fixed two-VPC proof profile");
    }
    if (argc < 3 || (!check && strcmp(argv[1], "--serve")) || argc > MAX_RANGES + 2)
        return fail("usage: --version | --reconcile | --uninstall | --check CIDR [...] | --serve CIDR [...]");
    uint32_t networks[MAX_RANGES], masks[MAX_RANGES];
    for (int i = 2; i < argc; i++) {
        if (!valid_cidr(argv[i], &networks[i-2], &masks[i-2]))
            return fail("invalid CIDR: require canonical RFC1918 IPv4 /16 through /28");
        for (int j = 2; j < i; j++) {
            uint32_t common = masks[i-2] & masks[j-2];
            if ((networks[i-2] & common) == (networks[j-2] & common))
                return fail("overlapping CIDRs");
        }
    }
    for (int i = 2; i < argc; i++) {
        if (route_conflicts(networks[i-2], masks[i-2]))
            return fail("destination overlaps an existing host/LAN/VPN route; no changes made");
    }
    if (route_conflicts(0xc0000201, UINT32_MAX) || route_conflicts(0xc0000202, UINT32_MAX))
        return fail("TEST-NET interface addresses conflict; no changes made");
    if (check) {
        puts("host route preflight PASS (no changes made)"); return 0;
    }
    if (geteuid() != 0 || !trusted_self())
        return fail("run only the root-owned installed native binary via sudo");
    const char *sudo_uid = getenv("SUDO_UID");
    char *end;
    unsigned long uid = sudo_uid ? strtoul(sudo_uid, &end, 10) : 0;
    if (!sudo_uid || *end || uid == 0 || uid > UINT32_MAX)
        return fail("require sudo from a non-root developer account");
    int installed = installation_lock();
    if (installed < 0) return fail("active session or installation changed; refused");
    int lock = lease();
    if (lock < 0) return fail("lease unavailable: another proof is active or state is unsafe");
    /* An interrupted journal is deliberately refused, never silently overwritten.
     * P0 must prove reconciliation before this can become production code. */
    int journal = open(JOURNAL, O_CREAT | O_EXCL | O_WRONLY | O_NOFOLLOW | O_CLOEXEC, 0600);
    if (journal < 0) { close(lock); return fail("journal exists: inspect stale state first"); }
    FILE *record = fdopen(journal, "w");
    if (!record) { close(journal); close(lock); return fail("cannot open journal"); }
    signal(SIGINT, stop_signal);
    signal(SIGTERM, stop_signal);
    signal(SIGPIPE, SIG_IGN);
    int result = 1, listener = -1, client = -1, tun = -1, applied = 0;
    bool socket_owned = false, clean = true;
    struct resolver_identity resolvers[2] = {0};
    char name[IFNAMSIZ] = {0};
    fprintf(record, "%s uid=%lu pid=%d\n", VERSION, uid, getpid());
    for (int i = 2; i < argc; i++) fprintf(record, "intent %s\n", argv[i]);
    if (dns_owner) fprintf(record, "dns-owner %s\n", dns_owner);
    if (fflush(record) || fsync(journal)) goto cleanup;
    tun = open_utun(name);
    if (tun < 0) { fail("utun creation failed"); goto cleanup; }
    fprintf(record, "interface %s\n", name);
    if (fflush(record) || fsync(journal)) goto cleanup;
    char *config[] = {"/sbin/ifconfig", name, "inet", "192.0.2.1", "192.0.2.2",
                      "netmask", "255.255.255.255", "mtu", "1500", "up", NULL};
    if (run(config)) { fail("interface configuration failed"); goto cleanup; }
    listener = socket(AF_UNIX, SOCK_STREAM, 0);
    if (listener < 0) goto cleanup;
    struct sockaddr_un address = {0};
    address.sun_family = AF_UNIX;
    strlcpy(address.sun_path, SOCKET, sizeof(address.sun_path));
    /* A foreign socket is a conflict, not permission to unlink it. */
    if (bind(listener, (struct sockaddr *)&address, sizeof(address))) goto cleanup;
    socket_owned = true;
    if (chmod(SOCKET, 0600) || chown(SOCKET, (uid_t)uid, 0) || listen(listener, 1)) goto cleanup;
    fprintf(stderr, "%s: waiting for uid=%lu at %s (60s)\n", VERSION, uid, SOCKET);
    for (int attempt = 0; attempt < 120 && !stopping; attempt++) {
        struct pollfd ready = {.fd = listener, .events = POLLIN};
        if (poll(&ready, 1, 500) > 0) { client = accept(listener, NULL, NULL); break; }
    }
    if (client < 0) goto cleanup;
    fcntl(client, F_SETFD, FD_CLOEXEC);
    uid_t peer_uid; gid_t peer_gid;
    if (getpeereid(client, &peer_uid, &peer_gid) || peer_uid != uid) {
        fail("caller identity rejected"); goto cleanup;
    }
    if (send_descriptor(client, tun, name)) goto cleanup;
    /* Client must acknowledge stack readiness before any destination is routed. */
    char ack[2];
    size_t received = 0;
    struct timespec began, now;
    clock_gettime(CLOCK_MONOTONIC, &began);
    while (received < sizeof(ack) && !stopping) {
        clock_gettime(CLOCK_MONOTONIC, &now);
        if (now.tv_sec - began.tv_sec >= 10) break;
        struct pollfd ready = {.fd = client, .events = POLLIN};
        if (poll(&ready, 1, 250) <= 0) continue;
        ssize_t n = recv(client, ack + received, sizeof(ack) - received, MSG_DONTWAIT);
        if (n <= 0) break;
        received += (size_t)n;
    }
    if (received != sizeof(ack) || memcmp(ack, "OK", 2)) {
        fail("client did not acknowledge readiness within 10s"); goto cleanup;
    }
    /* A VPN may have appeared while the client prepared its packet stack. */
    for (int i = 2; i < argc; i++) {
        if (route_conflicts(networks[i-2], masks[i-2])) {
            fail("host routes changed during startup; refusing destination conflict");
            goto cleanup;
        }
    }
    for (int i = 2; i < argc; i++) {
        fprintf(record, "applying %s\n", argv[i]);
        if (fflush(record) || fsync(journal)) goto cleanup;
        if (route("add", argv[i], name)) {
            /* A child can commit a route before timing out. Never discard intent
             * or delete a possibly foreign route based only on its exit status. */
            clean = false;
            fail("route installation failed; retain intent for inspection");
            goto cleanup;
        }
        applied++;
    }
    if (dns_owner) {
        for (unsigned i = 0; i < 2; i++) {
            if (resolver_create(dns_owner, i, record, &resolvers[i])) {
                fail("resolver installation failed; existing files are never overwritten");
                goto cleanup;
            }
        }
    }
    if (send(client, "READY\n", 6, 0) != 6) goto cleanup;
    while (!stopping) {
        struct pollfd owner = {.fd = client, .events = POLLIN};
        int status = poll(&owner, 1, 500);
        if (status < 0 && errno == EINTR) continue;
        if (status < 0 || (status > 0 && (owner.revents & (POLLIN | POLLHUP | POLLERR)))) break;
    }
    result = 0;
cleanup:
    /* Client closes its descriptor on EOF; root keeps its copy until rollback. */
    if (client >= 0) { shutdown(client, SHUT_RDWR); close(client); }
    if (dns_owner) {
        for (unsigned i = 0; i < 2; i++) {
            if (resolvers[i].ambiguous) clean = false;
            if (resolvers[i].owned && resolver_remove(dns_owner, i, resolvers[i])) clean = false;
        }
    }
    for (int i = applied - 1; i >= 0; i--) {
        if (route("delete", argv[i+2], name)) clean = false;
    }
    if (tun >= 0) close(tun);
    if (listener >= 0) close(listener);
    if (socket_owned && unlink(SOCKET)) clean = false;
    if (fclose(record)) clean = false;
    if (clean && unlink(JOURNAL)) clean = false;
    close(lock);
    close(installed);
    if (!clean) return fail("cleanup incomplete: retained " JOURNAL);
    return result;
}
