#include "snapshot.h"
#include "journal.h"
#include <bsm/libbsm.h>
#include <limits.h>
#include <stdlib.h>
#include <string.h>

struct cursor { uint8_t *bytes; size_t size, offset; bool writing; };

static bool transfer(struct cursor *cursor, void *value, size_t size) {
    if (size > cursor->size - cursor->offset) return false;
    if (cursor->writing) memcpy(cursor->bytes + cursor->offset, value, size);
    else memcpy(value, cursor->bytes + cursor->offset, size);
    cursor->offset += size;
    return true;
}

static bool number(struct cursor *cursor, uint64_t *value, size_t size) {
    uint8_t bytes[8];
    if (cursor->writing)
        for (size_t i = 0; i < size; i++) bytes[size - 1 - i] = (uint8_t)(*value >> (8 * i));
    if (!transfer(cursor, bytes, size)) return false;
    if (!cursor->writing) {
        *value = 0;
        for (size_t i = 0; i < size; i++) *value = (*value << 8) | bytes[i];
    }
    return true;
}

static bool nonzero(const uint8_t *bytes, size_t size) {
    uint8_t value = 0;
    for (size_t i = 0; i < size; i++) value |= bytes[i];
    return value != 0;
}

static bool domain_overlap(const char *left, const char *right) {
    size_t a = strlen(left), b = strlen(right);
    if (a < b) return domain_overlap(right, left);
    return !strcmp(left + a - b, right) && (a == b || left[a - b - 1] == '.');
}

static bool validate(struct stlv_snapshot *state) {
    if (!state->revision || !state->peer.uid || state->peer.pid <= 0 ||
        !state->peer.birth_seconds || state->peer.birth_microseconds >= 1000000 ||
        audit_token_to_pid(state->peer.token) != state->peer.pid ||
        audit_token_to_euid(state->peer.token) != state->peer.uid ||
        audit_token_to_ruid(state->peer.token) != state->peer.uid ||
        !nonzero(state->session, 16) || !nonzero(state->capability, 16) ||
        state->carrier_version != 1 || state->unit_count > STLV_MAX_UNITS) return false;
    for (uint8_t i = 0; i < state->unit_count; i++) {
        struct stlv_unit_snapshot *unit = &state->units[i];
        struct stlv_request *configuration = &unit->configuration;
        if (unit->phase < STLV_PREPARING || unit->phase > STLV_REMOVED ||
            unit->keep_dns > 1 ||
            (unit->generation == UINT64_MAX && (unit->keep_dns ||
             unit->phase == STLV_PREPARING || unit->phase == STLV_ACTIVE)) ||
            ((unit->phase == STLV_PREPARING || unit->phase == STLV_ACTIVE || unit->phase == STLV_REMOVED) && unit->keep_dns) ||
            (unit->phase == STLV_RETAINED && !unit->keep_dns) ||
            !unit->generation || !unit->interface_index || unit->interface_index > UINT16_MAX ||
            unit->packet_size > sizeof(unit->packet) ||
            !stlv_decode(unit->packet, unit->packet_size, configuration) ||
            configuration->operation != STLV_CONFIGURE ||
            configuration->generation > unit->generation ||
            memcmp(configuration->session, state->session, 16) ||
            memcmp(configuration->capability, state->capability, 16)) return false;
        if ((unit->phase == STLV_PREPARING || unit->phase == STLV_ACTIVE) &&
            configuration->generation != unit->generation) return false;
        for (uint8_t j = 0; j < configuration->resolver_count; j++) {
            struct stlv_file_receipt *file = &unit->files[j];
            if (file->phase > 2 ||
                (!file->phase && (file->device || file->inode)) ||
                (file->phase && (!file->device || !file->inode)) ||
                (unit->phase == STLV_REMOVED && file->phase) ||
                ((unit->phase == STLV_ACTIVE || unit->phase == STLV_RETAINED ||
                  (unit->phase == STLV_REMOVING && unit->keep_dns)) && file->phase != 2))
                return false;
        }
        for (uint8_t j = 0; j < i; j++) {
            const struct stlv_unit_snapshot *other = &state->units[j];
            const struct stlv_request *previous = &other->configuration;
            /* Tombstones retain unit/VPC identity and the last generation. */
            if (configuration->unit == previous->unit || !strcmp(configuration->vpc, previous->vpc))
                return false;
            if (unit->phase == STLV_REMOVED || other->phase == STLV_REMOVED) continue;
            for (uint8_t a = 0; a < configuration->range_count; a++)
                for (uint8_t b = 0; b < previous->range_count; b++) {
                    uint32_t mask = configuration->ranges[a].mask & previous->ranges[b].mask;
                    if ((configuration->ranges[a].network & mask) ==
                        (previous->ranges[b].network & mask)) return false;
                }
            for (uint8_t a = 0; a < configuration->resolver_count; a++)
                for (uint8_t b = 0; b < previous->resolver_count; b++)
                    if (domain_overlap(configuration->resolvers[a].domain,
                                       previous->resolvers[b].domain)) return false;
        }
    }
    return true;
}

