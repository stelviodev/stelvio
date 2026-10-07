/* Read-only schema harness: no locks, installation or host effects. */
#include "snapshot.h"
#include "journal.h"
#include "resolver_plan.h"
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

static uint32_t size_field(const uint8_t *bytes) {
    return ((uint32_t)bytes[0] << 24) | ((uint32_t)bytes[1] << 16) |
           ((uint32_t)bytes[2] << 8) | bytes[3];
}

int main(int argc, char **argv) {
    bool transitions = argc == 2 && !strcmp(argv[1], "--successor");
    bool resolver = argc == 4 && !strcmp(argv[1], "--resolver");
    unsigned long unit = 0, domain = 0;
    if (resolver) {
        char *end;
        unit = strtoul(argv[2], &end, 10);
        if (!argv[2][0] || *end || unit >= STLV_MAX_UNITS) return 2;
        domain = strtoul(argv[3], &end, 10);
        if (!argv[3][0] || *end || domain >= STLV_MAX_DOMAINS) return 2;
    }
    if (argc != 1 && !transitions && !resolver) return 2;
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
        if (resolver) {
            struct stlv_resolver_spec spec;
            if (stlv_resolver_spec(state, (uint8_t)unit, (uint8_t)domain, &spec) &&
                printf("%s\n", spec.public_name) > 0 &&
                fwrite(spec.contents, 1, spec.size, stdout) == spec.size && !fflush(stdout)) result = 0;
            /* Never print the capability-bearing private staging name. */
            memset(&spec, 0, sizeof(spec));
        } else if (stlv_snapshot_encode(state, output, STLV_MAX_JOURNAL, &size) &&
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
