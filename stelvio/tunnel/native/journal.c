/* Atomic root-only snapshots with bounded reads, fsync and inode/lock fencing. */
#include "journal.h"
#include "journal_format.h"
#include "ownership.h"
#include "acl.h"
#include <errno.h>
#include <fcntl.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/file.h>
#include <sys/stat.h>
#include <unistd.h>

#define HEADER_SIZE 16
#define NEXT "journal.next"

static bool regular(const struct stat *state) {
    return S_ISREG(state->st_mode) && !state->st_uid && state->st_nlink == 1 &&
           (state->st_mode & 0777) == 0600;
}

static bool same(const struct stat *left, const struct stat *right) {
    return left->st_dev == right->st_dev && left->st_ino == right->st_ino;
}

static bool lock_matches(int descriptor, const char *path, bool private) {
    struct stat opened, current;
    return descriptor >= 0 && stlv_no_acl_fd(descriptor) && !fstat(descriptor, &opened) &&
        S_ISREG(opened.st_mode) && !opened.st_uid && opened.st_nlink == 1 &&
        !(opened.st_mode & (private ? 077 : 022)) && !lstat(path, &current) &&
        same(&opened, &current) && !flock(descriptor, LOCK_EX | LOCK_NB);
}

static int directory(int image, int lease) {
    if (!stlv_trusted_image() || !lock_matches(image, STLV_INSTALL, false) ||
        !lock_matches(lease, STLV_LEASE, true)) return -1;
    int descriptor = open(STLV_STATE, O_RDONLY | O_DIRECTORY | O_NOFOLLOW | O_CLOEXEC);
    if (descriptor < 0) return -1;
    if (!stlv_no_acl_fd(descriptor)) { close(descriptor); return -1; }
    struct stat opened, current;
    if (fstat(descriptor, &opened) || !S_ISDIR(opened.st_mode) || opened.st_uid ||
        (opened.st_mode & 022) || lstat(STLV_STATE, &current) || !same(&opened, &current)) {
        close(descriptor);
        return -1;
    }
    return descriptor;
}

static uint32_t checksum(const uint8_t *data, size_t size) {
    uint32_t value = 2166136261u;
    for (size_t i = 0; i < size; i++) value = (value ^ data[i]) * 16777619u;
    return value;
}

static void put_u32(uint8_t *destination, uint32_t value) {
    destination[0] = (uint8_t)(value >> 24);
    destination[1] = (uint8_t)(value >> 16);
    destination[2] = (uint8_t)(value >> 8);
    destination[3] = (uint8_t)value;
}

static uint32_t get_u32(const uint8_t *value) {
    return ((uint32_t)value[0] << 24) | ((uint32_t)value[1] << 16) |
           ((uint32_t)value[2] << 8) | value[3];
}

static int transfer(int descriptor, void *buffer, size_t size, bool writing) {
    uint8_t *bytes = buffer;
    for (size_t offset = 0; offset < size;) {
        ssize_t count = writing ? write(descriptor, bytes + offset, size - offset) :
                                  read(descriptor, bytes + offset, size - offset);
        if (count < 0 && errno == EINTR) continue;
        if (count <= 0) return -1;
        offset += (size_t)count;
    }
    return 0;
}

static int read_snapshot(int parent, const char *name, void *payload, size_t capacity, size_t *size) {
    *size = 0;
    int descriptor = openat(parent, name, O_RDONLY | O_NONBLOCK | O_NOFOLLOW | O_CLOEXEC);
    if (descriptor < 0) return errno == ENOENT ? 0 : -1;
    struct stat opened, current;
    uint8_t header[HEADER_SIZE];
    int result = -1;
    if (!stlv_no_acl_fd(descriptor) || fstat(descriptor, &opened) || !regular(&opened) ||
        opened.st_size < HEADER_SIZE || opened.st_size > STLV_MAX_JOURNAL + HEADER_SIZE ||
        transfer(descriptor, header, sizeof(header), false) || memcmp(header, "STLVJNL1", 8)) goto done;
    size_t length = get_u32(header + 8);
    if (!length || length > capacity || length > STLV_MAX_JOURNAL ||
        (uint64_t)opened.st_size != HEADER_SIZE + length ||
        transfer(descriptor, payload, length, false) ||
        get_u32(header + 12) != checksum(payload, length) ||
        fstatat(parent, name, &current, AT_SYMLINK_NOFOLLOW) || !same(&opened, &current)) goto done;
    *size = length;
    result = 1;
done:
    if (close(descriptor)) result = -1;
    return result;
}

int stlv_journal_read(int image, int lease, void *payload, size_t capacity, size_t *size) {
    if (!payload || !size || !capacity || capacity > STLV_MAX_JOURNAL) return -1;
    int parent = directory(image, lease);
    if (parent < 0) return -1;
    int result = read_snapshot(parent, "journal", payload, capacity, size);
    if (close(parent)) result = -1;
    return result;
}

