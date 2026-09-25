#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <winevt.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#ifdef UURB_NATIVE_INPUT_ONLY
#include <tlhelp32.h>
#include <wchar.h>
#endif

typedef UINT(WINAPI *send_input_fn)(UINT, LPINPUT, int);

#define INPUT_BRIDGE_MAGIC 0x42525555UL
#define INPUT_BRIDGE_MAX_INPUTS 2048UL
#ifdef UURB_NATIVE_INPUT_ONLY
#define INPUT_BRIDGE_PIPE L"\\\\.\\pipe\\uurb-native-input-v1"
#else
#define INPUT_BRIDGE_PIPE L"\\\\.\\pipe\\uurb-input-v1"
#endif

typedef struct input_bridge_request {
    DWORD magic;
    DWORD count;
    DWORD input_size;
} input_bridge_request;

typedef struct input_bridge_response {
    DWORD result;
    DWORD error;
} input_bridge_response;

static send_input_fn original_send_input;
static HANDLE log_file = INVALID_HANDLE_VALUE;
static HANDLE broker_pipe = INVALID_HANDLE_VALUE;
static CRITICAL_SECTION broker_lock;
static BOOL broker_lock_initialized;
static volatile LONG input_call_count;
static volatile LONG keyboard_call_count;
static volatile LONG mouse_call_count;
static volatile LONG other_call_count;
static volatile LONG text_call_count;
static BOOL native_only;
static volatile LONG native_ready;
#ifdef UURB_NATIVE_INPUT_ONLY
/* Remote-thread-compatible, scalar readiness; no keys, pointers or tokens. */
__declspec(dllexport) DWORD WINAPI UurbNativeInputReady(void *unused)
{
    (void)unused;
    return (DWORD)InterlockedCompareExchange(&native_ready, 0, 0);
}
#endif

static EVT_HANDLE WINAPI safe_evt_open_publisher_metadata(
    EVT_HANDLE session, LPCWSTR publisher_identity, LPCWSTR log_file_path,
    LCID locale, DWORD flags)
{
    (void)session;
    (void)publisher_identity;
    (void)log_file_path;
    (void)locale;
    (void)flags;

    SetLastError(ERROR_EVT_PUBLISHER_METADATA_NOT_FOUND);
    return NULL;
}

static void write_log(const char *message)
{
    DWORD written;

    if (log_file == INVALID_HANDLE_VALUE)
        return;

    WriteFile(log_file, message, (DWORD)strlen(message), &written, NULL);
}

static void flush_log(void)
{
    if (log_file != INVALID_HANDLE_VALUE)
        FlushFileBuffers(log_file);
}

static void open_log(void)
{
    wchar_t path[MAX_PATH];
    DWORD length;

    length = GetEnvironmentVariableW(L"UU_INPUT_BRIDGE_LOG", path, MAX_PATH);
    if (length == 0 || length >= MAX_PATH) {
        length = GetTempPathW(MAX_PATH, path);
        if (length == 0 || length >= MAX_PATH - 20)
            lstrcpynW(path, L"uu-input-bridge.log", MAX_PATH);
        else
            lstrcatW(path, L"uu-input-bridge.log");
    }

    log_file = CreateFileW(path, FILE_APPEND_DATA,
                           FILE_SHARE_READ | FILE_SHARE_WRITE, NULL,
                           OPEN_ALWAYS, FILE_ATTRIBUTE_NORMAL, NULL);
}

static HWND find_relay_window(void)
{
    return FindWindowW(NULL, L"Ubuntu-Desktop-Relay");
}

static BOOL write_all(HANDLE handle, const void *buffer, DWORD size)
{
    const BYTE *position = (const BYTE *)buffer;

    while (size > 0) {
        DWORD written = 0;

        if (!WriteFile(handle, position, size, &written, NULL) || written == 0)
            return FALSE;
        position += written;
        size -= written;
    }

    return TRUE;
}

static BOOL read_all(HANDLE handle, void *buffer, DWORD size)
{
    BYTE *position = (BYTE *)buffer;

    while (size > 0) {
        DWORD received = 0;

        if (!ReadFile(handle, position, size, &received, NULL) || received == 0)
            return FALSE;
        position += received;
        size -= received;
    }

    return TRUE;
}

static void disconnect_broker(void)
{
    if (broker_pipe != INVALID_HANDLE_VALUE) {
        CloseHandle(broker_pipe);
        broker_pipe = INVALID_HANDLE_VALUE;
    }
}

