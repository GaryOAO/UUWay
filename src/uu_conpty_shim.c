/* Stand-in for the Microsoft conpty.dll bundled with UU's conpty_bridge.
 *
 * conpty_bridge hands CreatePseudoConsole the two pipes that carry UU's
 * terminal bytes. Wine's console host re-renders everything that passes
 * through it and loses VT sequences, so instead connect those pipes straight
 * to the authenticated Linux PTY broker: raw bytes both ways, as over SSH.
 *
 * The bridge's --uuyc-mux-session names a persistent broker session, so a
 * viewer that leaves and returns finds the same shell; the psmux pane of that
 * session anchors it (uu_terminal_proxy.c, uu_terminal_bridge.c).
 *
 * conpty_bridge still starts a child on the returned console and ends the
 * session when that child exits. The console is therefore a real Wine
 * pseudoconsole on private pipes, and the child (the PowerShell proxy) waits
 * on a named event that is set when this viewer's connection ends.
 *
 * Without a reachable broker, fall back to Wine's CreatePseudoConsole. Wine
 * ends every client at once when PSEUDOCONSOLE_INHERIT_CURSOR is set, which
 * conpty_bridge always passes, so that flag is dropped. */
#define WIN32_LEAN_AND_MEAN
#include <winsock2.h>
#include <windows.h>

#include <stdint.h>
#include <stdio.h>
#include <string.h>

#include "terminal_bridge_protocol.h"

#ifndef PSEUDOCONSOLE_INHERIT_CURSOR
#define PSEUDOCONSOLE_INHERIT_CURSOR 0x1
#endif

/* Windows' HPCON is an opaque handle; older mingw headers do not declare it. */
typedef void *pseudo_console;

typedef HRESULT (WINAPI *create_function)(COORD, HANDLE, HANDLE, DWORD, pseudo_console *);
typedef HRESULT (WINAPI *resize_function)(pseudo_console, COORD);
typedef void (WINAPI *close_function)(pseudo_console);

/* Each UU terminal runs in its own conpty_bridge process with one console. */
static struct {
    pseudo_console console;
    SOCKET socket;
    HANDLE bridge_input;
    HANDLE bridge_output;
    HANDLE console_input;
    HANDLE console_output;
    HANDLE done;
    HANDLE threads[3];
    CRITICAL_SECTION send_lock;
} session = {NULL, INVALID_SOCKET, NULL, NULL, NULL, NULL, NULL, {NULL, NULL, NULL}, {0}};

/* Phase-only diagnostics beside this DLL; never terminal bytes or tokens. */
static void trace(const char *event, DWORD value)
{
    char path[MAX_PATH], line[160], *slash;
    HMODULE self;
    SYSTEMTIME now;
    HANDLE file;
    DWORD written;
    int length;

    if (!GetModuleHandleExA(GET_MODULE_HANDLE_EX_FLAG_FROM_ADDRESS |
                            GET_MODULE_HANDLE_EX_FLAG_UNCHANGED_REFCOUNT,
                            (LPCSTR)(void *)trace, &self) ||
        !GetModuleFileNameA(self, path, sizeof(path) - 16) ||
        (slash = strrchr(path, '\\')) == NULL)
        return;
    strcpy(slash + 1, "uu-conpty.trace");
    file = CreateFileA(path, FILE_APPEND_DATA, FILE_SHARE_READ | FILE_SHARE_WRITE,
                       NULL, OPEN_ALWAYS, 0, NULL);
    if (file == INVALID_HANDLE_VALUE)
        return;
    GetSystemTime(&now);
    length = snprintf(line, sizeof(line), "%02u:%02u:%02u.%03uZ pid=%lu %s %lu\r\n",
                      now.wHour, now.wMinute, now.wSecond, now.wMilliseconds,
                      GetCurrentProcessId(), event, value);
    if (length > 0 && length < (int)sizeof(line))
        WriteFile(file, line, (DWORD)length, &written, NULL);
    CloseHandle(file);
}

static FARPROC kernel_function(const char *name)
{
    HMODULE kernel = GetModuleHandleW(L"kernel32.dll");

    return kernel != NULL ? GetProcAddress(kernel, name) : NULL;
}

