#include "resolver_inventory.h"
#include "resolver_domains.h"
#include <string.h>
#include <SystemConfiguration/SystemConfiguration.h>

static int domain(CFTypeRef value, const struct stlv_request *request) {
    char bytes[1024], normalized[STLV_DOMAIN_SIZE];
    if (CFGetTypeID(value) != CFStringGetTypeID() ||
        !CFStringGetCString(value, bytes, sizeof(bytes), kCFStringEncodingUTF8)) return -1;
    /* An empty supplemental domain is the ordinary default resolver. */
    if (!bytes[0]) return 0;
    if (!stlv_dns_domain(bytes, strlen(bytes), normalized)) return -1;
    for (size_t i = 0; i < request->resolver_count; i++)
        if (stlv_dns_overlap(normalized, request->resolvers[i].domain)) return 1;
    return 0;
}

int stlv_system_dns_conflict(const struct stlv_request *request) {
    if (!request || request->operation != STLV_CONFIGURE ||
        request->resolver_count > STLV_MAX_DOMAINS) return -1;
    for (uint8_t i = 0; i < request->resolver_count; i++) {
        char normalized[STLV_DOMAIN_SIZE];
        size_t size = strnlen(request->resolvers[i].domain, STLV_DOMAIN_SIZE);
        if (size >= STLV_DOMAIN_SIZE || !stlv_dns_domain(request->resolvers[i].domain, size, normalized) ||
            strcmp(normalized, request->resolvers[i].domain)) return -1;
    }
    SCDynamicStoreRef store = SCDynamicStoreCreate(NULL, CFSTR("Stelvio DNS inventory"), NULL, NULL);
    if (!store) return -1;
    CFArrayRef keys = SCDynamicStoreCopyKeyList(store,
        CFSTR("^(State|Setup):/Network/(Global|Service/[^/]+)/DNS$"));
    int result = -1;
    if (!keys || CFArrayGetCount(keys) > 4096) goto done;
    result = 0;
    for (CFIndex i = 0; i < CFArrayGetCount(keys) && !result; i++) {
        CFTypeRef key = CFArrayGetValueAtIndex(keys, i);
        if (CFGetTypeID(key) != CFStringGetTypeID()) { result = -1; break; }
        CFPropertyListRef value = SCDynamicStoreCopyValue(store, key);
        if (!value) { result = -1; break; }
        if (CFGetTypeID(value) != CFDictionaryGetTypeID()) result = -1;
        else {
            CFTypeRef one = CFDictionaryGetValue(value, kSCPropNetDNSDomainName);
            if (one) result = domain(one, request);
            CFTypeRef many = CFDictionaryGetValue(value, kSCPropNetDNSSupplementalMatchDomains);
            if (!result && many) {
                if (CFGetTypeID(many) != CFArrayGetTypeID() || CFArrayGetCount(many) > 4096) result = -1;
                else for (CFIndex j = 0; j < CFArrayGetCount(many) && !result; j++)
                    result = domain(CFArrayGetValueAtIndex(many, j), request);
            }
        }
        CFRelease(value);
    }
done:
    if (keys) CFRelease(keys);
    CFRelease(store);
    return result;
}