static BOOL connect_broker(void)
{
    if (broker_pipe != INVALID_HANDLE_VALUE)
        return TRUE;

    if (!WaitNamedPipeW(INPUT_BRIDGE_PIPE, 500))
        return FALSE;

    broker_pipe = CreateFileW(INPUT_BRIDGE_PIPE, GENERIC_READ | GENERIC_WRITE,
                              0, NULL, OPEN_EXISTING, 0, NULL);
    return broker_pipe != INVALID_HANDLE_VALUE;
}

static UINT send_through_broker(UINT count, const INPUT *inputs, int size,
                                DWORD *broker_error)
{
    input_bridge_request request;
    input_bridge_response response;
    UINT result = 0;
    int attempt;

    *broker_error = ERROR_ACCESS_DENIED;
    if (!broker_lock_initialized || count == 0 || inputs == NULL ||
        size != (int)sizeof(INPUT))
        return 0;
    if (count > INPUT_BRIDGE_MAX_INPUTS) {
        *broker_error = ERROR_INSUFFICIENT_BUFFER;
        return 0;
    }

    request.magic = INPUT_BRIDGE_MAGIC;
    request.count = count;
    request.input_size = (DWORD)size;

    /*
     * UU's phone dictation grows a provisional composition into one SendInput
     * array. Preserve that complete call so a semantic clipboard paste cannot
     * observe only the final fragment. The bounded protocol allows 1,024
     * UTF-16 key pairs, comfortably above the live 332-record failure while
     * keeping every helper allocation finite.
     */
    EnterCriticalSection(&broker_lock);
    /* A response may be lost after delivery. Never replay native input. */
    for (attempt = 0; attempt < (native_only ? 1 : 2); attempt++) {
        if (connect_broker() &&
            write_all(broker_pipe, &request, sizeof(request)) &&
            write_all(broker_pipe, inputs, count * sizeof(INPUT)) &&
            read_all(broker_pipe, &response, sizeof(response))) {
            result = response.result;
            *broker_error = response.error;
            break;
        }
        disconnect_broker();
    }
    LeaveCriticalSection(&broker_lock);
    return result;
}

static BOOL contains_unicode_keyboard(UINT count, const INPUT *inputs, int size)
{
    UINT index;

    if (count == 0 || inputs == NULL || size != (int)sizeof(INPUT))
        return FALSE;

    for (index = 0; index < count; index++) {
        if (inputs[index].type == INPUT_KEYBOARD &&
            (inputs[index].ki.dwFlags & KEYEVENTF_UNICODE) != 0)
            return TRUE;
    }

    return FALSE;
}

static BOOL contains_input_type(UINT count, const INPUT *inputs, int size,
                                DWORD type)
{
    UINT index;

    if (count == 0 || inputs == NULL || size != (int)sizeof(INPUT))
        return FALSE;
    for (index = 0; index < count; index++) {
        if (inputs[index].type == type)
            return TRUE;
    }
    return FALSE;
}

