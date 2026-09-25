#include "native_input_settings.h"
#include <assert.h>
int main(void)
{
    struct uurb_input_settings settings = {25,25,0};
    struct uurb_input_fraction fraction = {0};
    struct uurb_native_events out;
    uurb_x11_input_event input = {.type=UURB_X11_INPUT_MOUSE,.flags=1,.x=1,.y=-1};
    for (int i=0; i<4; ++i) {
        assert(uurb_native_translate(&input,&out)); uurb_input_scale(&out,&settings,&fraction);
        assert(out.events[0].value==(i==3 ? 1:0)); assert(out.events[1].value==(i==3 ? -1:0));
    }
    assert(!fraction.x && !fraction.y);
    settings.relative_percent=400;
    input.flags=0x8001; input.x=32100; input.y=42000;
    assert(uurb_native_translate(&input,&out)); uurb_input_scale(&out,&settings,&fraction);
    assert(out.events[0].value==32100 && out.events[1].value==42000);
    input.flags=0x800; input.x=input.y=0; input.data=15;
    settings.wheel_percent=200; settings.invert_wheel=1;
    struct uurb_native_scroll scroll={0};
    for(int i=0;i<4;++i) {
        assert(uurb_native_translate(&input,&out)); uurb_input_scale(&out,&settings,&fraction);
        uurb_native_scroll_legacy(&out,&scroll);
        assert(out.events[0].value==-30); assert(out.count==(i==3?3:2));
    }
    assert(out.events[1].code==REL_WHEEL && out.events[1].value==-1);
    assert(!fraction.wheel && !scroll.vertical);
    return 0;
}
