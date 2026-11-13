/* Exercise the production retry loop with a virtual clock and process handles. */
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <wchar.h>

typedef uint32_t DWORD;
typedef uint64_t ULONGLONG;
typedef int HANDLE;
typedef wchar_t WCHAR;
#define WAIT_OBJECT_0 0
#define WAIT_TIMEOUT 258
#define ERROR_SUCCESS 0
#define ERROR_CANCELLED 1223
#define ERROR_PROCESS_ABORTED 1067
#define ERROR_INVALID_HANDLE 6
#define ERROR_TIMEOUT 1460

#define check(value) do { if (!(value)) { \
    fprintf(stderr, "line %d: %s\n", __LINE__, #value); exit(1); } } while (0)

static ULONGLONG clock_ms;
static DWORD settle_result, attach_result, attach_duration;
static unsigned calls, failures_before_success, retries;
static int stop_during_retry, die_during_retry;
static ULONGLONG GetTickCount64(void) { return clock_ms; }
static DWORD settle_server(HANDLE process, HANDLE stop, DWORD duration)
{
    check(process == 1 && stop == 2);
    clock_ms += duration;
    if (duration == 20000) return settle_result;
    if (stop_during_retry && duration) return ERROR_CANCELLED;
    if (die_during_retry && clock_ms >= 23000) return ERROR_PROCESS_ABORTED;
    return ERROR_SUCCESS;
}
static void event(const char *name, DWORD status)
{
    (void)name;
    check(status != 0);
    ++retries;
}
static DWORD attach_input(const WCHAR *folder, DWORD pid, DWORD timeout)
{
    check(!wcscmp(folder, L"owned/") && pid == 42);
    check(timeout > 0 && timeout <= 35000);
    check(clock_ms + timeout <= 70000);
    ++calls;
    clock_ms += attach_duration < timeout ? attach_duration : timeout;
    return calls <= failures_before_success ? attach_result : ERROR_SUCCESS;
}
#include "../../src/uu_native_input_startup.h"

static void reset(void)
{
    clock_ms = calls = retries = 0;
    settle_result = ERROR_SUCCESS;
    attach_result = 1;
    attach_duration = failures_before_success = 0;
    stop_during_retry = die_during_retry = 0;
}
static DWORD run(void) { return hook_server_input(L"owned/", 42, 1, 2); }
int main(void)
{
    reset(); check(run() == 0); check(calls == 1); /* Normal grace must inject. */
    reset(); failures_before_success = 2;
    check(run() == 0); check(calls == 3 && retries == 2);
    reset(); failures_before_success = 99;
    check(run() == 1); check(calls == 5 && clock_ms == 32000);
    reset(); failures_before_success = 99; attach_duration = 35000;
    check(run() == 1); check(calls == 2 && clock_ms <= 70000);
    reset(); settle_result = ERROR_CANCELLED;
    check(run() == ERROR_CANCELLED); check(calls == 0);
    reset(); settle_result = ERROR_PROCESS_ABORTED;
    check(run() == ERROR_PROCESS_ABORTED); check(calls == 0);
    reset(); settle_result = ERROR_INVALID_HANDLE;
    check(run() == ERROR_INVALID_HANDLE); check(calls == 0);
    reset(); failures_before_success = 99; stop_during_retry = 1;
    check(run() == ERROR_CANCELLED); check(calls == 1);
    reset(); failures_before_success = 99; die_during_retry = 1;
    check(run() == ERROR_PROCESS_ABORTED); check(calls == 1);
    puts("native input startup checks passed");
    return 0;
}
