#include "unit.h"
#include "host.h"
#include "resolver_files.h"
#include "state_store.h"
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <errno.h>

static int save_phase(int image, int lease, struct stlv_snapshot *state,
                       uint8_t unit, uint8_t phase, uint64_t generation, bool keep_dns) {
    if (state->revision == UINT64_MAX) return -1;
    struct stlv_snapshot *next = malloc(sizeof(*next));
    if (!next) return -1;
    *next = *state;
    next->revision++;
    next->units[unit].phase = phase;
    next->units[unit].keep_dns = keep_dns;
    next->units[unit].generation = generation;
    int result = stlv_state_save(image, lease, next);
    if (!result) *state = *next;
    free(next);
    return result;
}

static bool same_packet(const struct stlv_unit_snapshot *unit, const uint8_t *packet, size_t size) {
    return unit->packet_size == size && !memcmp(unit->packet, packet, size);
}

static bool live(const struct stlv_unit_snapshot *unit, const struct stlv_interface *interface) {
    return interface->descriptor >= 0 && interface->index == unit->interface_index &&
        stlv_interface_current(interface);
}

static int finish_prepare(int image, int lease, struct stlv_snapshot *state,
                           struct stlv_interface *interface, uint8_t slot) {
    struct stlv_unit_snapshot *unit = &state->units[slot];
    if (!live(unit, interface) || stlv_resolver_conflict(image, lease, state, &unit->configuration)) return -1;
    if (!stlv_interface_configure(interface, slot)) return -1;
    for (uint8_t i = 0; i < unit->configuration.range_count; i++)
        if (!stlv_interface_add_route(interface, &unit->configuration.ranges[i])) return -1;
    for (uint8_t i = 0; i < unit->configuration.resolver_count; i++) {
        if (!unit->files[i].phase && stlv_resolver_stage(image, lease, state, slot, i)) return -1;
        if (unit->files[i].phase == 1 && stlv_resolver_publish(image, lease, state, slot, i)) return -1;
    }
    /* ACTIVE here describes committed host configuration only. The nonroot
     * coordinator still has to prove its relay and actual OS DNS generation. */
    if (stlv_resolver_conflict(image, lease, state, &unit->configuration)) return -1;
    for (uint8_t i = 0; i < unit->configuration.resolver_count; i++)
        if (stlv_resolver_current(image, lease, state, slot, i) != 1) return -1;
    for (uint8_t i = 0; i < unit->configuration.range_count; i++)
        if (stlv_route_status(&unit->configuration.ranges[i], interface->index) != STLV_ROUTE_OWNED) return -1;
    return save_phase(image, lease, state, slot, STLV_ACTIVE, unit->generation, false);
}

int stlv_unit_configure(int image, int lease, struct stlv_snapshot *state,
                        struct stlv_interface interfaces[STLV_MAX_UNITS],
                        const uint8_t *packet, size_t size) {
    struct stlv_request request;
    if (!stlv_trusted_image() || !state || !interfaces || !packet ||
        !stlv_decode(packet, size, &request) || request.operation != STLV_CONFIGURE ||
        /* Reserve a higher generation for cleanup; never accept an unremovable
         * maximal live generation even though the wire codec can represent it. */
        request.generation == UINT64_MAX || state->revision == UINT64_MAX ||
        memcmp(request.session, state->session, 16) ||
        memcmp(request.capability, state->capability, 16) ||
        !stlv_state_current(image, lease, state)) return -1;
    uint8_t slot = 0;
    for (; slot < state->unit_count; slot++)
        if (state->units[slot].configuration.unit == request.unit) break;
    if (slot < state->unit_count && state->units[slot].phase != STLV_REMOVED) {
        struct stlv_unit_snapshot *unit = &state->units[slot];
        if (!same_packet(unit, packet, size) || !live(unit, &interfaces[slot])) return -1;
        if (unit->phase == STLV_PREPARING) return finish_prepare(image, lease, state, &interfaces[slot], slot);
        if (unit->phase != STLV_ACTIVE || stlv_resolver_conflict(image, lease, state, &request)) return -1;
        for (uint8_t i = 0; i < request.resolver_count; i++)
            if (stlv_resolver_current(image, lease, state, slot, i) != 1) return -1;
        for (uint8_t i = 0; i < request.range_count; i++)
            if (stlv_route_status(&request.ranges[i], interfaces[slot].index) != STLV_ROUTE_OWNED) return -1;
        return 0;
    }
    if (slot >= STLV_MAX_UNITS || interfaces[slot].descriptor != -1 ||
        (slot < state->unit_count && request.generation <= state->units[slot].generation) ||
        stlv_resolver_conflict(image, lease, state, &request)) return -1;
    for (uint8_t i = 0; i < request.range_count; i++)
        if (stlv_route_conflict(request.ranges[i].network, request.ranges[i].mask, 0)) return -1;
    struct stlv_snapshot *next = malloc(sizeof(*next));
    if (!next) return -1;
    *next = *state;
    next->revision++;
    if (slot == state->unit_count) next->unit_count++;
    struct stlv_unit_snapshot *unit = &next->units[slot];
    memset(unit, 0, sizeof(*unit));
    unit->phase = STLV_PREPARING;
    unit->generation = request.generation;
    unit->configuration = request;
    unit->packet_size = size;
    memcpy(unit->packet, packet, size);
    /* Creation alone has no persistent effects after this sole FD closes.
     * Commit its diagnostic identity before address/route/resolver changes. */
    int result = -1;
    if (!stlv_interface_open(&interfaces[slot])) goto done;
    unit->interface_index = interfaces[slot].index;
    if (stlv_state_save(image, lease, next)) {
        stlv_interface_close(&interfaces[slot]);
        goto done;
    }
    *state = *next;
    result = finish_prepare(image, lease, state, &interfaces[slot], slot);
done:
    free(next);
    return result;
}

