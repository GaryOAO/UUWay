/* Windows types and the bounded settle/attach helpers are supplied by the
 * bootstrap translation unit; the deterministic probe uses a virtual clock. */
#ifndef UU_NATIVE_INPUT_STARTUP_H
#define UU_NATIVE_INPUT_STARTUP_H

#define INPUT_ATTACH_TIMEOUT_MS 35000
#define INPUT_DIAGNOSTIC_TIMEOUT_MS 20000
#define INPUT_HOOK_BUDGET_MS 70000
#define INPUT_HOOK_RETRY_MS 3000

static DWORD hook_server_input(const WCHAR *folder, DWORD target, HANDLE target_process, HANDLE stop)
{
    ULONGLONG hook_deadline = GetTickCount64() + INPUT_HOOK_BUDGET_MS;
    DWORD status = settle_server(target_process, stop, 20000);
    if (status) return status;
    status = ERROR_TIMEOUT;
    /* A successful grace period must still run the first injection. Retry
     * failures only while the same process lives, within the total budget. */
    for (unsigned attempt = 0; attempt < 5; ++attempt) {
        ULONGLONG now = GetTickCount64();
        if (now >= hook_deadline) break;
        DWORD remaining = (DWORD)(hook_deadline - now);
        DWORD delay = attempt ? (remaining < INPUT_HOOK_RETRY_MS ? remaining : INPUT_HOOK_RETRY_MS) : 0;
        if (attempt) event("native_input_hook_retry", status);
        DWORD waited = settle_server(target_process, stop, delay);
        if (waited) return waited;
        now = GetTickCount64();
        if (now >= hook_deadline) break;
        remaining = (DWORD)(hook_deadline - now);
        status = attach_input(folder, target,
                              remaining < INPUT_ATTACH_TIMEOUT_MS ? remaining : INPUT_ATTACH_TIMEOUT_MS);
        if (!status) break;
    }
    return status;
}
#endif
