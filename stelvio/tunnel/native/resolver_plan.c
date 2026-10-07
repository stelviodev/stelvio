#include "resolver_plan.h"
#include <inttypes.h>
#include <stdio.h>
#include <string.h>

static void hex(const uint8_t bytes[16], char result[33]) {
    static const char digits[] = "0123456789abcdef";
    for (size_t i = 0; i < 16; i++) {
        result[2 * i] = digits[bytes[i] >> 4];
        result[2 * i + 1] = digits[bytes[i] & 15];
    }
    result[32] = 0;
}

bool stlv_resolver_spec(const struct stlv_snapshot *state, uint8_t unit,
                        uint8_t domain, struct stlv_resolver_spec *spec) {
    if (!spec) return false;
    memset(spec, 0, sizeof(*spec));
    if (!state || state->carrier_version != 1 || state->unit_count > STLV_MAX_UNITS ||
        unit >= state->unit_count) return false;
    const struct stlv_unit_snapshot *owner = &state->units[unit];
    struct stlv_request configuration;
    if (owner->packet_size > sizeof(owner->packet) ||
        !stlv_decode(owner->packet, owner->packet_size, &configuration) ||
        configuration.operation != STLV_CONFIGURE ||
        configuration.generation > owner->generation ||
        memcmp(configuration.session, state->session, 16) ||
        memcmp(configuration.capability, state->capability, 16) ||
        domain >= configuration.resolver_count) return false;
    char session[33], capability[33];
    hex(state->session, session);
    hex(state->capability, capability);
    int private_size = snprintf(spec->private_name, sizeof(spec->private_name),
        "pending.%s.%s.%08x.%016" PRIx64 ".%02x", capability, session,
        configuration.unit, configuration.generation, domain);
    int public_size = snprintf(spec->public_name, sizeof(spec->public_name),
        "stelvio.%s.%08x.%016" PRIx64 ".%02x", session,
        configuration.unit, configuration.generation, domain);
    const struct stlv_resolver *endpoint = &configuration.resolvers[domain];
    int content_size = snprintf(spec->contents, sizeof(spec->contents),
        "# Stelvio tunnel/1 session=%s unit=%08x generation=%" PRIu64 "\n"
        "domain %s\nnameserver 127.0.0.1\nport %u\nsearch_order 0\n",
        session, configuration.unit, configuration.generation, endpoint->domain, endpoint->port);
    memset(capability, 0, sizeof(capability));
    if (private_size <= 0 || private_size >= (int)sizeof(spec->private_name) ||
        public_size <= 0 || public_size >= (int)sizeof(spec->public_name) ||
        content_size <= 0 || content_size >= (int)sizeof(spec->contents)) {
        memset(spec, 0, sizeof(*spec));
        return false;
    }
    spec->size = (size_t)content_size;
    return true;
}
