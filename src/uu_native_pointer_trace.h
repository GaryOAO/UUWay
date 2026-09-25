#ifndef UURB_NATIVE_POINTER_TRACE_H
#define UURB_NATIVE_POINTER_TRACE_H
/* Bounded diagnostics only: no coordinates, key contents or account data.
 * Called by the single-threaded native broker after the entire batch returns.
 * State is evidence of acknowledged dispatch, not compositor acceptance. */
struct uurb_pointer_trace {
    unsigned held, button_reports, drag_reports;
};
static inline int uurb_pointer_trace_update(struct uurb_pointer_trace *state,
    DWORD count, const INPUT *inputs, DWORD delivered, char *line, size_t capacity)
{
    unsigned down = 0, up = 0, moves = 0, next = state->held;
    for (DWORD i = 0; i < count; ++i) {
        if (inputs[i].type != INPUT_MOUSE) continue;
        DWORD flags = inputs[i].mi.dwFlags;
        const DWORD downs[] = {MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_RIGHTDOWN, MOUSEEVENTF_MIDDLEDOWN};
        const DWORD ups[] = {MOUSEEVENTF_LEFTUP, MOUSEEVENTF_RIGHTUP, MOUSEEVENTF_MIDDLEUP};
        for (unsigned b = 0; b < 3; ++b) {
            unsigned bit = 1u << b;
            if (flags & downs[b]) { down |= bit; next |= bit; }
            if (flags & ups[b]) { up |= bit; next &= ~bit; }
        }
        if (flags & MOUSEEVENTF_MOVE) ++moves;
    }
    int accepted = delivered == count;
    unsigned before = state->held;
    if (accepted) state->held = next;
    else if (delivered) state->held = 0; /* Partial dispatch is not a known hold. */
    int report = 0;
    if ((down || up) && state->button_reports < 256) {
        ++state->button_reports; report = 1;
    } else if (moves && (before || next) && state->drag_reports < 64) {
        ++state->drag_reports; report = 1;
    }
    if (!report) return 0;
    int length = snprintf(line, capacity,
        "UURB_NATIVE_POINTER {\"down_mask\":%u,\"up_mask\":%u,\"held_before\":%u,"
        "\"held_after\":%u,\"moves\":%u,\"accepted\":%s}\r\n",
        down, up, before, state->held, moves, accepted ? "true" : "false");
    return length > 0 && (size_t)length < capacity;
}
#endif
