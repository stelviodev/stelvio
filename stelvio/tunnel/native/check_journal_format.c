/* Pure framing classifier; no filesystem access or privileged operations. */
#include "journal_format.h"
#include <stdio.h>

int main(void) {
    uint8_t bytes[25];
    size_t count = fread(bytes, 1, sizeof(bytes), stdin);
    if (ferror(stdin) || count < 8 || count > 24) return 2;
    uint64_t size = 0;
    for (size_t i = 0; i < 8; i++) size = (size << 8) | bytes[i];
    return stlv_journal_incomplete(bytes + 8, count - 8, size) ? 0 : 1;
}
