#ifndef STELVIO_TUNNEL_JOURNAL_H
#define STELVIO_TUNNEL_JOURNAL_H

#include <stddef.h>
#include <stdbool.h>

#define STLV_MAX_JOURNAL (512 * 1024)

/* Caller holds the actual installation/lease descriptors for the transaction.
 * Read returns 0 for absent, 1 for a complete snapshot, -1 for uncertainty/error.
 * A successful snapshot precedes host effects; the service additionally validates
 * the payload's schema, session, unit generations and object identities. */
int stlv_journal_read(int image, int lease, void *payload, size_t capacity, size_t *size);
int stlv_journal_write(int image, int lease, const void *payload, size_t size);
int stlv_journal_remove(int image, int lease);
/* Promote only a complete pending snapshot accepted by the typed transition
 * validator. Malformed/incomplete pending bytes remain for explicit recovery. */
typedef bool (*stlv_journal_validator)(const void *previous, size_t previous_size,
                                     const void *pending, size_t pending_size);
int stlv_journal_recover(int image, int lease, stlv_journal_validator validate);

#endif
