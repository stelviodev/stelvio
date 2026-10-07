#include "resolver_domains.h"
#include <string.h>

bool stlv_dns_domain(const char *input, size_t size, char output[STLV_DOMAIN_SIZE]) {
    if (!output) return false;
    memset(output, 0, STLV_DOMAIN_SIZE);
    if (!input || !size) return false;
    if (input[size - 1] == '.') size--;
    if (!size || size >= STLV_DOMAIN_SIZE) return false;
    size_t label = 0;
    for (size_t i = 0; i < size; i++) {
        unsigned char c = (unsigned char)input[i];
        if (c >= 'A' && c <= 'Z') c += 'a' - 'A';
        if (c == '.') {
            if (!label || input[i - 1] == '-') goto fail;
            label = 0;
        } else {
            if (!((c >= 'a' && c <= 'z') || (c >= '0' && c <= '9') || c == '-') ||
                (!label && c == '-') || ++label > 63) goto fail;
        }
        output[i] = (char)c;
    }
    if (!label || output[size - 1] == '-') goto fail;
    return true;
fail:
    memset(output, 0, STLV_DOMAIN_SIZE);
    return false;
}

static bool suffix(const char *a, const char *b) {
    size_t first = strlen(a), second = strlen(b);
    return first >= second && !strcmp(a + first - second, b) &&
        (first == second || a[first - second - 1] == '.');
}

bool stlv_dns_overlap(const char *a, const char *b) {
    return a && b && a[0] && b[0] && (suffix(a, b) || suffix(b, a));
}

static bool space(char byte) { return byte == ' ' || byte == '\t' || byte == '\r'; }

bool stlv_resolver_domain(const char *name, const char *bytes, size_t size,
                           char output[STLV_DOMAIN_SIZE]) {
    if (!output) return false;
    memset(output, 0, STLV_DOMAIN_SIZE);
    if (!name || (!bytes && size) || size > STLV_RESOLVER_BYTES ||
        (size && memchr(bytes, 0, size))) return false;
    bool explicit = false;
    for (size_t start = 0; start < size;) {
        size_t end = start;
        while (end < size && bytes[end] != '\n' && bytes[end] != '#') end++;
        size_t next = end;
        while (next < size && bytes[next] != '\n') next++;
        while (start < end && space(bytes[start])) start++;
        size_t key = start;
        while (start < end && !space(bytes[start])) start++;
        if (start - key == 6 && !memcmp(bytes + key, "domain", 6)) {
            if (explicit) goto fail;
            while (start < end && space(bytes[start])) start++;
            size_t value = start;
            while (start < end && !space(bytes[start])) start++;
            size_t length = start - value;
            while (start < end && space(bytes[start])) start++;
            if (start != end || !stlv_dns_domain(bytes + value, length, output)) goto fail;
            explicit = true;
        }
        start = next < size ? next + 1 : size;
    }
    return explicit || stlv_dns_domain(name, strlen(name), output);
fail:
    memset(output, 0, STLV_DOMAIN_SIZE);
    return false;
}