int stlv_journal_write(int image, int lease, const void *payload, size_t size) {
    if (!payload || !size || size > STLV_MAX_JOURNAL) return -1;
    int parent = directory(image, lease);
    if (parent < 0) return -1;
    /* Existing regular mode/owner alone never authorizes replacing malformed
     * or unrelated contents at the reserved snapshot path. */
    void *previous = malloc(STLV_MAX_JOURNAL);
    if (!previous) { close(parent); return -1; }
    size_t previous_size;
    int previous_result = read_snapshot(parent, "journal", previous, STLV_MAX_JOURNAL, &previous_size);
    free(previous);
    if (previous_result < 0) { close(parent); return -1; }
    /* A previous incomplete next snapshot is never overwritten/adopted. The
     * service recovery path must inspect it under the same exclusion locks. */
    int descriptor = openat(parent, NEXT, O_WRONLY | O_CREAT | O_EXCL | O_NOFOLLOW | O_CLOEXEC, 0600);
    if (descriptor < 0) { close(parent); return -1; }
    struct stat created, current;
    uint8_t header[HEADER_SIZE] = "STLVJNL1";
    put_u32(header + 8, (uint32_t)size);
    put_u32(header + 12, checksum(payload, size));
    int result = -1;
    if (!stlv_no_acl_fd(descriptor) || fstat(descriptor, &created) || !regular(&created) ||
        transfer(descriptor, header, sizeof(header), true) ||
        transfer(descriptor, (void *)payload, size, true) || fsync(descriptor) ||
        fstatat(parent, NEXT, &current, AT_SYMLINK_NOFOLLOW) || !same(&created, &current)) goto done;
    /* Refuse a foreign-shaped replacement target instead of overwriting it. */
    if (!fstatat(parent, "journal", &current, AT_SYMLINK_NOFOLLOW)) {
        if (!regular(&current)) goto done;
    } else if (errno != ENOENT) goto done;
    if (renameat(parent, NEXT, parent, "journal") || fsync(parent)) goto done;
    result = 0;
done:
    if (close(descriptor)) result = -1;
    if (close(parent)) result = -1;
    return result;
}

int stlv_journal_remove(int image, int lease) {
    int parent = directory(image, lease);
    if (parent < 0) return -1;
    struct stat pending;
    if (!fstatat(parent, NEXT, &pending, AT_SYMLINK_NOFOLLOW) || errno != ENOENT) {
        close(parent);
        return -1;
    }
    void *payload = malloc(STLV_MAX_JOURNAL);
    if (!payload) { close(parent); return -1; }
    size_t size;
    int result = read_snapshot(parent, "journal", payload, STLV_MAX_JOURNAL, &size);
    free(payload);
    if (result == 1) result = unlinkat(parent, "journal", 0) ? -1 : 0;
    if (result == 0 && fsync(parent)) result = -1;
    if (close(parent)) result = -1;
    return result;
}

/* Incomplete .next bytes have never authorized host effects: only publication
 * of a complete committed snapshot does. Dispose only a recognized framing
 * prefix inside the exclusive root namespace, retaining the committed state. */
static int discard_incomplete(int parent) {
    int descriptor = openat(parent, NEXT, O_RDONLY | O_NONBLOCK | O_NOFOLLOW | O_CLOEXEC);
    if (descriptor < 0) return -1;
    if (!stlv_no_acl_fd(descriptor)) { close(descriptor); return -1; }
    struct stat opened, current;
    uint8_t header[HEADER_SIZE];
    int result = -1;
    if (!stlv_no_acl_fd(descriptor) || fstat(descriptor, &opened) || !regular(&opened) || opened.st_size < 0 ||
        opened.st_size > STLV_MAX_JOURNAL + HEADER_SIZE) goto done;
    size_t count = opened.st_size < HEADER_SIZE ? (size_t)opened.st_size : HEADER_SIZE;
    if (transfer(descriptor, header, count, false) ||
        !stlv_journal_incomplete(header, count, (uint64_t)opened.st_size) ||
        fstatat(parent, NEXT, &current, AT_SYMLINK_NOFOLLOW) || !same(&opened, &current)) goto done;
    if (unlinkat(parent, NEXT, 0) || fsync(parent)) goto done;
    result = 0;
done:
    if (close(descriptor)) result = -1;
    return result;
}

int stlv_journal_recover(int image, int lease, stlv_journal_validator validate) {
    if (!validate) return -1;
    int parent = directory(image, lease);
    if (parent < 0) return -1;
    void *previous = malloc(STLV_MAX_JOURNAL), *pending = malloc(STLV_MAX_JOURNAL);
    int result = -1;
    if (!previous || !pending) goto done;
    size_t previous_size, pending_size;
    int old = read_snapshot(parent, "journal", previous, STLV_MAX_JOURNAL, &previous_size);
    int next = read_snapshot(parent, NEXT, pending, STLV_MAX_JOURNAL, &pending_size);
    if (old < 0) goto done;
    if (next < 0) {
        if (!validate(old ? previous : NULL, previous_size, NULL, 0)) goto done;
        result = discard_incomplete(parent);
        goto done;
    }
    /* A prior rename may have succeeded before its directory sync failed. */
    if (!next) { result = fsync(parent) ? -1 : 0; goto done; }
    if (!validate(old ? previous : NULL, previous_size, pending, pending_size)) goto done;
    /* Recheck and sync complete bytes immediately before publishing. */
    int descriptor = openat(parent, NEXT, O_RDONLY | O_NONBLOCK | O_NOFOLLOW | O_CLOEXEC);
    if (descriptor < 0) goto done;
    struct stat opened, current;
    bool ready = stlv_no_acl_fd(descriptor) && !fstat(descriptor, &opened) && regular(&opened) &&
        !fstatat(parent, NEXT, &current, AT_SYMLINK_NOFOLLOW) && same(&opened, &current) &&
        !fsync(descriptor);
    if (close(descriptor)) ready = false;
    if (!ready || renameat(parent, NEXT, parent, "journal") || fsync(parent)) goto done;
    result = 1;
done:
    free(pending); free(previous);
    if (close(parent)) result = -1;
    return result;
}