static UINT WINAPI bridged_send_input(UINT count, LPINPUT inputs, int size)
{
    char line[512];
    HWND relay;
    UINT result;
    DWORD error;
    LONG call_number;
    DWORD first_type = UINT32_MAX;
    DWORD first_flags = 0;
    BOOL used_broker = FALSE;
    BOOL unicode_keyboard;
    BOOL physical_keyboard;
    BOOL mouse_input;
    const char *category;
    LONG category_call_number;
    ULONGLONG started_ms;
    ULONGLONG direct_started_ms;
    ULONGLONG broker_started_ms;
    DWORD direct_ms = 0;
    DWORD broker_ms = 0;

    started_ms = GetTickCount64();

    relay = native_only ? NULL : find_relay_window();
    if (relay != NULL)
        SetForegroundWindow(relay);

    if (count > 0 && inputs != NULL && size == (int)sizeof(INPUT)) {
        first_type = inputs[0].type;
        if (first_type == INPUT_MOUSE)
            first_flags = inputs[0].mi.dwFlags;
        else if (first_type == INPUT_KEYBOARD)
            first_flags = inputs[0].ki.dwFlags;
    }

    unicode_keyboard = contains_unicode_keyboard(count, inputs, size);
    physical_keyboard = !unicode_keyboard &&
                        contains_input_type(count, inputs, size,
                                            INPUT_KEYBOARD);
    mouse_input = !unicode_keyboard && !physical_keyboard &&
                  contains_input_type(count, inputs, size, INPUT_MOUSE);
    if (native_only || unicode_keyboard) {
        broker_started_ms = GetTickCount64();
        result = send_through_broker(count, inputs, size, &error);
        broker_ms = (DWORD)(GetTickCount64() - broker_started_ms);
        used_broker = TRUE;
        SetLastError(error);
    } else {
        direct_started_ms = GetTickCount64();
        SetLastError(ERROR_SUCCESS);
        result = original_send_input(count, inputs, size);
        error = GetLastError();
        direct_ms = (DWORD)(GetTickCount64() - direct_started_ms);
        if (result != count) {
            broker_started_ms = GetTickCount64();
            result = send_through_broker(count, inputs, size, &error);
            broker_ms = (DWORD)(GetTickCount64() - broker_started_ms);
            used_broker = TRUE;
            SetLastError(error);
        }
    }
    call_number = InterlockedIncrement(&input_call_count);
    if (unicode_keyboard) {
        category = "text";
        category_call_number = InterlockedIncrement(&text_call_count);
    } else if (physical_keyboard) {
        category = "keyboard";
        category_call_number = InterlockedIncrement(&keyboard_call_count);
    } else if (mouse_input) {
        category = "mouse";
        category_call_number = InterlockedIncrement(&mouse_call_count);
    } else {
        category = "other";
        category_call_number = InterlockedIncrement(&other_call_count);
    }

    if ((unicode_keyboard && category_call_number <= 256) ||
        (physical_keyboard && category_call_number <= 256) ||
        (mouse_input && category_call_number <= 32) ||
        (!unicode_keyboard && !physical_keyboard && !mouse_input &&
         category_call_number <= 64) ||
        result != count) {
        _snprintf(line, sizeof(line),
                  "call=%ld category=%s category-call=%ld count=%lu type=%lu flags=0x%08lx route=%s direct-ms=%lu broker-ms=%lu total-ms=%lu result=%lu error=%lu\r\n",
                  call_number, category, category_call_number,
                  (unsigned long)count,
                  (unsigned long)first_type, (unsigned long)first_flags,
                  used_broker ? "broker" : "direct",
                  (unsigned long)direct_ms, (unsigned long)broker_ms,
                  (unsigned long)(GetTickCount64() - started_ms),
                  (unsigned long)result, (unsigned long)error);
        line[sizeof(line) - 1] = '\0';
        write_log(line);
        if (result != count)
            flush_log();
    }

    return result;
}

static BOOL patch_import(HMODULE module, const char *dll_name,
                         const char *function_name, uintptr_t replacement,
                         uintptr_t *original)
{
    BYTE *base = (BYTE *)module;
    IMAGE_DOS_HEADER *dos = (IMAGE_DOS_HEADER *)base;
    IMAGE_NT_HEADERS *nt;
    IMAGE_IMPORT_DESCRIPTOR *descriptor;

    if (dos->e_magic != IMAGE_DOS_SIGNATURE)
        return FALSE;

    nt = (IMAGE_NT_HEADERS *)(base + dos->e_lfanew);
    if (nt->Signature != IMAGE_NT_SIGNATURE)
        return FALSE;

    descriptor = (IMAGE_IMPORT_DESCRIPTOR *)(
        base + nt->OptionalHeader
                   .DataDirectory[IMAGE_DIRECTORY_ENTRY_IMPORT]
                   .VirtualAddress);

    if ((BYTE *)descriptor == base)
        return FALSE;

    for (; descriptor->Name != 0; descriptor++) {
        const char *imported_dll = (const char *)(base + descriptor->Name);
        IMAGE_THUNK_DATA *names;
        IMAGE_THUNK_DATA *addresses;

        if (_stricmp(imported_dll, dll_name) != 0)
            continue;

        names = descriptor->OriginalFirstThunk != 0
                    ? (IMAGE_THUNK_DATA *)(base + descriptor->OriginalFirstThunk)
                    : (IMAGE_THUNK_DATA *)(base + descriptor->FirstThunk);
        addresses = (IMAGE_THUNK_DATA *)(base + descriptor->FirstThunk);

        for (; names->u1.AddressOfData != 0; names++, addresses++) {
            IMAGE_IMPORT_BY_NAME *import_name;
            DWORD old_protection;

            if (IMAGE_SNAP_BY_ORDINAL(names->u1.Ordinal))
                continue;

            import_name = (IMAGE_IMPORT_BY_NAME *)(
                base + names->u1.AddressOfData);
            if (strcmp((const char *)import_name->Name, function_name) != 0)
                continue;

            if (original != NULL)
                *original = (uintptr_t)addresses->u1.Function;
            if (!VirtualProtect(&addresses->u1.Function,
                                sizeof(addresses->u1.Function),
                                PAGE_READWRITE, &old_protection))
                return FALSE;

            addresses->u1.Function = (ULONGLONG)replacement;
            FlushInstructionCache(GetCurrentProcess(),
                                  &addresses->u1.Function,
                                  sizeof(addresses->u1.Function));
            VirtualProtect(&addresses->u1.Function,
                           sizeof(addresses->u1.Function), old_protection,
                           &old_protection);
            return TRUE;
        }
    }

    return FALSE;
}

