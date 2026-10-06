/* Read-only schema harness: no locks, installation or host effects. */
#include "snapshot.h"
#include "journal.h"
#include <stdio.h>
#include <stdlib.h>

int main(void) {
    uint8_t *input = malloc(STLV_MAX_JOURNAL + 1), *output = malloc(STLV_MAX_JOURNAL);
    struct stlv_snapshot *state = calloc(1, sizeof(*state));
    if (!input || !output || !state) return 2;
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
