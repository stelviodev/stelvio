#include "resolver_files.h"
#include "journal.h"
#include "acl.h"
#include "state_store.h"
#include "resolver_domains.h"
#include "resolver_inventory.h"
#include <dirent.h>
#include <errno.h>
#include <fcntl.h>
#include <stdlib.h>
#include <stdio.h>
#include <string.h>
#include <sys/stat.h>
#include <sys/stdio.h>
#include <unistd.h>

static bool same(const struct stat *a, const struct stat *b) {
    return a->st_dev == b->st_dev && a->st_ino == b->st_ino;
}

static int directory(const char *path) {
    int descriptor = open(path, O_RDONLY | O_DIRECTORY | O_NOFOLLOW | O_CLOEXEC);
    if (descriptor < 0) return -1;
    struct stat opened, current;
    if (!stlv_no_acl_fd(descriptor) || fstat(descriptor, &opened) || !S_ISDIR(opened.st_mode) || opened.st_uid ||
        (opened.st_mode & 022) || lstat(path, &current) || !same(&opened, &current)) {
        close(descriptor);
        return -1;
    }
    return descriptor;
}

static bool shape(const struct stat *file) {
    return S_ISREG(file->st_mode) && !file->st_uid &&
        (file->st_nlink == 1 || file->st_nlink == 2) && (file->st_mode & 0777) == 0644;
}

/* 0 absent, 1 exactly owned, -1 uncertainty/foreign. Public contents are complete;
 * private prefixes are permitted only when cleaning a recorded staging inode. */
static int owned(int parent, const char *name, const struct stlv_file_receipt *receipt,
                  const struct stlv_resolver_spec *spec, bool prefix) {
    int descriptor = openat(parent, name, O_RDONLY | O_NONBLOCK | O_NOFOLLOW | O_CLOEXEC);
    if (descriptor < 0) return errno == ENOENT ? 0 : -1;
    struct stat opened, current;
    char contents[sizeof(spec->contents)];
    int result = -1;
    if (!stlv_no_acl_fd(descriptor) || fstat(descriptor, &opened) || !shape(&opened) ||
        (uint64_t)opened.st_dev != receipt->device || (uint64_t)opened.st_ino != receipt->inode ||
        opened.st_size < 0 || (uint64_t)opened.st_size > spec->size ||
        (!prefix && (uint64_t)opened.st_size != spec->size)) goto done;
    size_t size = (size_t)opened.st_size, offset = 0;
    while (offset < size) {
        ssize_t count = read(descriptor, contents + offset, size - offset);
        if (count < 0 && errno == EINTR) continue;
        if (count <= 0) goto done;
        offset += (size_t)count;
    }
    if (memcmp(contents, spec->contents, size) ||
        fstatat(parent, name, &current, AT_SYMLINK_NOFOLLOW) || !same(&opened, &current)) goto done;
    result = 1;
done:
    if (close(descriptor)) result = -1;
    return result;
}

/* The committed PREPARING configuration reserves an unguessable private name
 * before creation. Only a single-link, root-owned canonical prefix at that name
 * can resume a write interrupted before its inode receipt was committed. */
static int pending(int parent, const struct stlv_resolver_spec *spec, size_t *offset) {
    int descriptor = openat(parent, spec->private_name,
        O_RDWR | O_CREAT | O_EXCL | O_NOFOLLOW | O_CLOEXEC, 0644);
    if (descriptor < 0 && errno == EEXIST)
        descriptor = openat(parent, spec->private_name, O_RDWR | O_NONBLOCK | O_NOFOLLOW | O_CLOEXEC);
    if (descriptor < 0) return -1;
    struct stat opened, current;
    char bytes[sizeof(spec->contents)];
    if (!stlv_no_acl_fd(descriptor) || fstat(descriptor, &opened) || !S_ISREG(opened.st_mode) || opened.st_uid ||
        opened.st_nlink != 1 || ((opened.st_mode & 0777) != 0644 &&
        (opened.st_mode & 0777) != 0600) || opened.st_size < 0 ||
        (uint64_t)opened.st_size > spec->size) goto fail;
    *offset = (size_t)opened.st_size;
    for (size_t position = 0; position < *offset;) {
        ssize_t count = read(descriptor, bytes + position, *offset - position);
        if (count < 0 && errno == EINTR) continue;
        if (count <= 0) goto fail;
        position += (size_t)count;
    }
    if (memcmp(bytes, spec->contents, *offset) ||
        fstatat(parent, spec->private_name, &current, AT_SYMLINK_NOFOLLOW) ||
        !same(&opened, &current) || fchmod(descriptor, 0644)) goto fail;
    return descriptor;
fail:
    close(descriptor);
    return -1;
}

