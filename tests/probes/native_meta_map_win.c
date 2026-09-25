#define UURB_NATIVE_INPUT_ONLY
#define wWinMain uurb_fixture_unused_main
#include "../../src/uu_input_broker.c"
#undef wWinMain
int wmain(void)
{
    native_only = TRUE;
    for (unsigned side=0;side<2;++side) for (unsigned form=0;form<4;++form) for (unsigned up=0;up<2;++up) {
        INPUT input={.type=INPUT_KEYBOARD};
        input.ki.wVk=form ? 0 : VK_LWIN+side;
        input.ki.wScan=form ? ((form==3 ? 0xe000 : 0) | (VK_LWIN+side)) : 0;
        input.ki.dwFlags=(form ? KEYEVENTF_SCANCODE : 0) | (form==2 ? KEYEVENTF_EXTENDEDKEY : 0) | (up ? KEYEVENTF_KEYUP : 0);
        uurb_x11_input_event out;
        if (!input_to_x11_event(&input,&out) || out.scan_code != VK_LWIN+side ||
            !(out.flags & KEYEVENTF_EXTENDEDKEY) || !!(out.flags & KEYEVENTF_KEYUP)!=up) return 1;
    }
    printf("UURB_META_MAP_PASS left_map=%u right_map=%u input_events_sent=0\n",
           MapVirtualKeyW(VK_LWIN,MAPVK_VK_TO_VSC_EX), MapVirtualKeyW(VK_RWIN,MAPVK_VK_TO_VSC_EX));
    return 0;
}
