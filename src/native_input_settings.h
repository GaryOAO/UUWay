#ifndef UURB_NATIVE_INPUT_SETTINGS_H
#define UURB_NATIVE_INPUT_SETTINGS_H
#include "uu_native_input_translate.h"
struct uurb_input_settings { int relative_percent, wheel_percent, invert_wheel; };
struct uurb_input_fraction { int x, y, wheel, hwheel; };
/* Returns 1 on a valid complete file, 0 if absent, -1 on rejection. */
int uurb_input_settings_read(const char *, struct uurb_input_settings *);
static inline void uurb_input_scale(struct uurb_native_events *out,
    const struct uurb_input_settings *settings, struct uurb_input_fraction *fraction)
{
    for (size_t i = 0; i < out->count; ++i) {
        struct input_event *event = &out->events[i];
        if (event->type != EV_REL) continue; /* Absolute coordinates never scaled. */
        int *remainder = NULL, factor = settings->relative_percent;
        switch (event->code) {
        case REL_X: remainder = &fraction->x; break;
        case REL_Y: remainder = &fraction->y; break;
        case REL_WHEEL_HI_RES: remainder = &fraction->wheel; factor = settings->wheel_percent; break;
        case REL_HWHEEL_HI_RES: remainder = &fraction->hwheel; factor = settings->wheel_percent; break;
        default: continue;
        }
        if ((event->code == REL_WHEEL_HI_RES || event->code == REL_HWHEEL_HI_RES) && settings->invert_wheel) factor = -factor;
        int64_t scaled = (int64_t)event->value * factor + *remainder;
        event->value = (int)(scaled / 100); *remainder = (int)(scaled % 100);
    }
}
#endif
