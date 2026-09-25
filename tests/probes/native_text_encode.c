#include "native_text_client.h"
#include <assert.h>
#include <stdio.h>
#include <string.h>
int main(void)
{
    uurb_x11_input_event events[5] = {{.type = 3, .data = 0x4e2d}, {.type = 3, .data = 0xd83d},
        {.type = 3, .data = 0xde42}, {.type = 3, .data = 10}, {.type = 3, .data = 9}};
    char output[8192]; size_t bytes = sizeof(output);
    const char expected[] = "中🙂\n\t";
    assert(uurb_native_text_encode(events, 5, output, &bytes));
    assert(bytes == sizeof(expected) - 1 && !memcmp(output, expected, bytes));
    for (unsigned mode = 0; mode < 7; ++mode) {
        uurb_x11_input_event bad[2] = {{.type = 3, .data = 'A'}, {.type = 3, .data = 'B'}};
        if (mode == 0) bad[1].data = 0xd800;
        if (mode == 1) bad[1].data = 0xdc00;
        if (mode == 2) bad[1].data = 0;
        if (mode == 3) bad[1].data = 0x10000;
        if (mode == 4) bad[1].flags = 4;
        if (mode == 5) bad[1].type = 1;
        if (mode == 6) bad[1].data = 8;
        memset(output, 0x5a, sizeof(output)); bytes = sizeof(output);
        assert(!uurb_native_text_encode(bad, 2, output, &bytes));
        assert(bytes == sizeof(output) && output[0] == 0x5a && output[8191] == 0x5a);
    }
    bytes = 1;
    assert(!uurb_native_text_encode(events, 5, output, &bytes) && bytes == 1);
    uurb_x11_input_event revision[] = {{.type = 3, .data = 8}, {.type = 3, .data = 8}, {.type = 3, .data = 0x65b0}};
    uint32_t deleted = 99;
    bytes = sizeof(output);
    assert(uurb_native_text_plan(revision, 3, output, &bytes, &deleted));
    assert(deleted == 2 && bytes == strlen("新") && !memcmp(output, "新", bytes));
    deleted = 99; bytes = sizeof(output); output[0] = 0x5a;
    assert(!uurb_native_text_plan(revision, 2, output, &bytes, &deleted));
    assert(deleted == 99 && bytes == sizeof(output) && output[0] == 0x5a);
    revision[0].data = 'A';
    assert(!uurb_native_text_plan(revision, 3, output, &bytes, &deleted));
    puts("NATIVE_TEXT_ENCODE_PASS UTF16 UTF8 surrogate bounds no partial mutation");
    return 0;
}