static HRESULT wine_create(COORD size, HANDLE input, HANDLE output,
                           DWORD flags, pseudo_console *console)
{
    create_function create =
        (create_function)(void *)kernel_function("CreatePseudoConsole");

    if (create == NULL)
        return E_NOTIMPL;
    return create(size, input, output, flags & ~PSEUDOCONSOLE_INHERIT_CURSOR,
                  console);
}

static void close_handle(HANDLE *handle)
{
    if (*handle != NULL && *handle != INVALID_HANDLE_VALUE)
        CloseHandle(*handle);
    *handle = NULL;
}

/* Read "version=1\nport=N\ntoken=<64 hex>\n" from beside this DLL. */
static int load_broker(uint16_t *port, char *token)
{
    char path[MAX_PATH];
    char config[256];
    char *slash;
    char *cursor;
    HMODULE self;
    HANDLE file;
    DWORD received = 0;
    unsigned long value;
    int result = 0;

    if (!GetModuleHandleExA(GET_MODULE_HANDLE_EX_FLAG_FROM_ADDRESS |
                            GET_MODULE_HANDLE_EX_FLAG_UNCHANGED_REFCOUNT,
                            (LPCSTR)(void *)load_broker, &self) ||
        !GetModuleFileNameA(self, path, sizeof(path)))
        return 0;
    slash = strrchr(path, '\\');
    if (slash == NULL || (size_t)(slash - path) + 1 +
        sizeof(UURB_TERMINAL_CONFIG_FILENAME) > sizeof(path))
        return 0;
    strcpy(slash + 1, UURB_TERMINAL_CONFIG_FILENAME);
    file = CreateFileA(path, GENERIC_READ, FILE_SHARE_READ | FILE_SHARE_DELETE,
                       NULL, OPEN_EXISTING, 0, NULL);
    if (file == INVALID_HANDLE_VALUE)
        return 0;
    if (ReadFile(file, config, sizeof(config) - 1, &received, NULL)) {
        config[received] = '\0';
        if (strncmp(config, "version=1\nport=", 15) == 0) {
            value = strtoul(config + 15, &cursor, 10);
            if (value > 0 && value <= 65535 && strncmp(cursor, "\ntoken=", 7) == 0 &&
                strlen(cursor + 7) == UURB_TERMINAL_TOKEN_LENGTH + 1 &&
                cursor[7 + UURB_TERMINAL_TOKEN_LENGTH] == '\n') {
                memcpy(token, cursor + 7, UURB_TERMINAL_TOKEN_LENGTH);
                *port = (uint16_t)value;
                result = 1;
            }
        }
    }
    CloseHandle(file);
    SecureZeroMemory(config, sizeof(config));
    return result;
}

static int send_all(const void *data, int length)
{
    const char *cursor = data;

    while (length > 0) {
        int sent = send(session.socket, cursor, length, 0);
        if (sent <= 0)
            return 0;
        cursor += sent;
        length -= sent;
    }
    return 1;
}

static int send_frame(uint8_t type, const void *payload, uint32_t length)
{
    struct uurb_terminal_frame frame;
    int result;

    memset(&frame, 0, sizeof(frame));
    frame.type = type;
    frame.length = htonl(length);
    EnterCriticalSection(&session.send_lock);
    result = send_all(&frame, sizeof(frame)) &&
             (length == 0 || send_all(payload, (int)length));
    LeaveCriticalSection(&session.send_lock);
    return result;
}

/* conpty_bridge names the UU terminal it serves with --uuyc-mux-session; that
 * name keys a persistent Linux session the viewer can leave and rejoin. */
static size_t bridge_session_name(char *name, size_t size)
{
    const wchar_t *cursor = wcsstr(GetCommandLineW(), L"--uuyc-mux-session");
    size_t length = 0;
    int quoted;

    if (cursor == NULL)
        return 0;
    cursor += wcslen(L"--uuyc-mux-session");
    if (*cursor == L'=')
        cursor++;
    while (*cursor == L' ' || *cursor == L'\t')
        cursor++;
    quoted = *cursor == L'"';
    if (quoted)
        cursor++;
    while (*cursor && (quoted ? *cursor != L'"' : *cursor != L' ' && *cursor != L'\t')) {
        wchar_t c = *cursor++;

        if (length + 1 >= size ||
            !((c >= L'a' && c <= L'z') || (c >= L'A' && c <= L'Z') ||
              (c >= L'0' && c <= L'9') || c == L'_' || c == L'-' || c == L'.'))
            return 0;
        name[length++] = (char)c;
    }
    name[length] = '\0';
    return length;
}

