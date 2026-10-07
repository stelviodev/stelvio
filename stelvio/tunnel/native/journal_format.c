#include "journal_format.h"
#include "journal.h"
#include <string.h>

bool stlv_journal_incomplete(const uint8_t *header, size_t count, uint64_t size) {
    size_t expected = size < 16 ? (size_t)size : 16;
    if ((!header && count) || count != expected || size > STLV_MAX_JOURNAL + 16) return false;
    size_t magic = count < 8 ? count : 8;
    if (magic && memcmp(header, "STLVJNL1", magic)) return false;
    uint32_t length = 0;
    for (size_t i = 8; i < 12; i++)
        length = (length << 8) | (i < count ? header[i] : 0);
    if (length > STLV_MAX_JOURNAL || (count >= 12 && !length)) return false;
    return count < 12 || size < 16 + (uint64_t)length;
}
