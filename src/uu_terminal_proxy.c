#define WIN32_LEAN_AND_MEAN
#include <winsock2.h>
#include <windows.h>

#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <errno.h>
#include <limits.h>

#include "terminal_bridge_protocol.h"

static SOCKET terminal_socket = INVALID_SOCKET;
static CRITICAL_SECTION send_lock;
static HANDLE stop_event;
/* UU's conpty helper passes the pipe ends explicitly.  Wine does not always
 * populate the child's CRT standard handles for those inherited handles, so
 * keep the parsed values separate from GetStdHandle(). */
static HANDLE proxy_input = NULL;
static HANDLE proxy_output = NULL;
static HANDLE proxy_control = NULL;
static uint16_t requested_columns = 80;
static uint16_t requested_rows = 24;
static int proxy_display_name_present;
static int proxy_handle_args_present;
static int proxy_argument_errors;
static int proxy_mux_args_present;
static int proxy_mux_attach_present;
static int proxy_mux_config_present;
static char proxy_mux_session[192];
static char proxy_mux_config[320];
/* Phase-only diagnostics beside this reviewed proxy.  No command,
 * token, port, handle or terminal byte is written. Each line is a single
 * append, with an identity and timestamp so concurrent retries are separable. */
static void trace_event(const char *event)
{
    char path[MAX_PATH], *slash;
    char line[512];
    SYSTEMTIME now;
    DWORD length, written;
    DWORD saved_error = GetLastError();
    HANDLE file;
    int line_length;
    length = GetModuleFileNameA(NULL, path, sizeof(path));
    if (!length || length >= sizeof(path) - 32) goto done;
    slash = strrchr(path, '\\');
    if (!slash) slash = strrchr(path, '/');
    if (!slash) goto done;
    strcpy(slash + 1, "uu-terminal-proxy.trace");
    file = CreateFileA(path, FILE_APPEND_DATA, FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE,
                       NULL, OPEN_ALWAYS, FILE_ATTRIBUTE_NORMAL, NULL);
    if (file == INVALID_HANDLE_VALUE) goto done;
    GetSystemTime(&now);
    line_length = snprintf(line, sizeof(line),
        "%04u-%02u-%02uT%02u:%02u:%02u.%03uZ pid=%lu tid=%lu %s\r\n",
        now.wYear, now.wMonth, now.wDay, now.wHour, now.wMinute,
        now.wSecond, now.wMilliseconds, (unsigned long)GetCurrentProcessId(),
        (unsigned long)GetCurrentThreadId(), event);
    if (line_length > 0 && line_length < (int)sizeof(line))
        WriteFile(file, line, (DWORD)line_length, &written, NULL);
    CloseHandle(file);
done:
    SetLastError(saved_error);
}

static void trace_error(const char *label, DWORD error)
{
    char line[160];
    snprintf(line, sizeof(line), "%s error=%lu", label, (unsigned long)error);
    trace_event(line);
}

static void trace_handle_status(const char *label, HANDLE handle)
{
    DWORD saved = GetLastError(), flags = 0;
    SetLastError(ERROR_SUCCESS);
    DWORD type = GetFileType(handle), error = GetLastError();
    BOOL valid = GetHandleInformation(handle, &flags);
    char line[192];
    snprintf(line, sizeof(line), "%s valid=%u type=%lu inherited=%u error=%lu",
             label, (unsigned)valid, (unsigned long)type,
             (unsigned)((flags & HANDLE_FLAG_INHERIT) != 0),
             (unsigned long)(valid ? error : GetLastError()));
    trace_event(line);
    SetLastError(saved);
}

static void trace_dimensions(const char *label, uint16_t columns, uint16_t rows)
{
    char line[128];
    snprintf(line, sizeof(line), "%s columns=%u rows=%u", label, columns, rows);
    trace_event(line);
}

static void trace_mux_option(const char *option)
{
    unsigned long hash = 2166136261UL;
    size_t index;
    char line[96];

    for (index = 0; option[index] != '\0'; index++) {
        hash ^= (unsigned char)option[index];
        hash *= 16777619UL;
    }
    _snprintf(line, sizeof(line), "mux_option_hash_%08lx_len_%u",
              hash, (unsigned)index);
    trace_event(line);
}

static void write_error(const char *message)
{
    DWORD written;
    HANDLE error_handle = GetStdHandle(STD_ERROR_HANDLE);

    if (error_handle != NULL && error_handle != INVALID_HANDLE_VALUE) {
        WriteFile(error_handle, message, (DWORD)strlen(message), &written, NULL);
        WriteFile(error_handle, "\r\n", 2, &written, NULL);
    }
}

static int send_all(const void *buffer, size_t size)
{
    const char *cursor = (const char *)buffer;

    while (size > 0) {
        int chunk = size > INT_MAX ? INT_MAX : (int)size;
        int sent = send(terminal_socket, cursor, chunk, 0);

        if (sent <= 0)
            return 0;
        cursor += sent;
        size -= (size_t)sent;
    }
    return 1;
}

