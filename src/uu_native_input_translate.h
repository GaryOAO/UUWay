#ifndef UURB_NATIVE_INPUT_TRANSLATE_H
#define UURB_NATIVE_INPUT_TRANSLATE_H
/* v3 physical-input wire records are shared with the retired X11 transport.
 * Translation here has no X11, Wine, clipboard or RDP dependency. */
#include "x11_input_protocol.h"
#include <linux/input.h>
#include <stddef.h>
#include <string.h>

struct uurb_native_events {
    struct input_event events[20];
    size_t count;
    int keyboard;
};
static inline void uurb_emit(struct uurb_native_events *out, unsigned type, unsigned code, int value)
{
    out->events[out->count++] = (struct input_event){.type = type, .code = code, .value = value};
}
static inline unsigned uurb_evdev_key(unsigned scan, unsigned extended, unsigned vk)
{
    if (vk == 0x13) return KEY_PAUSE;
    if (vk == 0x2c) return KEY_SYSRQ;
    if (!extended) {
        /* Linux input's legacy AT set-1 codes align only in this range. */
        return scan >= 1 && scan <= 88 && scan != 84 ? scan : 0;
    }
    switch (scan) {
    case 0x1c: return KEY_KPENTER;
    case 0x1d: return KEY_RIGHTCTRL;
    case 0x35: return KEY_KPSLASH;
    case 0x37: return KEY_SYSRQ;
    case 0x38: return KEY_RIGHTALT;
    case 0x47: return KEY_HOME;
    case 0x48: return KEY_UP;
    case 0x49: return KEY_PAGEUP;
    case 0x4b: return KEY_LEFT;
    case 0x4d: return KEY_RIGHT;
    case 0x4f: return KEY_END;
    case 0x50: return KEY_DOWN;
    case 0x51: return KEY_PAGEDOWN;
    case 0x52: return KEY_INSERT;
    case 0x53: return KEY_DELETE;
    case 0x5b: return KEY_LEFTMETA;
    case 0x5c: return KEY_RIGHTMETA;
    case 0x5d: return KEY_COMPOSE;
    default: return 0;
    }
}

static inline int uurb_native_translate(const uurb_x11_input_event *in, struct uurb_native_events *out)
{
    memset(out, 0, sizeof(*out));
    unsigned flags = in->flags;
    if (in->type == UURB_X11_INPUT_KEYBOARD) {
        if (flags & ~(UURB_KEYEVENTF_EXTENDED | UURB_KEYEVENTF_KEYUP | UURB_KEYEVENTF_SCANCODE) ||
            in->x || in->y || in->data) return 0;
        unsigned key = uurb_evdev_key(in->scan_code, flags & UURB_KEYEVENTF_EXTENDED, in->virtual_key);
        if (!key) return 0;
        out->keyboard = 1;
        uurb_emit(out, EV_KEY, key, !(flags & UURB_KEYEVENTF_KEYUP));
    } else if (in->type == UURB_X11_INPUT_MOUSE) {
        /* First runtime supports one captured output. The compositor maps the
         * full normalized absolute range; multi-output mapping must negotiate
         * a matching topology before this backend is activated there. */
        const unsigned move = 1, absolute = 0x8000, wheel = 0x800, hwheel = 0x1000;
        const unsigned xdown = 0x80, xup = 0x100;
        if (flags & ~0xf9ffu || in->virtual_key || in->scan_code) return 0;
        if ((flags & (absolute | 0x4000 | 0x2000)) && !(flags & move)) return 0;
        if ((flags & (wheel | hwheel)) && (flags & (xdown | xup))) return 0;
        if ((flags & wheel) && (flags & hwheel)) return 0;
        if (flags & move) {
            if (flags & absolute) {
                if (in->x < 0 || in->x > 65535 || in->y < 0 || in->y > 65535) return 0;
                uurb_emit(out, EV_ABS, ABS_X, in->x); uurb_emit(out, EV_ABS, ABS_Y, in->y);
            } else {
                if (in->x < -32768 || in->x > 32767 || in->y < -32768 || in->y > 32767) return 0;
                uurb_emit(out, EV_REL, REL_X, in->x); uurb_emit(out, EV_REL, REL_Y, in->y);
            }
        } else if (in->x || in->y) return 0;
        const unsigned bits[] = {2, 4, 8, 16, 32, 64};
        const unsigned buttons[] = {BTN_LEFT, BTN_LEFT, BTN_RIGHT, BTN_RIGHT, BTN_MIDDLE, BTN_MIDDLE};
        for (unsigned i = 0; i < 6; ++i) if (flags & bits[i])
            uurb_emit(out, EV_KEY, buttons[i], !(i & 1));
        if (flags & (xdown | xup)) {
            if (!in->data || in->data > 3) return 0;
            for (unsigned i = 0; i < 2; ++i) if (in->data & (1u << i)) {
                if (flags & xdown) uurb_emit(out, EV_KEY, i ? BTN_EXTRA : BTN_SIDE, 1);
                if (flags & xup) uurb_emit(out, EV_KEY, i ? BTN_EXTRA : BTN_SIDE, 0);
            }
        } else if (flags & (wheel | hwheel)) {
            int delta = (int32_t)in->data;
            if (delta < -12000 || delta > 12000) return 0;
            if (delta) uurb_emit(out, EV_REL, flags & wheel ? REL_WHEEL_HI_RES : REL_HWHEEL_HI_RES, delta);
        } else if (in->data) return 0;
    } else return 0;
    uurb_emit(out, EV_SYN, SYN_REPORT, 0);
    return 1;
}

/* Windows and evdev high-resolution wheels both use 120 units per detent.
 * Preserve every fractional report; legacy axes are accumulated separately.
 * Call only after full-batch validation. Commit state after successful write. */
struct uurb_native_scroll { int vertical, horizontal; };
static inline void uurb_native_scroll_legacy(struct uurb_native_events *out, struct uurb_native_scroll *state)
{
    for (size_t i = 0; i < out->count; ++i) {
        struct input_event *event = &out->events[i];
        if (event->type != EV_REL || (event->code != REL_WHEEL_HI_RES && event->code != REL_HWHEEL_HI_RES)) continue;
        int vertical = event->code == REL_WHEEL_HI_RES;
        int *remainder = vertical ? &state->vertical : &state->horizontal;
        int total = *remainder + event->value;
        *remainder = total % 120;
        if (total / 120) {
            --out->count; /* Replace the final SYN, then append a new one. */
            uurb_emit(out, EV_REL, vertical ? REL_WHEEL : REL_HWHEEL, total / 120);
            uurb_emit(out, EV_SYN, SYN_REPORT, 0);
        }
        break;
    }
}
#endif
