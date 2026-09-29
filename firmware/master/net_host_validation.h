#pragma once

#include <stdbool.h>
#include <stddef.h>

/* The Station host is a local IPv4 address, without scheme or port. */
static inline bool gs_is_valid_ipv4(const char *host)
{
    unsigned int octets = 0u;
    unsigned int digits = 0u;
    unsigned int value = 0u;

    if (host == NULL || host[0] == '\0') return false;
    for (const char *p = host; *p != '\0'; ++p) {
        if (*p >= '0' && *p <= '9') {
            if (digits >= 3u) return false;
            value = value * 10u + (unsigned int)(*p - '0');
            if (value > 255u) return false;
            ++digits;
        } else if (*p == '.' && digits > 0u && octets < 3u) {
            ++octets;
            digits = 0u;
            value = 0u;
        } else {
            return false;
        }
    }
    return octets == 3u && digits > 0u;
}
