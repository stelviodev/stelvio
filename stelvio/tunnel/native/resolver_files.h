#ifndef STELVIO_TUNNEL_RESOLVER_FILES_H
#define STELVIO_TUNNEL_RESOLVER_FILES_H

#include "resolver_plan.h"

#define STLV_RESOLVER_DIRECTORY "/private/etc/resolver"

/* Caller holds image/lease exclusion throughout and supplies committed state.
 * Every successful receipt transition is durable before publication/ready.
 * Errors retain evidence and require reload/reconciliation, never blind retry.
 * Shared resolver directory creation/inventory belongs to the installer. */
int stlv_resolver_stage(int image, int lease, struct stlv_snapshot *state,
                        uint8_t unit, uint8_t domain);
int stlv_resolver_publish(int image, int lease, struct stlv_snapshot *state,
                          uint8_t unit, uint8_t domain);
int stlv_resolver_remove(int image, int lease, struct stlv_snapshot *state,
                         uint8_t unit, uint8_t domain);

/* 1 current inode and contents, 0 absent, -1 conflict/error. This is a file
 * ownership check, not evidence that the OS resolver or DNS relay is ready. */
int stlv_resolver_current(int image, int lease, const struct stlv_snapshot *state,
                          uint8_t unit, uint8_t domain);

/* 0 no observed conflict, 1 foreign domain/suffix conflict, -1 uncertainty.
 * Inventory both resolver files and SystemConfiguration. This is a point-in-
 * time observation; ownership and OS/relay readiness must be rechecked. */
int stlv_resolver_conflict(int image, int lease, const struct stlv_snapshot *state,
                            const struct stlv_request *request);

#endif
