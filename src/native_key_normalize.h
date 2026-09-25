#ifndef UURB_NATIVE_KEY_NORMALIZE_H
#define UURB_NATIVE_KEY_NORMALIZE_H
#include "x11_input_protocol.h"
/* Normalize known Windows logo keys at the Windows -> native boundary.
 * Some remote senders omit EXTENDED on the otherwise unambiguous 5B/5C
 * scans or pack E0 into wScan. Do not infer text keys or alter modifiers. */
static inline int uurb_native_key_normalize(uint32_t virtual_key, uint32_t *scan, uint32_t *flags)
{
    if (!(*flags & UURB_KEYEVENTF_SCANCODE) && (virtual_key == 0x5b || virtual_key == 0x5c)) {
        *scan = virtual_key; *flags |= UURB_KEYEVENTF_EXTENDED;
    }
    if ((*scan & 0xff00u) == 0xe000u) {
        *scan &= 0xffu; *flags |= UURB_KEYEVENTF_EXTENDED;
    }
    if (*scan == 0x5b || *scan == 0x5c) *flags |= UURB_KEYEVENTF_EXTENDED;
    return *scan > 0 && *scan <= 0xff;
}
#endif