static SOCKET connect_broker(COORD size)
{
    struct sockaddr_in address;
    struct uurb_terminal_hello hello;
    struct uurb_terminal_session session;
    char name[UURB_TERMINAL_MAX_SESSION_NAME + 1];
    size_t name_length = bridge_session_name(name, sizeof(name));
    char token[UURB_TERMINAL_TOKEN_LENGTH + 1] = {0};
    unsigned char accepted = 0;
    uint16_t port;
    WSADATA winsock;
    SOCKET connection;
    int received;

    if (!load_broker(&port, token) || WSAStartup(MAKEWORD(2, 2), &winsock) != 0)
        return INVALID_SOCKET;
    connection = socket(AF_INET, SOCK_STREAM, IPPROTO_TCP);
    memset(&address, 0, sizeof(address));
    address.sin_family = AF_INET;
    address.sin_port = htons(port);
    address.sin_addr.s_addr = htonl(INADDR_LOOPBACK);
    memset(&hello, 0, sizeof(hello));
    hello.magic = htonl(UURB_TERMINAL_MAGIC);
    hello.version = htons(name_length > 0 ? UURB_TERMINAL_VERSION_SESSION
                                          : UURB_TERMINAL_VERSION);
    session.role = UURB_TERMINAL_ROLE_ATTACH;
    session.name_length = (uint8_t)name_length;
    hello.token_length = htons(UURB_TERMINAL_TOKEN_LENGTH);
    hello.columns = htons((uint16_t)(size.X > 0 ? size.X : 80));
    hello.rows = htons((uint16_t)(size.Y > 0 ? size.Y : 24));
    if (connection == INVALID_SOCKET ||
        connect(connection, (struct sockaddr *)&address, sizeof(address)) != 0 ||
        send(connection, (const char *)&hello, sizeof(hello), 0) != sizeof(hello) ||
        send(connection, token, UURB_TERMINAL_TOKEN_LENGTH, 0) != UURB_TERMINAL_TOKEN_LENGTH ||
        (name_length > 0 &&
         (send(connection, (const char *)&session, sizeof(session), 0) != sizeof(session) ||
          send(connection, name, (int)name_length, 0) != (int)name_length)) ||
        (received = recv(connection, (char *)&accepted, 1, 0)) != 1 ||
        accepted != UURB_TERMINAL_ACCEPTED) {
        if (connection != INVALID_SOCKET)
            closesocket(connection);
        connection = INVALID_SOCKET;
    }
    SecureZeroMemory(token, sizeof(token));
    return connection;
}

/* UU keystrokes (already VT input) to the Linux PTY. */
static DWORD WINAPI input_pump(LPVOID unused)
{
    char buffer[16384];
    DWORD received;

    (void)unused;
    while (ReadFile(session.bridge_input, buffer, sizeof(buffer), &received, NULL) &&
           received > 0) {
        if (!send_frame(UURB_TERMINAL_FRAME_DATA, buffer, received)) {
            trace("input_send_failed", WSAGetLastError());
            return 0;
        }
    }
    trace("input_ended", GetLastError());
    send_frame(UURB_TERMINAL_FRAME_EOF, NULL, 0);
    return 0;
}

/* Linux PTY output to UU; the broker closes the socket when the shell ends. */
static DWORD WINAPI output_pump(LPVOID unused)
{
    char buffer[16384];
    DWORD written;
    int received;

    (void)unused;
    while ((received = recv(session.socket, buffer, sizeof(buffer), 0)) > 0) {
        int offset = 0;
        while (offset < received) {
            if (!WriteFile(session.bridge_output, buffer + offset,
                           (DWORD)(received - offset), &written, NULL) || written == 0) {
                trace("output_write_failed", GetLastError());
                goto done;
            }
            offset += (int)written;
        }
    }
    trace("broker_closed", received < 0 ? (DWORD)WSAGetLastError() : 0);
done:
    SetEvent(session.done);
    return 0;
}

/* The placeholder child's console output is never shown, but conhost stalls
 * if nobody reads it. */
static DWORD WINAPI console_drain(LPVOID unused)
{
    char buffer[4096];
    DWORD received;

    (void)unused;
    while (ReadFile(session.console_output, buffer, sizeof(buffer), &received, NULL) &&
           received > 0)
        ;
    return 0;
}