/* Read-only disappearance checks never authorize operations on cached indices.
 * A reused index or foreign replacement route retains cleanup uncertainty. */
static bool detached(const struct stlv_unit_snapshot *unit) {
    struct timespec began, now;
    if (clock_gettime(CLOCK_MONOTONIC, &began)) return false;
    for (;;) {
        char name[IFNAMSIZ];
        errno = 0;
        bool absent = !if_indextoname(unit->interface_index, name) && errno == ENXIO;
        for (uint8_t i = 0; i < unit->configuration.range_count && absent; i++)
            absent = stlv_route_status(&unit->configuration.ranges[i], 0) == STLV_ROUTE_ABSENT;
        if (absent) return true;
        if (clock_gettime(CLOCK_MONOTONIC, &now) ||
            (now.tv_sec - began.tv_sec) * 1000000000LL + now.tv_nsec - began.tv_nsec >= 3000000000LL) return false;
        struct timespec pause = {.tv_nsec=10000000};
        nanosleep(&pause, NULL);
    }
}

int stlv_unit_remove(int image, int lease, struct stlv_snapshot *state,
                     struct stlv_interface interfaces[STLV_MAX_UNITS],
                     uint32_t identity, uint64_t generation, bool keep_dns) {
    if (!stlv_trusted_image() || !state || !interfaces || !generation ||
        (keep_dns && generation == UINT64_MAX) ||
        !stlv_state_current(image, lease, state)) return -1;
    uint8_t slot = 0;
    for (; slot < state->unit_count; slot++)
        if (state->units[slot].configuration.unit == identity) break;
    if (slot == state->unit_count) return -1;
    struct stlv_unit_snapshot *unit = &state->units[slot];
    if (unit->phase == STLV_REMOVED)
        return generation == unit->generation && !keep_dns && interfaces[slot].descriptor == -1 ? 0 : -1;
    if (unit->phase == STLV_RETAINED && keep_dns && generation == unit->generation) {
        if (interfaces[slot].descriptor != -1) return -1;
        for (uint8_t i = 0; i < unit->configuration.resolver_count; i++)
            if (stlv_resolver_current(image, lease, state, slot, i) != 1) return -1;
        return 0;
    }
    if (unit->phase != STLV_REMOVING) {
        if (generation <= unit->generation) return -1;
        if (keep_dns) {
            for (uint8_t i = 0; i < unit->configuration.resolver_count; i++)
                if (unit->files[i].phase != 2) return -1;
        } else if (unit->phase == STLV_PREPARING) {
            /* Receipt any interrupted private stage while PREPARING still
             * permits that transition; public publication is unnecessary. */
            for (uint8_t i = 0; i < unit->configuration.resolver_count; i++)
                if (!unit->files[i].phase && stlv_resolver_stage(image, lease, state, slot, i)) return -1;
        }
        if (save_phase(image, lease, state, slot, STLV_REMOVING, generation, keep_dns)) return -1;
    } else if (unit->keep_dns && !keep_dns && generation > unit->generation) {
        /* A distinct higher-generation disposal can abandon retention even if
         * a public file vanished before RETAINED could be certified. The old
         * generation's policy remains immutable; retries cannot switch it. */
        if (save_phase(image, lease, state, slot, STLV_REMOVING, generation, false)) return -1;
    } else if (generation != unit->generation || unit->keep_dns != keep_dns) return -1;
    if (interfaces[slot].descriptor >= 0) {
        if (!live(unit, &interfaces[slot])) return -1;
        if (!stlv_interface_close(&interfaces[slot])) return -1;
    }
    if (!detached(unit)) return -1;
    /* After restart there is no owned kernel FD to adopt, so cached indices
     * authorize no route operations. Resolver inode receipts remain actionable. */
    if (keep_dns) {
        for (uint8_t i = 0; i < unit->configuration.resolver_count; i++)
            if (stlv_resolver_current(image, lease, state, slot, i) != 1) return -1;
    } else {
        for (uint8_t i = 0; i < unit->configuration.resolver_count; i++)
            if (stlv_resolver_remove(image, lease, state, slot, i)) return -1;
    }
    return save_phase(image, lease, state, slot, keep_dns ? STLV_RETAINED : STLV_REMOVED, generation, keep_dns);
}
