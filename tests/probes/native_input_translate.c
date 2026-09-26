#include "uu_native_input_translate.h"
#include <assert.h>
#include <stdio.h>
int main(void)
{
    struct uurb_native_events out;
    uurb_x11_input_event in = {.type = UURB_X11_INPUT_KEYBOARD,
        .flags = UURB_KEYEVENTF_SCANCODE, .scan_code = 0x1e};
    assert(uurb_native_translate(&in, &out));
    assert(out.keyboard && out.count == 2 && out.events[0].code == KEY_A && out.events[0].value == 1);
    in.flags |= UURB_KEYEVENTF_KEYUP;
    assert(uurb_native_translate(&in, &out) && out.events[0].value == 0);
    in.flags = UURB_KEYEVENTF_SCANCODE | UURB_KEYEVENTF_EXTENDED; in.scan_code = 0x4b;
    assert(uurb_native_translate(&in, &out) && out.events[0].code == KEY_LEFT);
    in.flags |= UURB_KEYEVENTF_UNICODE;
    assert(!uurb_native_translate(&in, &out));
    in = (uurb_x11_input_event){.type = UURB_X11_INPUT_MOUSE, .flags = 0xc001, .x = 65535, .y = 0};
    assert(uurb_native_translate(&in, &out) && !out.keyboard && out.count == 3);
    assert(out.events[0].type == EV_ABS && out.events[0].value == 65535);
    in.x = 65536; in.y = -3;
    assert(uurb_native_translate(&in, &out) && out.events[0].value == 65535 && out.events[1].value == 0);
    /* A phone tap: absolute button events without MOVE act where the pointer is. */
    in = (uurb_x11_input_event){.type = UURB_X11_INPUT_MOUSE, .flags = 0xc002, .x = 30000, .y = 20000};
    assert(uurb_native_translate(&in, &out) && out.count == 2);
    assert(out.events[0].type == EV_KEY && out.events[0].code == BTN_LEFT && out.events[0].value == 1);
    in.flags = 0xc004;
    assert(uurb_native_translate(&in, &out) && out.count == 2 && out.events[0].value == 0);
    in.flags = 0xc008;
    assert(uurb_native_translate(&in, &out) && out.events[0].code == BTN_RIGHT);
    in = (uurb_x11_input_event){.type = UURB_X11_INPUT_MOUSE, .flags = 1, .x = -7, .y = 9};
    assert(uurb_native_translate(&in, &out) && out.events[0].type == EV_REL);
    in.flags = 0x800; in.x = in.y = 0; in.data = (uint32_t)-120;
    assert(uurb_native_translate(&in, &out) && out.events[0].code == REL_WHEEL_HI_RES && out.events[0].value == -120);
    struct uurb_native_scroll scroll = {0};
    uurb_native_scroll_legacy(&out, &scroll);
    assert(out.count == 3 && out.events[1].code == REL_WHEEL && out.events[1].value == -1);
    assert(!scroll.vertical && !scroll.horizontal);
    for (unsigned i = 0; i < 8; ++i) {
        in.data = 15;
        assert(uurb_native_translate(&in, &out));
        uurb_native_scroll_legacy(&out, &scroll);
        assert(out.events[0].value == 15 && out.events[0].code == REL_WHEEL_HI_RES);
        assert(out.count == (i == 7 ? 3 : 2));
    }
    assert(out.events[1].code == REL_WHEEL && out.events[1].value == 1 && !scroll.vertical);
    in.data = 1; assert(uurb_native_translate(&in, &out));
    uurb_native_scroll_legacy(&out, &scroll);
    assert(scroll.vertical == 1);
    in.flags = 0x1000; in.data = (uint32_t)-15;
    assert(uurb_native_translate(&in, &out));
    uurb_native_scroll_legacy(&out, &scroll);
    assert(scroll.vertical == 1 && scroll.horizontal == -15);
    in.flags = 0x800; in.data = (uint32_t)-1;
    assert(uurb_native_translate(&in, &out));
    uurb_native_scroll_legacy(&out, &scroll);
    assert(!scroll.vertical && scroll.horizontal == -15);
    in.data = 12001; assert(!uurb_native_translate(&in, &out));
    in.data = 120; in.flags |= 0x80; assert(!uurb_native_translate(&in, &out));
    in.flags = 0x180; in.data = 3;
    assert(uurb_native_translate(&in, &out) && out.count == 5);
    in.flags = 0x400; assert(!uurb_native_translate(&in, &out));
    in = (uurb_x11_input_event){.type = UURB_X11_INPUT_TEXT};
    assert(!uurb_native_translate(&in, &out));
    puts("native physical input translation checks passed");
    return 0;
}