static HRESULT direct_create(COORD size, HANDLE input, HANDLE output,
                             DWORD flags, pseudo_console *console)
{
    HANDLE process = GetCurrentProcess();
    HANDLE child_input = NULL;
    HANDLE child_output = NULL;
    wchar_t event_name[64];
    HRESULT result;

    if (session.console != NULL)
        return E_FAIL;
    session.socket = connect_broker(size);
    if (session.socket == INVALID_SOCKET)
        return E_FAIL;
    InitializeCriticalSection(&session.send_lock);
    swprintf(event_name, 64, L"Local\\uurb-conpty-%lu", GetCurrentProcessId());
    session.done = CreateEventW(NULL, TRUE, FALSE, event_name);
    if (session.done == NULL ||
        !DuplicateHandle(process, input, process, &session.bridge_input, 0, FALSE,
                         DUPLICATE_SAME_ACCESS) ||
        !DuplicateHandle(process, output, process, &session.bridge_output, 0, FALSE,
                         DUPLICATE_SAME_ACCESS) ||
        !CreatePipe(&child_input, &session.console_input, NULL, 0) ||
        !CreatePipe(&session.console_output, &child_output, NULL, 0))
        goto failed;
    result = wine_create(size, child_input, child_output, flags, &session.console);
    close_handle(&child_input);
    close_handle(&child_output);
    if (FAILED(result))
        goto failed;
    /* The child waits on this before exiting; see uu_terminal_proxy.c. */
    SetEnvironmentVariableW(L"UURB_CONPTY_SESSION_EVENT", event_name);
    session.threads[0] = CreateThread(NULL, 0, input_pump, NULL, 0, NULL);
    session.threads[1] = CreateThread(NULL, 0, output_pump, NULL, 0, NULL);
    session.threads[2] = CreateThread(NULL, 0, console_drain, NULL, 0, NULL);
    *console = session.console;
    return S_OK;

failed:
    close_handle(&child_input);
    close_handle(&child_output);
    close_handle(&session.bridge_input);
    close_handle(&session.bridge_output);
    close_handle(&session.console_input);
    close_handle(&session.console_output);
    close_handle(&session.done);
    closesocket(session.socket);
    session.socket = INVALID_SOCKET;
    session.console = NULL;
    DeleteCriticalSection(&session.send_lock);
    return E_FAIL;
}

HRESULT WINAPI shim_create(COORD size, HANDLE input, HANDLE output,
                           DWORD flags, pseudo_console *console)
{
    HRESULT result;

    trace("create_flags", flags);
    if (SUCCEEDED(direct_create(size, input, output, flags, console))) {
        trace("direct_session", (DWORD)size.X << 16 | (DWORD)size.Y);
        return S_OK;
    }
    result = wine_create(size, input, output, flags, console);
    trace("wine_fallback", (DWORD)result);
    return result;
}

HRESULT WINAPI shim_resize(pseudo_console console, COORD size)
{
    resize_function resize =
        (resize_function)(void *)kernel_function("ResizePseudoConsole");

    trace("resize", (DWORD)size.X << 16 | (DWORD)size.Y);
    if (console != NULL && console == session.console) {
        uint16_t dimensions[2] = {htons((uint16_t)size.X), htons((uint16_t)size.Y)};
        send_frame(UURB_TERMINAL_FRAME_RESIZE, dimensions, sizeof(dimensions));
    }
    return resize != NULL ? resize(console, size) : E_NOTIMPL;
}

void WINAPI shim_close(pseudo_console console)
{
    close_function close =
        (close_function)(void *)kernel_function("ClosePseudoConsole");

    trace("close", console != NULL && console == session.console);
    if (console != NULL && console == session.console) {
        /* Hanging up the socket ends the Linux shell. */
        shutdown(session.socket, SD_BOTH);
        closesocket(session.socket);
        SetEvent(session.done);
        close_handle(&session.bridge_input);
        close_handle(&session.bridge_output);
        close_handle(&session.console_input);
        WaitForMultipleObjects(3, session.threads, TRUE, 2000);
        for (int index = 0; index < 3; index++)
            close_handle(&session.threads[index]);
        close_handle(&session.console_output);
        session.console = NULL;
    }
    if (close != NULL)
        close(console);
}
