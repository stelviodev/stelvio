#include "state_store.h"
#include "journal.h"
#include <stdlib.h>
#include <string.h>

static bool accepted(const void *previous, size_t previous_size, const void *pending, size_t pending_size) {
    struct stlv_snapshot *a = calloc(1, sizeof(*a)), *b = calloc(1, sizeof(*b));
    bool result = false;
    if (!a || !b || !stlv_snapshot_decode(pending, pending_size, b)) goto done;
    if (!previous) result = b->revision == 1 && !b->unit_count;
    else if (stlv_snapshot_decode(previous, previous_size, a)) result = stlv_snapshot_successor(a, b);
done:
    free(b); free(a);
    return result;
}

int stlv_state_load(int image, int lease, struct stlv_snapshot *state) {
    if (!state) return -1;
    memset(state, 0, sizeof(*state));
    if (stlv_journal_recover(image, lease, accepted) < 0) return -1;
    void *bytes = malloc(STLV_MAX_JOURNAL);
    if (!bytes) return -1;
    size_t size;
    int result = stlv_journal_read(image, lease, bytes, STLV_MAX_JOURNAL, &size);
    if (result == 1 && !stlv_snapshot_decode(bytes, size, state)) result = -1;
    free(bytes);
    return result;
}

int stlv_state_save(int image, int lease, const struct stlv_snapshot *state) {
    uint8_t *previous = malloc(STLV_MAX_JOURNAL), *pending = malloc(STLV_MAX_JOURNAL);
    int result = -1;
    if (!previous || !pending || stlv_journal_recover(image, lease, accepted) < 0) goto done;
    size_t previous_size, pending_size;
    int old = stlv_journal_read(image, lease, previous, STLV_MAX_JOURNAL, &previous_size);
    if (old < 0 || !stlv_snapshot_encode(state, pending, STLV_MAX_JOURNAL, &pending_size) ||
        !accepted(old ? previous : NULL, previous_size, pending, pending_size)) goto done;
    result = stlv_journal_write(image, lease, pending, pending_size);
done:
    free(pending); free(previous);
    return result;
}
