/* Bounded, read-only packet grant validator; never opens a host interface. */
#include "packet.h"
#include <stdio.h>
#include <stdlib.h>

static uint32_t word(const uint8_t *bytes) {
    return ((uint32_t)bytes[0] << 24) | ((uint32_t)bytes[1] << 16) |
           ((uint32_t)bytes[2] << 8) | bytes[3];
}

int main(void) {
    size_t capacity = 9 + STLV_HEADER + STLV_MAX_BODY + STLV_MAX_PACKET;
    uint8_t *input = malloc(capacity + 1);
    struct stlv_request *configuration = malloc(sizeof(*configuration));
    if (!input || !configuration) return 2;
    size_t count = fread(input, 1, capacity + 1, stdin);
    int result = 1;
    if (!ferror(stdin) && count >= 9 && count <= capacity) {
        size_t config_size = word(input);
        if (config_size <= STLV_HEADER + STLV_MAX_BODY && config_size <= count - 9 && input[8] <= 1 &&
            stlv_decode(input + 9, config_size, configuration) &&
            stlv_packet_allowed(input + 9 + config_size, count - 9 - config_size,
                                configuration, word(input + 4), input[8] != 0)) result = 0;
    }
    free(configuration); free(input);
    return result;
}
