/* Read-only schema harness: no locks, installation or host effects. */
#include "snapshot.h"
#include "journal.h"
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

static uint32_t size_field(const uint8_t *bytes) {
    return ((uint32_t)bytes[0] << 24) | ((uint32_t)bytes[1] << 16) |
           ((uint32_t)bytes[2] << 8) | bytes[3];
}

int main(int argc, char **argv) {
    bool transitions = argc == 2 && !strcmp(argv[1], "--successor");
    if (argc != 1 && !transitions) return 2;
    if (transitions) {
        uint8_t *bytes = malloc(2 * STLV_MAX_JOURNAL + 9);
        struct stlv_snapshot *a = malloc(sizeof(*a)), *b = malloc(sizeof(*b));
        if (!bytes || !a || !b) return 2;
        size_t count = fread(bytes, 1, 2 * STLV_MAX_JOURNAL + 9, stdin);
        int result = 1;
        if (!ferror(stdin) && count >= 8) {
            size_t first = size_field(bytes), second = size_field(bytes + 4);
            if (first <= STLV_MAX_JOURNAL && second <= STLV_MAX_JOURNAL &&
                count == 8 + first + second && stlv_snapshot_decode(bytes + 8, first, a) &&
                stlv_snapshot_decode(bytes + 8 + first, second, b) && stlv_snapshot_successor(a, b)) result = 0;
        }
        free(b); free(a); free(bytes);
        return result;
    }
    uint8_t *input = malloc(STLV_MAX_JOURNAL + 1), *output = malloc(STLV_MAX_JOURNAL);
    struct stlv_snapshot *state = calloc(1, sizeof(*state));
    if (!input || !output || !state) return 2;
    memset(state, 0xa5, sizeof(*state));
    size_t count = fread(input, 1, STLV_MAX_JOURNAL + 1, stdin), size = 0;
    int result = 1;
    if (!ferror(stdin) && stlv_snapshot_decode(input, count, state)) {
        if (stlv_snapshot_encode(state, output, STLV_MAX_JOURNAL, &size) &&
            fwrite(output, 1, size, stdout) == size && !fflush(stdout)) result = 0;
    } else {
        const uint8_t *bytes = (const uint8_t *)state;
        for (size_t i = 0; i < sizeof(*state); i++)
            if (bytes[i]) { result = 2; break; }
    }
    free(state);
    free(output);
    free(input);
    return result;
}
