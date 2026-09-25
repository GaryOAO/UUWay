#define UURB_NATIVE_INPUT_ONLY
#define wWinMain uurb_fixture_unused_main
#include "../../src/uu_input_broker.c"
#undef wWinMain
#include <assert.h>
int wmain(void)
{
    INPUT in[24] = {0};
    uurb_x11_input_event out[24]; DWORD used;
    const WCHAR known[] = L"中文🙂";
    for (unsigned i=0; known[i]; ++i) {
        in[2*i] = (INPUT){.type=INPUT_KEYBOARD, .ki={.wScan=known[i], .dwFlags=KEYEVENTF_UNICODE}};
        in[2*i+1] = in[2*i]; in[2*i+1].ki.dwFlags |= KEYEVENTF_KEYUP;
    }
    assert(normalize_native_text_inputs(20,in,out,&used) && used==4);
    for (unsigned i=0;i<used;++i) assert(out[i].type==UURB_X11_INPUT_TEXT && out[i].data==known[i]);
    for (unsigned field=0;field<6;++field) {
        INPUT bad[24]; memcpy(bad,in,sizeof(in));
        switch(field) {
        case 0:bad[19].mi.dx=1;break;
        case 1:bad[19].mi.dy=1;break;
        case 2:bad[19].mi.mouseData=1;break;
        case 3:bad[19].mi.dwFlags=MOUSEEVENTF_LEFTDOWN;break;
        case 4:bad[19].mi.time=1;break;
        case 5:bad[19].mi.dwExtraInfo=1;break;
        }
        used=99;
        assert(!normalize_native_text_inputs(20,bad,out,&used) && used==99);
    }
    in[8]=(INPUT){.type=INPUT_KEYBOARD,.ki={.wVk=VK_BACK}};
    in[9]=in[8];in[9].ki.dwFlags=KEYEVENTF_KEYUP;
    assert(normalize_native_text_inputs(20,in,out,&used) && used==5 && out[4].data==8);
    puts("NATIVE_TEXT_BATCH_PASS CJK surrogate padding real_mouse_refused no_input_sent");
    return 0;
}