#ifdef UURB_NATIVE_INPUT_ONLY
/* Route standard SendInput imports from vendor DLLs as well as the main EXE.
 * In particular, a streamer's import does not go through the EXE's IAT slot.
 * Scope is the installed application directory, not every Wine/system DLL.
 * No version-specific vendor address or remote-account setting is involved. */
#include "uu_native_cursor_win.h"
#include "uu_native_display_trace_win.h"
__declspec(dllexport) DWORD WINAPI UurbInputBridgeDisplaySetVersion(void)
{
    return UURB_DISPLAY_SET_IMPLEMENTATION_VERSION;
}
__declspec(dllexport) DWORD WINAPI UurbInputBridgeDisplayDpiV2Version(void)
{
    return UURB_DISPLAY_SNAPSHOT_VERSION;
}
static BOOL native_image_range(BYTE *base, DWORD size, DWORD rva, size_t bytes)
{
    MEMORY_BASIC_INFORMATION region;
    if (rva > size || bytes > size - rva ||
        !VirtualQuery(base + rva, &region, sizeof(region)) ||
        region.AllocationBase != base || region.State != MEM_COMMIT ||
        (region.Protect & (PAGE_NOACCESS | PAGE_GUARD))) return FALSE;
    return bytes <= region.RegionSize - ((base + rva) - (BYTE *)region.BaseAddress);
}
static BOOL native_image_string(BYTE *base, DWORD size, DWORD rva, const char *expected)
{
    size_t length = strlen(expected) + 1;
    return native_image_range(base, size, rva, length) &&
           !_stricmp((const char *)(base + rva), expected);
}
static unsigned patch_native_module(HMODULE module)
{
    BYTE *base = (BYTE *)module;
    IMAGE_DOS_HEADER *dos = (IMAGE_DOS_HEADER *)base;
    if (dos->e_magic != IMAGE_DOS_SIGNATURE || dos->e_lfanew < (LONG)sizeof(*dos) || dos->e_lfanew > 1048576)
        return 0;
    if (!native_image_range(base, 1048576, (DWORD)dos->e_lfanew, sizeof(IMAGE_NT_HEADERS))) return 0;
    IMAGE_NT_HEADERS *nt = (IMAGE_NT_HEADERS *)(base + dos->e_lfanew);
    if (nt->Signature != IMAGE_NT_SIGNATURE || nt->OptionalHeader.Magic != IMAGE_NT_OPTIONAL_HDR_MAGIC ||
        nt->OptionalHeader.NumberOfRvaAndSizes <= IMAGE_DIRECTORY_ENTRY_IMPORT) return 0;
    DWORD size = nt->OptionalHeader.SizeOfImage;
    IMAGE_DATA_DIRECTORY directory = nt->OptionalHeader.DataDirectory[IMAGE_DIRECTORY_ENTRY_IMPORT];
    if (!directory.VirtualAddress || !native_image_range(base, size, directory.VirtualAddress, directory.Size)) return 0;
    IMAGE_IMPORT_DESCRIPTOR *table = (IMAGE_IMPORT_DESCRIPTOR *)(base + directory.VirtualAddress);
    unsigned patched = 0;
    for (DWORD i = 0; i < directory.Size / sizeof(*table) && table[i].Name; ++i) {
        if ((!native_image_string(base, size, table[i].Name, "user32.dll") &&
             !native_image_string(base, size, table[i].Name, "setupapi.dll") &&
             !native_image_string(base, size, table[i].Name, "advapi32.dll") &&
             !native_image_string(base, size, table[i].Name, "kernel32.dll") &&
             !native_image_string(base, size, table[i].Name, "kernelbase.dll")) || !table[i].FirstThunk) continue;
        for (DWORD offset = 0; offset <= size && sizeof(IMAGE_THUNK_DATA) <= size - offset;
             offset += sizeof(IMAGE_THUNK_DATA)) {
            if (table[i].FirstThunk > size - offset) break;
            DWORD rva = table[i].FirstThunk + offset;
            if (!native_image_range(base, size, rva, sizeof(IMAGE_THUNK_DATA))) break;
            IMAGE_THUNK_DATA *slot = (IMAGE_THUNK_DATA *)(base + rva);
            if (!slot->u1.Function) break;
            /* Resolved import addresses also work with absent original thunks.
             * Never replace another hook or write this bridge's own IAT. */
            uintptr_t original = (uintptr_t)original_send_input;
            void *replacement = (void *)&bridged_send_input;
            BOOL cursor_slot = FALSE;
            BOOL display_slot = FALSE;
            BOOL terminal_slot = native_terminal_hook_for((uintptr_t)slot->u1.Function) != NULL;
            void *display_hook = native_display_hook_for((uintptr_t)slot->u1.Function);
            if (native_get_cursor_info && (uintptr_t)slot->u1.Function == native_get_cursor_info) {
                original = native_get_cursor_info;
                replacement = (void *)&native_cursor_info;
                cursor_slot = TRUE;
            } else if (!native_cursor_video_embedded && native_get_cursor_pos && (uintptr_t)slot->u1.Function == native_get_cursor_pos) {
                original = native_get_cursor_pos;
                replacement = (void *)&native_cursor_pos;
                cursor_slot = TRUE;
            } else if (display_hook || (display_getproc && (uintptr_t)slot->u1.Function == (uintptr_t)display_getproc)) {
                original = (uintptr_t)slot->u1.Function;
                replacement = display_hook ? display_hook : (void *)&traced_display_getproc;
                display_slot = TRUE;
            } else if ((uintptr_t)slot->u1.Function != original) continue;
            DWORD protection;
            if (!VirtualProtect(&slot->u1.Function, sizeof(slot->u1.Function), PAGE_READWRITE, &protection)) continue;
            void *previous = InterlockedCompareExchangePointer((void *volatile *)&slot->u1.Function,
                replacement, (void *)original);
            DWORD ignored;
            VirtualProtect(&slot->u1.Function, sizeof(slot->u1.Function), protection, &ignored);
            if (previous == (void *)original) {
                if (terminal_slot) InterlockedIncrement(&native_terminal_import_slots);
                else if (display_slot) InterlockedIncrement(&native_display_import_slots);
                else if (cursor_slot) InterlockedIncrement(&native_cursor_import_slots);
                else ++patched;
            }
        }
    }
    return patched;
}
static unsigned patch_native_vendor_modules(void)
{
    WCHAR root[32768], path[32768];
    DWORD length = GetModuleFileNameW(NULL, root, ARRAYSIZE(root));
    if (!length || length >= ARRAYSIZE(root)) return 0;
    WCHAR *name = wcsrchr(root, L'\\');
    if (!name) return 0;
    *name = 0;
    WCHAR *folder = wcsrchr(root, L'\\');
    if (folder && !_wcsicmp(folder + 1, L"bin")) *folder = 0;
    size_t prefix = wcslen(root);
    HMODULE self = NULL;
    GetModuleHandleExW(GET_MODULE_HANDLE_EX_FLAG_FROM_ADDRESS | GET_MODULE_HANDLE_EX_FLAG_UNCHANGED_REFCOUNT,
                      (LPCWSTR)&patch_native_vendor_modules, &self);
    HANDLE snapshot = CreateToolhelp32Snapshot(TH32CS_SNAPMODULE, GetCurrentProcessId());
    if (snapshot == INVALID_HANDLE_VALUE) return 0;
    MODULEENTRY32W item = {.dwSize = sizeof(item)};
    unsigned patched = 0;
    if (Module32FirstW(snapshot, &item)) do {
        if (item.hModule == self) continue;
        HMODULE held = NULL;
        if (!GetModuleHandleExW(GET_MODULE_HANDLE_EX_FLAG_FROM_ADDRESS, (LPCWSTR)item.modBaseAddr, &held)) continue;
        length = GetModuleFileNameW(held, path, ARRAYSIZE(path));
        if (length > prefix && length < ARRAYSIZE(path) && path[prefix] == L'\\' &&
            !_wcsnicmp(root, path, prefix)) patched += patch_native_module(held);
        FreeLibrary(held);
    } while (Module32NextW(snapshot, &item));
    CloseHandle(snapshot);
    return patched;
}
#endif

