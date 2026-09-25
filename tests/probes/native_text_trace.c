#include <assert.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
typedef uint32_t DWORD;
typedef int BOOL;
typedef struct { DWORD type; struct { unsigned wVk, wScan, dwFlags; } ki; } INPUT;
#define INPUT_MOUSE 0
#define INPUT_KEYBOARD 1
#define KEYEVENTF_KEYUP 2
#define KEYEVENTF_UNICODE 4
#define KEYEVENTF_SCANCODE 8
#define VK_BACK 8
#include "uu_native_text_trace_win.h"
int main(void)
{
    INPUT batch[] = {{1,{0,0x4e2d,4}}, {1,{0,0x4e2d,6}}, {1,{8,0,0}}, {1,{8,0,2}}};
    char a[512], b[512]; unsigned reports = 0;
    assert(uurb_text_trace(&reports, 4, batch, 0, 50, a, sizeof(a)));
    assert(strstr(a, "\"kinds\":\"TtDd\""));
    batch[0].ki.wScan = batch[1].ki.wScan = 'Z';
    assert(uurb_text_trace(&reports, 4, batch, 0, 50, b, sizeof(b)));
    assert(!strcmp(a,b)); /* Payload changes cannot appear in diagnostics. */
    assert(!uurb_text_trace(&reports, 4, batch, 0, 50, b, 2));
    reports = 128;
    assert(!uurb_text_trace(&reports, 4, batch, 0, 50, b, sizeof(b)));
    assert(!uurb_text_trace(&reports, 1, NULL, 0, 50, b, sizeof(b)));
    puts("NATIVE_TEXT_TRACE_PASS bounded content-independent event classes");
}
