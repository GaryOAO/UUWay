#ifndef UURB_NATIVE_META_TRACE_WIN_H
#define UURB_NATIVE_META_TRACE_WIN_H
/* Only logo-key structure/dispatch, never typed text or other key identities. */
static inline int uurb_meta_trace(unsigned *reports, DWORD count, const INPUT *inputs,
    DWORD delivered, char *line, size_t capacity)
{
    if (*reports >= 32) return 0;
    unsigned down = 0, up = 0, missing_extended = 0, packed = 0;
    for (DWORD i = 0; i < count; ++i) {
        const INPUT *input = &inputs[i];
        if (input->type != INPUT_KEYBOARD || (input->ki.dwFlags & KEYEVENTF_UNICODE)) continue;
        unsigned scan = input->ki.wScan & 0xff;
        unsigned key = input->ki.dwFlags & KEYEVENTF_SCANCODE ? scan : input->ki.wVk;
        if (key != VK_LWIN && key != VK_RWIN) continue;
        unsigned bit = key == VK_LWIN ? 1 : 2;
        if (input->ki.dwFlags & KEYEVENTF_KEYUP) up |= bit; else down |= bit;
        if (!(input->ki.dwFlags & KEYEVENTF_EXTENDEDKEY)) ++missing_extended;
        if ((input->ki.wScan & 0xff00) == 0xe000) ++packed;
    }
    if (!down && !up) return 0;
    ++*reports;
    int size = snprintf(line, capacity,
        "UURB_NATIVE_META {\"down_mask\":%u,\"up_mask\":%u,\"missing_extended\":%u,"
        "\"packed_scan\":%u,\"accepted\":%s}\r\n", down, up, missing_extended, packed,
        delivered == count ? "true" : "false");
    return size > 0 && (size_t)size < capacity;
}
#endif
