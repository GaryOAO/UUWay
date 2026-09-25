#ifndef UURB_NATIVE_TEXT_TRACE_WIN_H
#define UURB_NATIVE_TEXT_TRACE_WIN_H
/* Structural diagnostics only: never emit key values, Unicode values,
 * pointer addresses, text, or payload hashes. */
static inline int uurb_text_trace(unsigned *reports, DWORD count, const INPUT *inputs,
    DWORD delivered, DWORD error, char *line, size_t capacity)
{
    if (*reports >= 128 || !inputs || !count || count > 2048) return 0;
    unsigned unicode = 0, nonzero_vk = 0, bad_flags = 0;
    char kinds[65];
    for (DWORD i = 0; i < count; ++i) {
        const INPUT *in = &inputs[i];
        char kind = 'O';
        if (in->type == INPUT_MOUSE) kind = 'M';
        if (in->type == INPUT_KEYBOARD) {
            BOOL up = !!(in->ki.dwFlags & KEYEVENTF_KEYUP);
            kind = up ? 'k' : 'K';
            if (in->ki.dwFlags & KEYEVENTF_UNICODE) {
                ++unicode;
                nonzero_vk += in->ki.wVk != 0;
                bad_flags += !!(in->ki.dwFlags & ~(KEYEVENTF_UNICODE | KEYEVENTF_KEYUP));
                kind = in->ki.wScan == 8 ? (up ? 'b' : 'B') : (up ? 't' : 'T');
            } else if (in->ki.wVk == VK_BACK ||
                       ((in->ki.dwFlags & KEYEVENTF_SCANCODE) && in->ki.wScan == 0x0e)) {
                kind = up ? 'd' : 'D';
            }
        }
        if (i < sizeof(kinds) - 1) kinds[i] = kind;
    }
    if (!unicode) return 0;
    kinds[count < sizeof(kinds) ? count : sizeof(kinds) - 1] = 0;
    ++*reports;
    int size = snprintf(line, capacity,
        "UURB_NATIVE_TEXT {\"events\":%lu,\"kinds\":\"%s\",\"nonzero_vk\":%u,"
        "\"bad_flags\":%u,\"accepted\":%s,\"error\":%lu}\r\n",
        (unsigned long)count, kinds, nonzero_vk, bad_flags,
        delivered == count ? "true" : "false", (unsigned long)error);
    return size > 0 && (size_t)size < capacity;
}
#endif