static bool coding(struct cursor *cursor, struct stlv_snapshot *state) {
    uint8_t magic[8] = "STLVSNP3";
    if (!transfer(cursor, magic, 8) || memcmp(magic, "STLVSNP3", 8) ||
        !number(cursor, &state->revision, 8)) return false;
    uint64_t uid = state->peer.uid, pid = (uint32_t)state->peer.pid;
    if (!number(cursor, &uid, 4) || !number(cursor, &pid, 4) || pid > INT32_MAX ||
        !number(cursor, &state->peer.birth_seconds, 8) ||
        !number(cursor, &state->peer.birth_microseconds, 8)) return false;
    state->peer.uid = (uint32_t)uid;
    state->peer.pid = (int32_t)pid;
    for (size_t i = 0; i < 8; i++) {
        uint64_t word = state->peer.token.val[i];
        if (!number(cursor, &word, 4)) return false;
        state->peer.token.val[i] = (uint32_t)word;
    }
    uint64_t carrier_version = state->carrier_version;
    if (!transfer(cursor, state->session, 16) || !transfer(cursor, state->capability, 16) ||
        !number(cursor, &carrier_version, 4) || !transfer(cursor, &state->unit_count, 1) ||
        state->unit_count > STLV_MAX_UNITS) return false;
    state->carrier_version = (uint32_t)carrier_version;
    for (uint8_t i = 0; i < state->unit_count; i++) {
        struct stlv_unit_snapshot *unit = &state->units[i];
        uint64_t length = unit->packet_size;
        uint64_t interface_index = unit->interface_index;
        if (!transfer(cursor, &unit->phase, 1) || !transfer(cursor, &unit->keep_dns, 1) || !number(cursor, &unit->generation, 8) ||
            !number(cursor, &interface_index, 4) ||
            !number(cursor, &length, 4) || length > sizeof(unit->packet)) return false;
        unit->interface_index = (uint32_t)interface_index;
        unit->packet_size = (size_t)length;
        if (!transfer(cursor, unit->packet, unit->packet_size) ||
            !stlv_decode(unit->packet, unit->packet_size, &unit->configuration)) return false;
        for (uint8_t j = 0; j < unit->configuration.resolver_count; j++) {
            struct stlv_file_receipt *file = &unit->files[j];
            if (!transfer(cursor, &file->phase, 1) || !number(cursor, &file->device, 8) ||
                !number(cursor, &file->inode, 8)) return false;
        }
    }
    return validate(state);
}

bool stlv_snapshot_decode(const uint8_t *bytes, size_t size, struct stlv_snapshot *state) {
    if (!state) return false;
    memset(state, 0, sizeof(*state));
    if (!bytes || !size || size > STLV_MAX_JOURNAL) return false;
    struct cursor cursor = {.bytes=(uint8_t *)bytes, .size=size};
    if (!coding(&cursor, state) || cursor.offset != size) {
        memset(state, 0, sizeof(*state));
        return false;
    }
    return true;
}

bool stlv_snapshot_encode(const struct stlv_snapshot *state, uint8_t *bytes,
                          size_t capacity, size_t *size) {
    if (size) *size = 0;
    if (!state || !bytes || !size || !capacity || capacity > STLV_MAX_JOURNAL) return false;
    /* Validation derives configurations from the preserved protocol bytes. */
    struct stlv_snapshot copy = *state;
    if (!validate(&copy)) return false;
    struct cursor cursor = {.bytes=bytes, .size=capacity, .writing=true};
    if (!coding(&cursor, &copy)) return false;
    *size = cursor.offset;
    return true;
}

static bool same_receipt(const struct stlv_file_receipt *a, const struct stlv_file_receipt *b) {
    return a->phase == b->phase && a->device == b->device && a->inode == b->inode;
}