static int save_receipt(int image, int lease, struct stlv_snapshot *state, uint8_t unit,
                        uint8_t domain, struct stlv_file_receipt receipt) {
    if (state->revision == UINT64_MAX) return -1;
    struct stlv_snapshot *next = malloc(sizeof(*next));
    if (!next) return -1;
    *next = *state;
    next->revision++;
    next->units[unit].files[domain] = receipt;
    int result = stlv_state_save(image, lease, next);
    if (!result) *state = *next;
    free(next);
    return result;
}

/* The shared public directory may be managed concurrently by other root tools.
 * Move its current entry atomically into our exclusive private namespace, then
 * validate the actual moved inode before deleting anything. A foreign entry
 * is restored exclusively; if restoration collides, retain it and fail rather
 * than overwriting the new public setting or deleting the foreign contents. */
static int unpublish(int private, int public, const struct stlv_resolver_spec *spec,
                      const struct stlv_file_receipt *receipt) {
    char retired[128];
    int length = snprintf(retired, sizeof(retired), "%s.retired", spec->private_name);
    if (length <= 0 || length >= (int)sizeof(retired)) return -1;
    if (!receipt->phase) {
        struct stat unexpected;
        if (!fstatat(private, retired, &unexpected, AT_SYMLINK_NOFOLLOW) || errno != ENOENT) return -1;
        return owned(public, spec->public_name, receipt, spec, false) == 0 ? 0 : -1;
    }
    int moved = owned(private, retired, receipt, spec, false);
    if (!moved) {
        int linked = owned(public, spec->public_name, receipt, spec, false);
        if (linked <= 0) return linked;
        if (renameatx_np(public, spec->public_name, private, retired, RENAME_EXCL)) return -1;
        moved = owned(private, retired, receipt, spec, false);
    }
    if (moved < 0) {
        /* Also covers a crash after a foreign entry was moved but before its
         * verification. Never read/log or adopt the foreign entry's contents. */
        if (renameatx_np(private, retired, public, spec->public_name, RENAME_EXCL)) return -1;
        if (fsync(public) || fsync(private)) return -1;
        return -1;
    }
    if (fsync(public) || fsync(private) || unlinkat(private, retired, 0) || fsync(private)) return -1;
    /* A replacement at the public name belongs to its other writer: retain
     * uncertainty rather than forgetting the conflict or modifying that file. */
    return owned(public, spec->public_name, receipt, spec, false) == 0 ? 0 : -1;
}

int stlv_resolver_stage(int image, int lease, struct stlv_snapshot *state,
                        uint8_t unit, uint8_t domain) {
    struct stlv_resolver_spec spec;
    if (!stlv_trusted_image() || !stlv_resolver_spec(state, unit, domain, &spec) ||
        state->units[unit].phase != STLV_PREPARING || state->units[unit].files[domain].phase ||
        !stlv_state_current(image, lease, state)) return -1;
    int private = directory(STLV_STATE), public = directory(STLV_RESOLVER_DIRECTORY);
    int descriptor = -1, result = -1;
    if (private < 0 || public < 0) goto done;
    struct stat private_state, public_state;
    if (fstat(private, &private_state) || fstat(public, &public_state) ||
        private_state.st_dev != public_state.st_dev) goto done;
    struct stat existing;
    if (!fstatat(public, spec.public_name, &existing, AT_SYMLINK_NOFOLLOW) || errno != ENOENT) goto done;
    size_t offset = 0;
    descriptor = pending(private, &spec, &offset);
    if (descriptor < 0) goto done;
    for (; offset < spec.size;) {
        ssize_t count = write(descriptor, spec.contents + offset, spec.size - offset);
        if (count < 0 && errno == EINTR) continue;
        if (count <= 0) goto done;
        offset += (size_t)count;
    }
    struct stat created;
    if (!stlv_no_acl_fd(descriptor) || fsync(descriptor) || fsync(private) || fstat(descriptor, &created) ||
        !shape(&created) || created.st_nlink != 1 ||
        fstatat(private, spec.private_name, &existing, AT_SYMLINK_NOFOLLOW) || !same(&created, &existing)) goto done;
    struct stlv_file_receipt receipt = {.phase=1, .device=(uint64_t)created.st_dev,
                                       .inode=(uint64_t)created.st_ino};
    result = save_receipt(image, lease, state, unit, domain, receipt);
done:
    if (descriptor >= 0 && close(descriptor)) result = -1;
    if (public >= 0 && close(public)) result = -1;
    if (private >= 0 && close(private)) result = -1;
    /* Retain failed staging under its secret reserved intent name for recovery. */
    memset(&spec, 0, sizeof(spec));
    return result;
}