static DWORD WINAPI initialize_bridge(void *unused)
{
    uintptr_t send_input_address = 0;
    BOOL input_patched;
    BOOL event_log_patched;

    (void)unused;
#ifdef UURB_NATIVE_INPUT_ONLY
    native_only = TRUE;
#endif
    open_log();
    InitializeCriticalSection(&broker_lock);
    broker_lock_initialized = TRUE;
    input_patched = patch_import(
        GetModuleHandleW(NULL), "USER32.dll", "SendInput",
        (uintptr_t)&bridged_send_input, &send_input_address);
    original_send_input = (send_input_fn)send_input_address;
#ifdef UURB_NATIVE_INPUT_ONLY
    native_display_trace_initialize();
    int display_status = native_display_adapter_initialize();
    if (display_status < 0) {
        write_log("UURB_NATIVE_DISPLAY {\"initialization_failed\":true}\r\n");
        InterlockedExchange(&native_ready, 2);
        return 1;
    }
    int cursor_status = native_cursor_initialize();
    if (cursor_status < 0) {
        write_log("UURB_NATIVE_CURSOR {\"initialization_failed\":true}\r\n");
        InterlockedExchange(&native_ready, 2);
        return 1;
    }
    if (input_patched && original_send_input) {
        unsigned additional = patch_native_vendor_modules();
        char message[128];
        snprintf(message, sizeof(message), "UURB_NATIVE_IMPORTS {\"additional_sendinput_slots\":%u}\r\n", additional);
        write_log(message);
        snprintf(message, sizeof(message), "UURB_NATIVE_DISPLAY {\"import_slots\":%ld,\"passthrough_only\":%s}\r\n",
                 native_display_import_slots, display_status ? "false" : "true");
        write_log(message);
        snprintf(message, sizeof(message), "UURB_NATIVE_TERMINAL {\"event\":\"hooks_ready\",\"import_slots\":%ld}\r\n",
                 native_terminal_import_slots);
        write_log(message);
        if (cursor_status) {
            snprintf(message, sizeof(message), "UURB_NATIVE_CURSOR {\"import_slots\":%ld}\r\n", native_cursor_import_slots);
            write_log(message);
        }
    }
#endif
    InterlockedExchange(&native_ready, input_patched && original_send_input ? 1 : 2);
    event_log_patched = patch_import(
        GetModuleHandleW(NULL), "wevtapi.dll", "EvtOpenPublisherMetadata",
        (uintptr_t)&safe_evt_open_publisher_metadata, NULL);

    write_log(input_patched ? "UU SendInput bridge active\r\n"
                            : "UU bridge could not find SendInput import\r\n");
    write_log(event_log_patched
                  ? "UU Wine event-log compatibility active\r\n"
                  : "UU bridge could not find event-log import\r\n");
    flush_log();
#ifdef UURB_NATIVE_INPUT_ONLY
    /* DLLs may load lazily after remote connection. Refresh only application
     * imports and only write newly resolved original SendInput slots. Existing
     * slots are untouched, including unrelated third-party hooks. */
    if (input_patched && original_send_input) for (;;) {
        Sleep(500);
        LONG cursor_before = native_cursor_import_slots;
        unsigned additional = patch_native_vendor_modules();
        if (native_cursor_import_slots != cursor_before) {
            char message[128];
            snprintf(message, sizeof(message), "UURB_NATIVE_CURSOR {\"import_slots\":%ld}\r\n", native_cursor_import_slots);
            write_log(message); flush_log();
        }
        if (additional) {
            char message[128];
            snprintf(message, sizeof(message), "UURB_NATIVE_IMPORTS {\"additional_sendinput_slots\":%u}\r\n", additional);
            write_log(message); flush_log();
        }
    }
#endif
    return input_patched && event_log_patched ? 0 : 1;
}

BOOL WINAPI DllMain(HINSTANCE instance, DWORD reason, LPVOID reserved)
{
    HANDLE thread;

    (void)reserved;

    if (reason == DLL_PROCESS_ATTACH) {
        DisableThreadLibraryCalls(instance);
        thread = CreateThread(NULL, 0, initialize_bridge, NULL, 0, NULL);
        if (thread != NULL)
            CloseHandle(thread);
    } else if (reason == DLL_PROCESS_DETACH) {
        disconnect_broker();
        if (broker_lock_initialized) {
            DeleteCriticalSection(&broker_lock);
            broker_lock_initialized = FALSE;
        }
        if (log_file != INVALID_HANDLE_VALUE) {
            CloseHandle(log_file);
            log_file = INVALID_HANDLE_VALUE;
        }
    }

    return TRUE;
}