static int receive_all(void *buffer, size_t size)
{
    char *cursor = (char *)buffer;

    while (size > 0) {
        int chunk = size > INT_MAX ? INT_MAX : (int)size;
        int received = recv(terminal_socket, cursor, chunk, 0);

        if (received <= 0)
            return 0;
        cursor += received;
        size -= (size_t)received;
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
    EnterCriticalSection(&send_lock);
    result = send_all(&frame, sizeof(frame));
    if (result && length > 0)
        result = send_all(payload, length);
    LeaveCriticalSection(&send_lock);
    return result;
}

static void console_size(uint16_t *columns, uint16_t *rows)
{
    CONSOLE_SCREEN_BUFFER_INFO info;
    HANDLE output = proxy_output != NULL ? proxy_output
                                         : GetStdHandle(STD_OUTPUT_HANDLE);
    SHORT width;
    SHORT height;

    *columns = requested_columns;
    *rows = requested_rows;
    if (!GetConsoleScreenBufferInfo(output, &info))
        return;
    width = (SHORT)(info.srWindow.Right - info.srWindow.Left + 1);
    height = (SHORT)(info.srWindow.Bottom - info.srWindow.Top + 1);
    if (width > 0)
        *columns = (uint16_t)width;
    if (height > 0)
        *rows = (uint16_t)height;
}

/* Inside a psmux pane this proxy owns a real Wine console. Its default
 * cooked mode line-buffers and echoes input and decodes output as the OEM
 * code page, so switch it to a raw UTF-8 VT byte stream like a PTY. */
static void configure_console(void)
{
    HANDLE input = GetStdHandle(STD_INPUT_HANDLE);
    HANDLE output = GetStdHandle(STD_OUTPUT_HANDLE);
    DWORD mode;

    if (proxy_input == NULL && GetConsoleMode(input, &mode)) {
        SetConsoleCP(CP_UTF8);
        mode &= ~(DWORD)(ENABLE_LINE_INPUT | ENABLE_ECHO_INPUT |
                         ENABLE_PROCESSED_INPUT | ENABLE_MOUSE_INPUT |
                         ENABLE_WINDOW_INPUT | ENABLE_QUICK_EDIT_MODE);
        if (!SetConsoleMode(input, mode | ENABLE_EXTENDED_FLAGS |
                            ENABLE_VIRTUAL_TERMINAL_INPUT))
            trace_error("console_input_mode_failed", GetLastError());
    }
    if (proxy_output == NULL && GetConsoleMode(output, &mode)) {
        SetConsoleOutputCP(CP_UTF8);
        if (!SetConsoleMode(output, ENABLE_PROCESSED_OUTPUT |
                            ENABLE_VIRTUAL_TERMINAL_PROCESSING |
                            DISABLE_NEWLINE_AUTO_RETURN))
            trace_error("console_output_mode_failed", GetLastError());
    }
}

static DWORD WINAPI input_worker(LPVOID unused)
{
    unsigned char buffer[16384];
    DWORD received;
    int input_seen = 0;
    HANDLE input = proxy_input != NULL ? proxy_input
                                       : GetStdHandle(STD_INPUT_HANDLE);

    (void)unused;
    while (WaitForSingleObject(stop_event, 0) == WAIT_TIMEOUT) {
        if (!ReadFile(input, buffer, sizeof(buffer), &received, NULL)) {
            trace_error("input_read_failed", GetLastError());
            send_frame(UURB_TERMINAL_FRAME_EOF, NULL, 0);
            return 0;
        }
        if (received == 0) {
            trace_event("input_eof");
            send_frame(UURB_TERMINAL_FRAME_EOF, NULL, 0);
            return 0;
        }
        if (!send_frame(UURB_TERMINAL_FRAME_DATA, buffer, received)) {
            trace_error("input_forward_failed", WSAGetLastError());
            return 1;
        }
        if (!input_seen) {
            char line[96];
            snprintf(line, sizeof(line), "input_forwarded bytes=%lu", (unsigned long)received);
            trace_event(line);
            input_seen = 1;
        }
    }
    return 0;
}

static int read_handle_all(HANDLE handle, void *data, DWORD length)
{
    unsigned char *cursor = data;
    while (length > 0) {
        DWORD received;
        if (!ReadFile(handle, cursor, length, &received, NULL)) {
            trace_error("control_read_failed", GetLastError());
            return 0;
        }
        if (!received) {
            trace_event("control_eof");
            return 0;
        }
        cursor += received;
        length -= received;
    }
    return 1;
}

static DWORD WINAPI resize_worker(LPVOID unused)
{
    uint16_t columns;
    uint16_t rows;
    uint16_t previous_columns = 0;
    uint16_t previous_rows = 0;
    uint16_t dimensions[2];

    (void)unused;
    while (WaitForSingleObject(stop_event, 250) == WAIT_TIMEOUT) {
        console_size(&columns, &rows);
        if (columns == previous_columns && rows == previous_rows)
            continue;
        previous_columns = columns;
        previous_rows = rows;
        dimensions[0] = htons(columns);
        dimensions[1] = htons(rows);
        if (!send_frame(UURB_TERMINAL_FRAME_RESIZE, dimensions,
                        sizeof(dimensions)))
            return 1;
    }
    return 0;
}

/* conpty_bridge's ctl pipe carries a tiny private control stream.  The
 * vendor helper reads one byte followed by a little-endian uint16 column and
 * row for type 1 (resize), and type 2 requests shutdown.  Forward resize to
 * the authenticated Linux broker so UU's dynamic terminal-size updates work
 * even when stdout is an ordinary pipe rather than a console. */
static DWORD WINAPI control_worker(LPVOID unused)
{
    unsigned char type;
    unsigned char dimensions[4];
    uint16_t network_dimensions[2];

    (void)unused;
    if (proxy_control == NULL)
        return 0;
    while (WaitForSingleObject(stop_event, 0) == WAIT_TIMEOUT) {
        if (!read_handle_all(proxy_control, &type, 1))
            break;
        if (type == 2) {
            trace_event("control_exit");
            SetEvent(stop_event);
            shutdown(terminal_socket, SD_BOTH);
            break;
        }
        if (type != 1) {
            trace_error("control_unknown_type", type);
            break;
        }
        if (!read_handle_all(proxy_control, dimensions, sizeof(dimensions)))
            break;
        if ((((uint16_t)dimensions[0] | ((uint16_t)dimensions[1] << 8)) == 0) ||
            (((uint16_t)dimensions[2] | ((uint16_t)dimensions[3] << 8)) == 0))
            continue;
        network_dimensions[0] = htons((uint16_t)dimensions[0] |
                                      ((uint16_t)dimensions[1] << 8));
        network_dimensions[1] = htons((uint16_t)dimensions[2] |
                                      ((uint16_t)dimensions[3] << 8));
        if (!send_frame(UURB_TERMINAL_FRAME_RESIZE, network_dimensions,
                        sizeof(network_dimensions))) {
            trace_error("control_resize_failed", WSAGetLastError());
            break;
        }
        trace_dimensions("control_resize_forwarded", ntohs(network_dimensions[0]),
                         ntohs(network_dimensions[1]));
    }
    return 0;
}

static int parse_port(const char *value, uint16_t *port)
{
    char *end = NULL;
    unsigned long parsed;

    if (value == NULL || *value == '\0')
        return 0;
    parsed = strtoul(value, &end, 10);
    if (*end != '\0' || parsed == 0 || parsed > 65535)
        return 0;
    *port = (uint16_t)parsed;
    return 1;
}

static int parse_handle_value(const char *value, HANDLE *handle)
{
    char *end = NULL;
    unsigned long long parsed;

    if (value == NULL || !( (*value >= '0' && *value <= '9') ||
                           (*value >= 'a' && *value <= 'f') ||
                           (*value >= 'A' && *value <= 'F')))
        return 0;
    errno = 0;
    /* conpty_bridge formats inherited handles in hexadecimal, including
     * digit-only strings without a 0x prefix.  Base 0 silently misroutes
     * those handles ("100" is handle 0x100, never decimal 100). */
    parsed = strtoull(value, &end, 16);
    if (errno == ERANGE || end == value || *end != '\0' || parsed == 0 ||
        parsed == (unsigned long long)UINTPTR_MAX)
        return 0;
    if (sizeof(uintptr_t) < sizeof(parsed) &&
        parsed > (unsigned long long)UINTPTR_MAX)
        return 0;
    *handle = (HANDLE)(uintptr_t)parsed;
    return 1;
}

static int parse_dimension(const char *value, uint16_t *dimension)
{
    char *end = NULL;
    unsigned long parsed;

    if (value == NULL || *value == '\0')
        return 0;
    errno = 0;
    parsed = strtoul(value, &end, 10);
    if (errno == ERANGE || end == value || *end != '\0' ||
        parsed == 0 || parsed > 1000)
        return 0;
    *dimension = (uint16_t)parsed;
    return 1;
}

/* Parse the switches used by the stock conpty_bridge.exe.  Unknown switches
 * are deliberately ignored: newer UU releases add attach/display flags that
 * do not affect this stdio compatibility layer. */
static void parse_bridge_arguments(int argc, char **argv)
{
    int index;

    for (index = 1; index < argc; index++) {
        const char *argument = argv[index];
        const char *value = NULL;
        HANDLE *destination = NULL;
        uint16_t *dimension = NULL;
        char option[32];
        const char *equals;
        size_t option_length;

        if (argument == NULL || strncmp(argument, "--", 2) != 0)
            continue;
        equals = strchr(argument + 2, '=');
        option_length = equals != NULL ? (size_t)(equals - (argument + 2))
                                       : strlen(argument + 2);
        if (option_length == 0 || option_length >= sizeof(option))
            continue;
        memcpy(option, argument + 2, option_length);
        option[option_length] = '\0';
        if (strncmp(option, "uuyc-mux-", 9) == 0) {
            proxy_mux_args_present = 1;
            trace_mux_option(option);
        }
        if (strcmp(option, "uuyc-mux-attach-existing") == 0)
            proxy_mux_attach_present = 1;
        if (strcmp(option, "uuyc-mux-config") == 0)
            proxy_mux_config_present = 1;
        if (strcmp(option, "uuyc-mux-session") == 0) {
            const char *session_value = equals != NULL ? equals + 1 :
                                        (index + 1 < argc ? argv[++index] : NULL);
            if (session_value != NULL && strlen(session_value) < sizeof(proxy_mux_session))
                strcpy(proxy_mux_session, session_value);
            continue;
        }
        if (strcmp(option, "uuyc-mux-config") == 0) {
            const char *config_value = equals != NULL ? equals + 1 :
                                       (index + 1 < argc ? argv[++index] : NULL);
            if (config_value != NULL && strlen(config_value) < sizeof(proxy_mux_config))
                strcpy(proxy_mux_config, config_value);
            continue;
        }
        if (strcmp(option, "stdin-handle") == 0)
            destination = &proxy_input;
        else if (strcmp(option, "stdout-handle") == 0)
            destination = &proxy_output;
        else if (strcmp(option, "ctl-handle") == 0)
            destination = &proxy_control;
        else if (strcmp(option, "cols") == 0)
            dimension = &requested_columns;
        else if (strcmp(option, "rows") == 0)
            dimension = &requested_rows;
        else if (strcmp(option, "display-name") == 0) {
            if (equals != NULL || index + 1 < argc)
                proxy_display_name_present = 1;
            continue;
        }
        if (strcmp(option, "stdin-handle") == 0 ||
            strcmp(option, "stdout-handle") == 0 ||
            strcmp(option, "ctl-handle") == 0)
            proxy_handle_args_present = 1;
        else if (dimension == NULL)
            continue;
        if (equals != NULL) {
            value = equals + 1;
        } else if (index + 1 < argc && argv[index + 1] != NULL &&
                   strncmp(argv[index + 1], "--", 2) != 0) {
            value = argv[++index];
        }
        if (destination != NULL) {
            HANDLE parsed;
            if (parse_handle_value(value, &parsed))
                *destination = parsed;
            else {
                proxy_argument_errors = 1;
                trace_error("invalid_handle_argument", ERROR_INVALID_PARAMETER);
            }
        } else if (dimension != NULL) {
            if (!parse_dimension(value, dimension)) {
                proxy_argument_errors = 1;
                trace_error("invalid_dimension_argument", ERROR_INVALID_PARAMETER);
            }
        }
    }
}

static int token_is_valid(const char *token)
{
    size_t index;

    if (token == NULL || strlen(token) != UURB_TERMINAL_TOKEN_LENGTH)
        return 0;
    for (index = 0; index < UURB_TERMINAL_TOKEN_LENGTH; index++) {
        if (!((token[index] >= '0' && token[index] <= '9') ||
              (token[index] >= 'a' && token[index] <= 'f')))
            return 0;
    }
    return 1;
}

static int load_environment_configuration(char *token, uint16_t *port)
{
    char port_text[16];
    DWORD token_length;
    DWORD port_length;

    token_length = GetEnvironmentVariableA(
        "UURB_TERMINAL_BRIDGE_TOKEN", token,
        UURB_TERMINAL_TOKEN_LENGTH + 1);
    port_length = GetEnvironmentVariableA(
        "UURB_TERMINAL_BRIDGE_PORT", port_text, sizeof(port_text));
    return token_length == UURB_TERMINAL_TOKEN_LENGTH &&
           port_length > 0 && port_length < sizeof(port_text) &&
           token_is_valid(token) && parse_port(port_text, port);
}

static int runtime_configuration_path(char *path, size_t path_size)
{
    char *backslash;
    char *separator;
    char *slash;
    DWORD length;
    size_t directory_length;
    size_t filename_length = strlen(UURB_TERMINAL_CONFIG_FILENAME);

    length = GetModuleFileNameA(NULL, path, (DWORD)path_size);
    if (length == 0 || length >= path_size)
        return 0;
    backslash = strrchr(path, '\\');
    slash = strrchr(path, '/');
    separator = backslash;
    if (slash != NULL && (separator == NULL || slash > separator))
        separator = slash;
    if (separator == NULL)
        return 0;
    directory_length = (size_t)(separator - path) + 1;
    if (directory_length + filename_length + 1 > path_size)
        return 0;
    memcpy(path + directory_length, UURB_TERMINAL_CONFIG_FILENAME,
           filename_length + 1);
    return 1;
}

static int load_runtime_configuration(char *token, uint16_t *port)
{
    static const char prefix[] = "version=1\nport=";
    char config[256];
    char path[32768];
    char port_text[16];
    char *cursor;
    char *newline;
    DWORD received;
    HANDLE file = INVALID_HANDLE_VALUE;
    LARGE_INTEGER size;
    size_t port_length;
    int result = 0;

    if (!runtime_configuration_path(path, sizeof(path)))
        goto done;
    file = CreateFileA(path, GENERIC_READ,
                       FILE_SHARE_READ | FILE_SHARE_DELETE, NULL,
                       OPEN_EXISTING, FILE_ATTRIBUTE_NORMAL, NULL);
    if (file == INVALID_HANDLE_VALUE ||
        !GetFileSizeEx(file, &size) || size.QuadPart <= 0 ||
        size.QuadPart >= (LONGLONG)sizeof(config) ||
        !ReadFile(file, config, (DWORD)size.QuadPart, &received, NULL) ||
        received != (DWORD)size.QuadPart)
        goto done;
    config[received] = '\0';
    if (strncmp(config, prefix, sizeof(prefix) - 1) != 0)
        goto done;
    cursor = config + sizeof(prefix) - 1;
    newline = strchr(cursor, '\n');
    if (newline == NULL)
        goto done;
    port_length = (size_t)(newline - cursor);
    if (port_length == 0 || port_length >= sizeof(port_text))
        goto done;
    memcpy(port_text, cursor, port_length);
    port_text[port_length] = '\0';
    cursor = newline + 1;
    if (strncmp(cursor, "token=", 6) != 0)
        goto done;
    cursor += 6;
    if (strlen(cursor) != UURB_TERMINAL_TOKEN_LENGTH + 1 ||
        cursor[UURB_TERMINAL_TOKEN_LENGTH] != '\n')
        goto done;
    memcpy(token, cursor, UURB_TERMINAL_TOKEN_LENGTH);
    token[UURB_TERMINAL_TOKEN_LENGTH] = '\0';
    result = token_is_valid(token) && parse_port(port_text, port);

done:
    if (file != INVALID_HANDLE_VALUE)
        CloseHandle(file);
    SecureZeroMemory(config, sizeof(config));
    SecureZeroMemory(path, sizeof(path));
    SecureZeroMemory(port_text, sizeof(port_text));
    if (!result)
        SecureZeroMemory(token, UURB_TERMINAL_TOKEN_LENGTH + 1);
    return result;
}

static int load_configuration(char *token, uint16_t *port)
{
    if (load_environment_configuration(token, port))
        return 1;
    SecureZeroMemory(token, UURB_TERMINAL_TOKEN_LENGTH + 1);
    return load_runtime_configuration(token, port);
}

/* UU 4.39's conpty_bridge starts its psmux session through
 *   powershell -Command "...; & '<bin>\uuyc-mux.exe' args; $x = $LASTEXITCODE;
 *   if ($x -ne 0) { exit $x }; ...; exit $LASTEXITCODE"
 * Wine has no PowerShell, so interpret exactly that fixed grammar. Only a
 * uuyc-mux.exe is ever started; any other statement rejects the whole script
 * instead of being evaluated. */
#define MUX_SCRIPT_REJECTED (-1)
#define MUX_MAX_TOKENS 64

static const wchar_t *mux_script_from_command_line(const wchar_t *line,
                                                   size_t *length)
{
    const wchar_t *cursor;
    const wchar_t *end;
    const wchar_t *mux;

    for (cursor = line; *cursor; cursor++) {
        if (_wcsnicmp(cursor, L"-Command", 8) == 0 &&
            (cursor[8] == L' ' || cursor[8] == L'\t'))
            break;
    }
    if (!*cursor)
        return NULL;
    cursor += 8;
    while (*cursor == L' ' || *cursor == L'\t')
        cursor++;
    if (*cursor == L'"') {
        end = wcschr(++cursor, L'"');
        if (end == NULL)
            return NULL;
    } else {
        end = cursor + wcslen(cursor);
    }
    *length = (size_t)(end - cursor);
    mux = wcsstr(cursor, L"uuyc-mux.exe");
    return mux != NULL && mux < end ? cursor : NULL;
}

/* Split one statement into words; single quotes group, '' is a literal '. */
static int mux_tokenize(const wchar_t **cursor, const wchar_t *end,
                        wchar_t tokens[][512], int *count)
{
    *count = 0;
    while (*cursor < end) {
        wchar_t *token;
        size_t used = 0;

        while (*cursor < end && (**cursor == L' ' || **cursor == L'\t'))
            (*cursor)++;
        if (*cursor >= end)
            break;
        if (**cursor == L';') {
            (*cursor)++;
            break;
        }
        if (*count >= MUX_MAX_TOKENS)
            return 0;
        token = tokens[(*count)++];
        if (**cursor == L'\'') {
            for ((*cursor)++; ; (*cursor)++) {
                if (*cursor >= end)
                    return 0;
                if (**cursor == L'\'') {
                    if (*cursor + 1 < end && (*cursor)[1] == L'\'')
                        (*cursor)++;
                    else {
                        (*cursor)++;
                        break;
                    }
                }
                if (used + 1 >= 512)
                    return 0;
                token[used++] = **cursor;
            }
        } else {
            while (*cursor < end && **cursor != L' ' && **cursor != L'\t' &&
                   **cursor != L';') {
                if (used + 1 >= 512)
                    return 0;
                token[used++] = *(*cursor)++;
            }
        }
        token[used] = L'\0';
    }
    return 1;
}

static int mux_append_argument(wchar_t *line, size_t size, size_t *used,
                               const wchar_t *argument)
{
    size_t backslashes = 0;
    const wchar_t *cursor;
    int quote = *argument == L'\0' || wcspbrk(argument, L" \t\"") != NULL;

#define MUX_PUT(ch) do { if (*used + 2 >= size) return 0; line[(*used)++] = (ch); } while (0)
    if (*used > 0)
        MUX_PUT(L' ');
    if (quote)
        MUX_PUT(L'"');
    for (cursor = argument; *cursor; cursor++) {
        if (*cursor == L'\\') {
            backslashes++;
        } else if (*cursor == L'"') {
            for (backslashes = backslashes * 2 + 1; backslashes; backslashes--)
                MUX_PUT(L'\\');
        } else {
            backslashes = 0;
        }
        MUX_PUT(*cursor);
    }
    if (quote) {
        for (; backslashes; backslashes--)
            MUX_PUT(L'\\');
        MUX_PUT(L'"');
    }
#undef MUX_PUT
    line[*used] = L'\0';
    return 1;
}

static int mux_is_executable(const wchar_t *path)
{
    const wchar_t *base = wcsrchr(path, L'\\');

    base = base != NULL ? base + 1 : path;
    return _wcsicmp(base, L"uuyc-mux.exe") == 0 &&
           GetFileAttributesW(path) != INVALID_FILE_ATTRIBUTES;
}

/* UU's psmux config hides the session name from panes (expose-mux-environment
 * off), so pass it to the pane's command: `new ... -s NAME -- powershell.exe
 * ...` gains `-UurbSession NAME` for anchor_session. */
#define MUX_PANE_SESSION_ARGUMENT "-UurbSession"

static void mux_name_pane(wchar_t tokens[][512], int *count)
{
    const wchar_t *name = NULL;
    int creates = 0;
    int separator = 0;

    for (int index = 2; index < *count; index++) {
        if (wcscmp(tokens[index], L"new") == 0 || wcscmp(tokens[index], L"new-session") == 0)
            creates = 1;
        else if (wcscmp(tokens[index], L"-s") == 0 && index + 1 < *count && !separator)
            name = tokens[index + 1];
        else if (wcscmp(tokens[index], L"--") == 0)
            separator = 1;
    }
    if (!creates || name == NULL || !separator || *count + 2 > MUX_MAX_TOKENS ||
        wcslen(name) > UURB_TERMINAL_MAX_SESSION_NAME)
        return;
    wcscpy(tokens[*count], L"" MUX_PANE_SESSION_ARGUMENT);
    wcscpy(tokens[*count + 1], name);
    *count += 2;
}

/* When uu_conpty_shim.c has joined UU's pipes straight to the Linux PTY, the
 * processes conpty_bridge and psmux start only need to live as long as that
 * session. Block until the shell ends, the bridge exits, or `child` (when
 * given) exits. Returns 0 when no such session is announced in the
 * environment, 1 when the session ended, and 2 when `child` ended first. */
static int wait_direct_session(HANDLE child)
{
    wchar_t event_name[64];
    const wchar_t *pid;
    HANDLE waits[3] = {NULL, NULL, NULL};
    HANDLE bridge = NULL;
    DWORD count = 1;
    DWORD signaled;

    if (GetEnvironmentVariableW(L"UURB_CONPTY_SESSION_EVENT", event_name, 64) - 1 >= 63 ||
        (waits[0] = OpenEventW(SYNCHRONIZE, FALSE, event_name)) == NULL)
        return 0;
    /* The name carries conpty_bridge's pid. */
    pid = wcsrchr(event_name, L'-');
    if (pid != NULL &&
        (bridge = OpenProcess(SYNCHRONIZE, FALSE, wcstoul(pid + 1, NULL, 10))) != NULL)
        waits[count++] = bridge;
    if (child != NULL)
        waits[count++] = child;
    trace_event("direct_session_wait");
    signaled = WaitForMultipleObjects(count, waits, FALSE, INFINITE);
    if (bridge != NULL)
        CloseHandle(bridge);
    CloseHandle(waits[0]);
    return child != NULL && signaled == WAIT_OBJECT_0 + count - 1 ? 2 : 1;
}

static int mux_run(wchar_t tokens[][512], int count, DWORD *exit_code)
{
    static wchar_t line[32768];
    size_t used = 0;
    STARTUPINFOW startup;
    PROCESS_INFORMATION process;
    int index;

    if (count < 2 || !mux_is_executable(tokens[1]))
        return 0;
    for (index = 1; index < count; index++) {
        if (!mux_append_argument(line, sizeof(line) / sizeof(line[0]), &used,
                                 tokens[index]))
            return 0;
    }
    /* The bridge starts this proxy on its pseudoconsole with empty standard
     * handles; reusing them would give `attach` an instant EOF. Hand the
     * console itself to uuyc-mux instead. */
    SECURITY_ATTRIBUTES inherit = {sizeof(inherit), NULL, TRUE};
    HANDLE console_in = CreateFileW(L"CONIN$", GENERIC_READ | GENERIC_WRITE,
                                    FILE_SHARE_READ | FILE_SHARE_WRITE, &inherit,
                                    OPEN_EXISTING, 0, NULL);
    HANDLE console_out = CreateFileW(L"CONOUT$", GENERIC_READ | GENERIC_WRITE,
                                     FILE_SHARE_READ | FILE_SHARE_WRITE, &inherit,
                                     OPEN_EXISTING, 0, NULL);
    int started;

    memset(&startup, 0, sizeof(startup));
    startup.cb = sizeof(startup);
    if (console_in != INVALID_HANDLE_VALUE && console_out != INVALID_HANDLE_VALUE) {
        startup.dwFlags = STARTF_USESTDHANDLES;
        startup.hStdInput = console_in;
        startup.hStdOutput = console_out;
        startup.hStdError = console_out;
    }
    started = CreateProcessW(tokens[1], line, NULL, NULL, TRUE, 0, NULL, NULL,
                             &startup, &process);
    if (!started)
        trace_error("mux_start_failed", GetLastError());
    if (console_in != INVALID_HANDLE_VALUE)
        CloseHandle(console_in);
    if (console_out != INVALID_HANDLE_VALUE)
        CloseHandle(console_out);
    if (!started)
        return 0;
    CloseHandle(process.hThread);
    if (wait_direct_session(process.hProcess) == 1) {
        /* A direct session ended while a step (the idle `attach`) still ran. */
        TerminateProcess(process.hProcess, 0);
    }
    WaitForSingleObject(process.hProcess, INFINITE);
    if (!GetExitCodeProcess(process.hProcess, exit_code))
        *exit_code = 1;
    CloseHandle(process.hProcess);
    return 1;
}

static int run_mux_script(const wchar_t *script, size_t length)
{
    static wchar_t tokens[MUX_MAX_TOKENS][512];
    const wchar_t *cursor = script;
    const wchar_t *end = script + length;
    DWORD last_exit = 0;
    int count;
    char line[64];

    trace_event("mux_script_start");
    /* psmux otherwise pre-spawns a spare pane and later hands it to a new
     * session under the spare's name, which would break anchor_session. */
    SetEnvironmentVariableW(L"PSMUX_NO_WARM", L"1");
    while (cursor < end) {
        if (!mux_tokenize(&cursor, end, tokens, &count))
            return MUX_SCRIPT_REJECTED;
        if (count == 0)
            continue;
        if (wcscmp(tokens[0], L"&") == 0) {
            /* Every step runs as scripted: UU treats the terminal as created
             * only once a client is attached to the psmux session. With a
             * direct session this process sits on the shim's private console,
             * so `attach` renders nowhere and the raw stream stays direct. */
            const wchar_t *program = count > 1 ? wcsrchr(tokens[1], L'\\') : NULL;

            /* UU 4.42 runs chcp by path: `& '...\chcp.com' 65001 | Out-Null`. */
            if (count > 1 && _wcsicmp(program != NULL ? program + 1 : tokens[1], L"chcp.com") == 0) {
                SetConsoleCP(CP_UTF8);
                SetConsoleOutputCP(CP_UTF8);
                last_exit = 0;
                continue;
            }
            mux_name_pane(tokens, &count);
            if (!mux_run(tokens, count, &last_exit))
                return MUX_SCRIPT_REJECTED;
            snprintf(line, sizeof(line), "mux_step exit=%lu", (unsigned long)last_exit);
            trace_event(line);
        } else if (wcscmp(tokens[0], L"exit") == 0 && count == 2 && tokens[1][0] == L'$') {
            return (int)last_exit;
        } else if (wcscmp(tokens[0], L"if") == 0 && count >= 6 &&
                   wcscmp(tokens[2], L"-ne") == 0 && wcscmp(tokens[3], L"0)") == 0 &&
                   wcscmp(tokens[4], L"{") == 0 && wcscmp(tokens[5], L"exit") == 0) {
            if (last_exit != 0)
                return (int)last_exit;
        } else if (tokens[0][0] == L'$' && count == 3 && wcscmp(tokens[1], L"=") == 0 &&
                   _wcsicmp(tokens[2], L"$LASTEXITCODE") == 0) {
            /* The script copies $LASTEXITCODE into a named variable. */
        } else if (_wcsnicmp(tokens[0], L"[Console]::", 11) == 0 ||
                   _wcsicmp(tokens[0], L"$OutputEncoding") == 0) {
            /* Encoding setup: Wine consoles already pass UTF-8 through. */
        } else if (_wcsicmp(tokens[0], L"chcp") == 0) {
            SetConsoleCP(CP_UTF8);
            SetConsoleOutputCP(CP_UTF8);
        } else {
            return MUX_SCRIPT_REJECTED;
        }
    }
    return (int)last_exit;
}

/* A psmux pane created by run_mux_script anchors the persistent Linux session
 * of the same name, which uu_conpty_shim.c created for the viewer: the shell
 * lives while UU keeps the pane, and the pane ends with the shell, as a
 * native pane would. Returns 0 when there is no such session, so the pane
 * runs its own shell. */
static int anchor_session(const char *token, uint16_t port, const char *name)
{
    struct uurb_terminal_hello hello;
    struct uurb_terminal_session session;
    struct sockaddr_in address;
    unsigned char accepted = 0;
    char discard[256];
    size_t length = name != NULL ? strlen(name) : 0;
    SOCKET anchor;

    if (length == 0 || length > UURB_TERMINAL_MAX_SESSION_NAME)
        return 0;
    anchor = socket(AF_INET, SOCK_STREAM, IPPROTO_TCP);
    if (anchor == INVALID_SOCKET)
        return 0;
    memset(&address, 0, sizeof(address));
    address.sin_family = AF_INET;
    address.sin_port = htons(port);
    address.sin_addr.s_addr = htonl(INADDR_LOOPBACK);
    memset(&hello, 0, sizeof(hello));
    hello.magic = htonl(UURB_TERMINAL_MAGIC);
    hello.version = htons(UURB_TERMINAL_VERSION_SESSION);
    hello.token_length = htons(UURB_TERMINAL_TOKEN_LENGTH);
    session.role = UURB_TERMINAL_ROLE_ANCHOR;
    session.name_length = (uint8_t)length;
    if (connect(anchor, (struct sockaddr *)&address, sizeof(address)) != 0 ||
        send(anchor, (const char *)&hello, sizeof(hello), 0) != sizeof(hello) ||
        send(anchor, token, UURB_TERMINAL_TOKEN_LENGTH, 0) != UURB_TERMINAL_TOKEN_LENGTH ||
        send(anchor, (const char *)&session, sizeof(session), 0) != sizeof(session) ||
        send(anchor, name, (int)length, 0) != (int)length ||
        recv(anchor, (char *)&accepted, 1, 0) != 1 ||
        accepted != UURB_TERMINAL_ACCEPTED) {
        closesocket(anchor);
        trace_event("anchor_unavailable");
        return 0;
    }
    trace_event("anchor_held");
    while (recv(anchor, discard, sizeof(discard), 0) > 0)
        ;
    closesocket(anchor);
    trace_event("anchor_released");
    return 1;
}

int main(int argc, char **argv)
{
    WSADATA winsock;
    struct sockaddr_in address;
    struct uurb_terminal_hello hello;
    char token[UURB_TERMINAL_TOKEN_LENGTH + 1];
    DWORD written;
    HANDLE input_thread = NULL;
    HANDLE resize_thread = NULL;
    HANDLE control_thread = NULL;
    HANDLE output;
    unsigned char buffer[16384];
    uint16_t columns;
    uint16_t rows;
    uint16_t port;
    unsigned char accepted;
    int received;
    int output_seen = 0;
    int exit_code = 1;
    size_t script_length;
    const char *pane_session = NULL;
    const wchar_t *script = mux_script_from_command_line(GetCommandLineW(),
                                                         &script_length);

    if (script != NULL) {
        exit_code = run_mux_script(script, script_length);
        if (exit_code == MUX_SCRIPT_REJECTED) {
            trace_event("mux_script_rejected");
            write_error("UU Ubuntu terminal bridge refused an unsupported PowerShell command");
            return 1;
        }
        return exit_code;
    }
    /* Any other PowerShell command (UU also tries one to start its
     * visible-attach helper) fails like Wine's placeholder, so UU moves on
     * to its cmd candidate instead of this opening an unrelated shell. */
    for (int index = 1; index < argc; index++) {
        if (_stricmp(argv[index], "-Command") == 0) {
            trace_event("unsupported_command");
            return 1;
        }
    }
    parse_bridge_arguments(argc, argv);
    for (int index = 1; index + 1 < argc; index++) {
        if (strcmp(argv[index], MUX_PANE_SESSION_ARGUMENT) == 0)
            pane_session = argv[index + 1];
    }
    trace_event("build_hex_stdio_v2");
    if (proxy_argument_errors) {
        trace_event("argument_validation_failed");
        return 2;
    }
    /* Keep Win32 and CRT-facing standard handles coherent for Wine builds
     * that inspect GetStdHandle() in a child helper before entering this
     * proxy's workers. */
    if (proxy_input != NULL)
        SetStdHandle(STD_INPUT_HANDLE, proxy_input);
    if (proxy_output != NULL)
        SetStdHandle(STD_OUTPUT_HANDLE, proxy_output);
    output = proxy_output != NULL ? proxy_output
                                   : GetStdHandle(STD_OUTPUT_HANDLE);
    /* The control endpoint is the vendor's stable one-byte command stream:
     * type 1 plus little-endian cols/rows, or type 2 for shutdown. */
    trace_event("start");
    trace_handle_status("input_handle", GetStdHandle(STD_INPUT_HANDLE));
    trace_handle_status("output_handle", output);
    if (proxy_control != NULL)
        trace_handle_status("control_handle", proxy_control);
    if (proxy_display_name_present)
        trace_event("display_name_present");
    if (proxy_handle_args_present)
        trace_event("explicit_handles_present");
    if (proxy_mux_args_present)
        trace_event("mux_args_present");
    if (proxy_mux_session[0] != '\0')
        trace_event("mux_session_present");
    if (proxy_mux_attach_present)
        trace_event("mux_attach_present");
    if (proxy_mux_config_present)
        trace_event("mux_config_present");
    if (!load_configuration(token, &port)) {
        trace_event("configuration_failed");
        write_error("UU Ubuntu terminal bridge is not configured");
        return 2;
    }
    trace_event("configuration_loaded");
    if (WSAStartup(MAKEWORD(2, 2), &winsock) != 0) {
        trace_event("winsock_failed");
        write_error("UU Ubuntu terminal bridge could not initialize Winsock");
        return 3;
    }
    if (!proxy_handle_args_present && anchor_session(token, port, pane_session)) {
        WSACleanup();
        return 0;
    }
    configure_console();
    terminal_socket = socket(AF_INET, SOCK_STREAM, IPPROTO_TCP);
    if (terminal_socket == INVALID_SOCKET) {
        trace_event("socket_failed");
        write_error("UU Ubuntu terminal bridge could not create a socket");
        goto done;
    }
    memset(&address, 0, sizeof(address));
    address.sin_family = AF_INET;
    address.sin_port = htons(port);
    address.sin_addr.s_addr = htonl(INADDR_LOOPBACK);
    if (connect(terminal_socket, (struct sockaddr *)&address,
                sizeof(address)) == SOCKET_ERROR) {
        trace_event("connect_failed");
        write_error("UU Ubuntu terminal bridge is not reachable");
        goto done;
    }

    console_size(&columns, &rows);
    trace_dimensions("initial_size", columns, rows);
    memset(&hello, 0, sizeof(hello));
    hello.magic = htonl(UURB_TERMINAL_MAGIC);
    hello.version = htons(UURB_TERMINAL_VERSION);
    hello.token_length = htons(UURB_TERMINAL_TOKEN_LENGTH);
    hello.columns = htons(columns);
    hello.rows = htons(rows);
    if (!send_all(&hello, sizeof(hello)) ||
        !send_all(token, UURB_TERMINAL_TOKEN_LENGTH)) {
        trace_event("hello_failed");
        write_error("UU Ubuntu terminal bridge handshake failed");
        goto done;
    }
    if (!receive_all(&accepted, 1) || accepted != UURB_TERMINAL_ACCEPTED) {
        trace_event("authentication_rejected");
        write_error("UU Ubuntu terminal bridge rejected authentication");
        goto done;
    }
    trace_event("authentication_accepted");

    InitializeCriticalSection(&send_lock);
    stop_event = CreateEventW(NULL, TRUE, FALSE, NULL);
    if (stop_event == NULL) {
        trace_event("stop_event_failed");
        write_error("UU Ubuntu terminal bridge could not create its stop event");
        DeleteCriticalSection(&send_lock);
        goto done;
    }
    input_thread = CreateThread(NULL, 0, input_worker, NULL, 0, NULL);
    /* A passed control pipe is the authoritative resize source. Polling
     * pipe-backed stdout would otherwise keep resetting it to initial args. */
    if (proxy_control == NULL)
        resize_thread = CreateThread(NULL, 0, resize_worker, NULL, 0, NULL);
    if (proxy_control != NULL)
        control_thread = CreateThread(NULL, 0, control_worker, NULL, 0, NULL);
    if (input_thread == NULL ||
        (proxy_control == NULL && resize_thread == NULL) ||
        (proxy_control != NULL && control_thread == NULL)) {
        trace_event("workers_failed");
        write_error("UU Ubuntu terminal bridge could not start I/O workers");
        goto workers_done;
    }
    trace_event("io_workers_started");

    while ((received = recv(terminal_socket, (char *)buffer,
                            sizeof(buffer), 0)) > 0) {
        DWORD offset = 0;
        while (offset < (DWORD)received) {
            if (!WriteFile(output, buffer + offset, (DWORD)received - offset,
                           &written, NULL)) {
                trace_error("output_write_failed", GetLastError());
                goto workers_done;
            }
            if (!written) {
                trace_event("output_write_zero");
                goto workers_done;
            }
            offset += written;
        }
        if (!output_seen) {
            char line[96];
            snprintf(line, sizeof(line), "output_forwarded bytes=%lu", (unsigned long)offset);
            trace_event(line);
            output_seen = 1;
        }
    }
    if (received < 0 && WaitForSingleObject(stop_event, 0) != WAIT_OBJECT_0) {
        trace_error("broker_receive_failed", WSAGetLastError());
        goto workers_done;
    }
    exit_code = 0;
    trace_event("session_closed");

workers_done:
    SetEvent(stop_event);
    shutdown(terminal_socket, SD_BOTH);
    if (input_thread != NULL) {
        CancelSynchronousIo(input_thread);
        WaitForSingleObject(input_thread, 1000);
        CloseHandle(input_thread);
    }
    if (resize_thread != NULL) {
        WaitForSingleObject(resize_thread, 1000);
        CloseHandle(resize_thread);
    }
    if (control_thread != NULL) {
        CancelSynchronousIo(control_thread);
        WaitForSingleObject(control_thread, 1000);
        CloseHandle(control_thread);
    }
    CloseHandle(stop_event);
    DeleteCriticalSection(&send_lock);

done:
    if (terminal_socket != INVALID_SOCKET)
        closesocket(terminal_socket);
    SecureZeroMemory(token, sizeof(token));
    WSACleanup();
    {
        char line[96];
        snprintf(line, sizeof(line), "process_exit code=%d", exit_code);
        trace_event(line);
    }
    return exit_code;
}