int stlv_resolver_publish(int image, int lease, struct stlv_snapshot *state,
                          uint8_t unit, uint8_t domain) {
    struct stlv_resolver_spec spec;
    if (!stlv_trusted_image() || !stlv_resolver_spec(state, unit, domain, &spec) ||
        state->units[unit].phase != STLV_PREPARING || state->units[unit].files[domain].phase != 1 ||
        !stlv_state_current(image, lease, state)) return -1;
    int private = directory(STLV_STATE), public = directory(STLV_RESOLVER_DIRECTORY), result = -1;
    if (private < 0 || public < 0) goto done;
    struct stlv_file_receipt receipt = state->units[unit].files[domain];
    int staged = owned(private, spec.private_name, &receipt, &spec, false);
    int linked = owned(public, spec.public_name, &receipt, &spec, false);
    if (staged < 0 || linked < 0 || (!staged && !linked)) goto done;
    if (!linked && linkat(private, spec.private_name, public, spec.public_name, 0)) goto done;
    if (owned(public, spec.public_name, &receipt, &spec, false) != 1 || fsync(public)) goto done;
    if (staged && unlinkat(private, spec.private_name, 0)) goto done;
    if (fsync(private)) goto done;
    receipt.phase = 2;
    result = save_receipt(image, lease, state, unit, domain, receipt);
done:
    if (public >= 0 && close(public)) result = -1;
    if (private >= 0 && close(private)) result = -1;
    memset(&spec, 0, sizeof(spec));
    return result;
}

int stlv_resolver_remove(int image, int lease, struct stlv_snapshot *state,
                         uint8_t unit, uint8_t domain) {
    struct stlv_resolver_spec spec;
    if (!stlv_trusted_image() || !stlv_resolver_spec(state, unit, domain, &spec) ||
        state->units[unit].phase != STLV_REMOVING ||
        !stlv_state_current(image, lease, state)) return -1;
    int private = directory(STLV_STATE), public = directory(STLV_RESOLVER_DIRECTORY), result = -1;
    if (private < 0 || public < 0) goto done;
    struct stlv_file_receipt receipt = state->units[unit].files[domain];
    int staged = owned(private, spec.private_name, &receipt, &spec, true);
    if (staged < 0 || (!receipt.phase && staged)) goto done;
    if (unpublish(private, public, &spec, &receipt)) goto done;
    if (fsync(public)) goto done;
    if (staged && unlinkat(private, spec.private_name, 0)) goto done;
    if (fsync(private)) goto done;
    result = receipt.phase ? save_receipt(image, lease, state, unit, domain,
                                         (struct stlv_file_receipt){0}) : 0;
done:
    if (public >= 0 && close(public)) result = -1;
    if (private >= 0 && close(private)) result = -1;
    memset(&spec, 0, sizeof(spec));
    return result;
}

int stlv_resolver_current(int image, int lease, const struct stlv_snapshot *state,
                          uint8_t unit, uint8_t domain) {
    struct stlv_resolver_spec spec;
    if (!stlv_trusted_image() || !stlv_resolver_spec(state, unit, domain, &spec) ||
        (state->units[unit].phase < STLV_PREPARING || state->units[unit].phase > STLV_RETAINED) ||
        state->units[unit].files[domain].phase != 2 || !stlv_state_current(image, lease, state)) return -1;
    int public = directory(STLV_RESOLVER_DIRECTORY), result = -1;
    if (public >= 0) {
        result = owned(public, spec.public_name, &state->units[unit].files[domain], &spec, false);
        if (close(public)) result = -1;
    }
    memset(&spec, 0, sizeof(spec));
    return result;
}

