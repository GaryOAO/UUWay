#include <assert.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
/* Minimal INPUT view for platform-independent trace policy checks; the actual
 * Windows layout is compiled by build-uu-native-input.sh against windows.h. */
typedef uint32_t DWORD;
typedef struct { DWORD type; struct { DWORD dwFlags; } mi; } INPUT;
#define INPUT_MOUSE 0
#define MOUSEEVENTF_MOVE 1
#define MOUSEEVENTF_LEFTDOWN 2
#define MOUSEEVENTF_LEFTUP 4
#define MOUSEEVENTF_RIGHTDOWN 8
#define MOUSEEVENTF_RIGHTUP 16
#define MOUSEEVENTF_MIDDLEDOWN 32
#define MOUSEEVENTF_MIDDLEUP 64
#include "../../src/uu_native_pointer_trace.h"
int main(void)
{
    struct uurb_pointer_trace state = {0};
    char line[512];
    INPUT input = {.type = INPUT_MOUSE, .mi.dwFlags = MOUSEEVENTF_LEFTDOWN};
    assert(uurb_pointer_trace_update(&state, 1, &input, 1, line, sizeof(line)) && state.held == 1);
    input.mi.dwFlags = MOUSEEVENTF_MOVE;
    assert(uurb_pointer_trace_update(&state, 1, &input, 1, line, sizeof(line)) && state.held == 1);
    assert(strstr(line, "\"held_before\":1") && strstr(line, "\"moves\":1"));
    input.mi.dwFlags = MOUSEEVENTF_LEFTUP;
    assert(uurb_pointer_trace_update(&state, 1, &input, 1, line, sizeof(line)) && state.held == 0);
    input.mi.dwFlags = MOUSEEVENTF_LEFTDOWN;
    assert(uurb_pointer_trace_update(&state, 1, &input, 0, line, sizeof(line)) && state.held == 0);
    assert(strstr(line, "\"accepted\":false"));
    for (unsigned i = 0; i < 300; ++i) uurb_pointer_trace_update(&state, 1, &input, 1, line, sizeof(line));
    assert(state.button_reports == 256);
    assert(!uurb_pointer_trace_update(&state, 1, &input, 1, line, sizeof(line)));
    input.mi.dwFlags = MOUSEEVENTF_MOVE;
    for (unsigned i = 0; i < 100; ++i) uurb_pointer_trace_update(&state, 1, &input, 1, line, sizeof(line));
    assert(state.drag_reports == 64);
    assert(!uurb_pointer_trace_update(&state, 1, &input, 1, line, sizeof(line)));
    return 0;
}