static bool same_unit(const struct stlv_unit_snapshot *a, const struct stlv_unit_snapshot *b) {
    if (a->phase != b->phase || a->keep_dns != b->keep_dns || a->generation != b->generation || a->interface_index != b->interface_index ||
        a->packet_size != b->packet_size || memcmp(a->packet, b->packet, a->packet_size)) return false;
    for (uint8_t i = 0; i < a->configuration.resolver_count; i++)
        if (!same_receipt(&a->files[i], &b->files[i])) return false;
    return true;
}

static bool transition(const struct stlv_unit_snapshot *a, const struct stlv_unit_snapshot *b) {
    if (a->configuration.unit != b->configuration.unit ||
        strcmp(a->configuration.vpc, b->configuration.vpc)) return false;
    if (a->phase == STLV_REMOVED) {
        if (b->phase != STLV_PREPARING || b->generation <= a->generation) return false;
        for (uint8_t i = 0; i < b->configuration.resolver_count; i++)
            if (b->files[i].phase) return false;
        return true;
    }
    if (a->interface_index != b->interface_index) return false;
    if (a->packet_size != b->packet_size || memcmp(a->packet, b->packet, a->packet_size)) return false;
    if (b->phase == STLV_REMOVING &&
        (a->phase != STLV_REMOVING || (a->keep_dns && !b->keep_dns && b->generation > a->generation))) {
        if (b->generation <= a->generation) return false;
        for (uint8_t i = 0; i < a->configuration.resolver_count; i++)
            if (!same_receipt(&a->files[i], &b->files[i])) return false;
        return true;
    }
    if (a->generation != b->generation || a->keep_dns != b->keep_dns) return false;
    if (a->phase == STLV_PREPARING && b->phase == STLV_ACTIVE) {
        /* Journal each link before completing the unit. */
        for (uint8_t i = 0; i < a->configuration.resolver_count; i++)
            if (!same_receipt(&a->files[i], &b->files[i])) return false;
        return true;
    }
    if (a->phase == STLV_REMOVING && (b->phase == STLV_REMOVED || b->phase == STLV_RETAINED)) {
        for (uint8_t i = 0; i < a->configuration.resolver_count; i++)
            if (!same_receipt(&a->files[i], &b->files[i])) return false;
        return true;
    }
    if (a->phase != b->phase || (a->phase != STLV_PREPARING && a->phase != STLV_REMOVING)) return false;
    unsigned changes = 0;
    for (uint8_t i = 0; i < a->configuration.resolver_count; i++) {
        const struct stlv_file_receipt *left = &a->files[i], *right = &b->files[i];
        if (same_receipt(left, right)) continue;
        if (++changes > 1) return false;
        if (a->phase == STLV_PREPARING) {
            if (left->phase == 0 && right->phase == 1) continue;
            if (left->phase == 1 && right->phase == 2 &&
                left->device == right->device && left->inode == right->inode) continue;
            return false;
        }
        if (a->keep_dns || !left->phase || right->phase) return false;
    }
    return changes == 1;
}

bool stlv_snapshot_successor(const struct stlv_snapshot *before, const struct stlv_snapshot *after) {
    if (!before || !after) return false;
    struct stlv_snapshot *a = malloc(sizeof(*a)), *b = malloc(sizeof(*b));
    if (!a || !b) { free(a); free(b); return false; }
    *a = *before; *b = *after;
    bool valid = false;
    if (!validate(a) || !validate(b) || a->revision == UINT64_MAX || b->revision != a->revision + 1 ||
        a->peer.uid != b->peer.uid || a->peer.pid != b->peer.pid ||
        a->peer.birth_seconds != b->peer.birth_seconds || a->peer.birth_microseconds != b->peer.birth_microseconds ||
        memcmp(&a->peer.token, &b->peer.token, sizeof(a->peer.token)) ||
        memcmp(a->session, b->session, 16) || memcmp(a->capability, b->capability, 16) ||
        a->carrier_version != b->carrier_version || b->unit_count < a->unit_count ||
        b->unit_count > a->unit_count + 1) goto done;
    unsigned changes = 0;
    for (uint8_t i = 0; i < a->unit_count; i++) {
        if (same_unit(&a->units[i], &b->units[i])) continue;
        if (++changes > 1 || !transition(&a->units[i], &b->units[i])) goto done;
    }
    if (b->unit_count > a->unit_count) {
        if (changes) goto done;
        const struct stlv_unit_snapshot *unit = &b->units[a->unit_count];
        if (unit->phase != STLV_PREPARING) goto done;
        for (uint8_t i = 0; i < unit->configuration.resolver_count; i++)
            if (unit->files[i].phase) goto done;
        changes++;
    }
    valid = changes == 1;
done:
    free(b); free(a);
    return valid;
}