/* Only receipt-matching public entries are excluded from foreign inventory.
 * A familiar filename alone never establishes ownership. */
static int own_entry(int parent, const char *name, const struct stlv_snapshot *state) {
    for (uint8_t unit = 0; unit < state->unit_count; unit++) {
        for (uint8_t domain = 0; domain < state->units[unit].configuration.resolver_count; domain++) {
            struct stlv_resolver_spec spec;
            if (!stlv_resolver_spec(state, unit, domain, &spec)) return -1;
            bool match = !strcmp(name, spec.public_name);
            memset(spec.private_name, 0, sizeof(spec.private_name));
            if (match) {
                const struct stlv_file_receipt *receipt = &state->units[unit].files[domain];
                if (!receipt->phase) return -1;
                return owned(parent, name, receipt, &spec, false) == 1 ? 1 : -1;
            }
        }
    }
    return 0;
}

static int foreign_domain(int parent, const char *name, const struct stlv_request *request) {
    int descriptor = openat(parent, name, O_RDONLY | O_NONBLOCK | O_NOFOLLOW | O_CLOEXEC);
    if (descriptor < 0) return -1;
    struct stat opened, current;
    char bytes[STLV_RESOLVER_BYTES], domain[STLV_DOMAIN_SIZE];
    int result = -1;
    if (!stlv_no_acl_fd(descriptor) || fstat(descriptor, &opened) || !S_ISREG(opened.st_mode) || opened.st_uid ||
        (opened.st_mode & 022) || opened.st_size < 0 || opened.st_size > STLV_RESOLVER_BYTES) goto done;
    size_t size = (size_t)opened.st_size;
    for (size_t offset = 0; offset < size;) {
        ssize_t count = read(descriptor, bytes + offset, size - offset);
        if (count < 0 && errno == EINTR) continue;
        if (count <= 0) goto done;
        offset += (size_t)count;
    }
    if (fstatat(parent, name, &current, AT_SYMLINK_NOFOLLOW) || !same(&opened, &current) ||
        !stlv_resolver_domain(name, bytes, size, domain)) goto done;
    result = 0;
    for (uint8_t i = 0; i < request->resolver_count; i++)
        if (stlv_dns_overlap(domain, request->resolvers[i].domain)) { result = 1; break; }
done:
    if (close(descriptor)) result = -1;
    return result;
}

int stlv_resolver_conflict(int image, int lease, const struct stlv_snapshot *state,
                            const struct stlv_request *request) {
    if (!stlv_trusted_image() || !state || !request || request->operation != STLV_CONFIGURE ||
        request->resolver_count > STLV_MAX_DOMAINS || !stlv_state_current(image, lease, state)) return -1;
    for (uint8_t i = 0; i < request->resolver_count; i++) {
        char normalized[STLV_DOMAIN_SIZE];
        size_t size = strnlen(request->resolvers[i].domain, STLV_DOMAIN_SIZE);
        if (size >= STLV_DOMAIN_SIZE || !stlv_dns_domain(request->resolvers[i].domain, size, normalized) ||
            strcmp(normalized, request->resolvers[i].domain)) return -1;
    }
    int result = stlv_system_dns_conflict(request);
    if (result) return result;
    int parent = directory(STLV_RESOLVER_DIRECTORY);
    if (parent < 0) return -1;
    DIR *entries = fdopendir(parent);
    if (!entries) { close(parent); return -1; }
    size_t count = 0;
    for (;;) {
        errno = 0;
        struct dirent *entry = readdir(entries);
        if (!entry) { if (errno) result = -1; break; }
        if (!strcmp(entry->d_name, ".") || !strcmp(entry->d_name, "..")) continue;
        if (++count > 4096) { result = -1; break; }
        int ours = own_entry(parent, entry->d_name, state);
        if (ours < 0) { result = -1; break; }
        if (!ours) result = foreign_domain(parent, entry->d_name, request);
        if (result) break;
    }
    if (closedir(entries)) result = -1;
    return result;
}
