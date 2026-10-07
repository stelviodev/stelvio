#ifndef STELVIO_TUNNEL_JOURNAL_FORMAT_H
#define STELVIO_TUNNEL_JOURNAL_FORMAT_H
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

/* Recognize only a physically incomplete prefix of our journal framing. This
 * is not an ownership check: callers must separately fence the private inode,
 * exclusive lease, installed image and previously committed typed state. */
bool stlv_journal_incomplete(const uint8_t *header, size_t header_size, uint64_t file_size);
#endif
