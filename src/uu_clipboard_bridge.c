/* Two-way CLIPBOARD text bridge between UU's private X display and the desktop.
 *
 * The native service runs UU under its own Xvfb, so Wine's clipboard (and
 * with it UU's phone clipboard sync) lives on that display, while desktop
 * applications use Xwayland, whose CLIPBOARD Mutter keeps in step with the
 * Wayland clipboard. Watch CLIPBOARD ownership on both with XFixes; when one
 * side gets a new owner, fetch the formats this bridge understands (UTF-8
 * text, file lists and PNG images) and offer them on the other side. Wine
 * converts file lists to CF_HDROP and PNG to bitmaps for UU.
 *
 * Both displays are found at run time: UU's from GameViewerServer.exe's
 * environment, the desktop's from the Xwayland command line. A supervisor
 * loop reconnects after either X server goes away. Clipboard contents are
 * never logged. */
#define _GNU_SOURCE

#include <X11/Xatom.h>
#include <X11/Xlib.h>
#include <X11/extensions/Xfixes.h>

#include <dirent.h>
#include <errno.h>
#include <poll.h>
#include <signal.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <sys/types.h>
#include <sys/wait.h>
#include <time.h>
#include <unistd.h>

/* One X request must carry a whole format; Xwayland and Xvfb accept 16 MB. */
#define MAX_FORMAT_BYTES (12u << 20)
#define TRANSFER_TIMEOUT_MS 5000
#define RETRY_SECONDS 2

/* Targets copied between displays, by name; atoms differ per display. */
static const char *const copied_targets[] = {
    "UTF8_STRING", "text/uri-list", "x-special/gnome-copied-files", "image/png",
};
#define MAX_FORMATS 4

struct format {
    char name[48];
    unsigned char *data;
    size_t size;
};

struct side {
    const char *label;
    char display_name[64];
    char authority[4096];
    Display *display;
    Window window;
    int xfixes_event;
    /* Formats this side currently offers as CLIPBOARD owner. */
    struct format formats[MAX_FORMATS];
    int format_count;
};

static Atom clipboard, targets_atom, incr_atom,
    timestamp_atom, transfer_atom;
static volatile sig_atomic_t stop_requested;

static void handle_stop(int signal_number)
{
    (void)signal_number;
    stop_requested = 1;
}

static int64_t now_ms(void)
{
    struct timespec now;

    clock_gettime(CLOCK_MONOTONIC, &now);
    return (int64_t)now.tv_sec * 1000 + now.tv_nsec / 1000000;
}

/* Read NUL-separated KEY=value pairs from a process environment. */
static int environment_value(pid_t pid, const char *key, char *value, size_t size)
{
    char path[64], buffer[65536];
    size_t key_length = strlen(key), length;
    FILE *file;
    char *cursor;

    snprintf(path, sizeof(path), "/proc/%d/environ", (int)pid);
    file = fopen(path, "re");
    if (file == NULL)
        return 0;
    length = fread(buffer, 1, sizeof(buffer) - 1, file);
    fclose(file);
    buffer[length] = '\0';
    for (cursor = buffer; cursor < buffer + length; cursor += strlen(cursor) + 1) {
        if (strncmp(cursor, key, key_length) == 0 && cursor[key_length] == '=' &&
            strlen(cursor + key_length + 1) < size) {
            strcpy(value, cursor + key_length + 1);
            return 1;
        }
    }
    return 0;
}

static size_t read_cmdline(pid_t pid, char *buffer, size_t size)
{
    char path[64];
    FILE *file;
    size_t length;

    snprintf(path, sizeof(path), "/proc/%d/cmdline", (int)pid);
    file = fopen(path, "re");
    if (file == NULL)
        return 0;
    length = fread(buffer, 1, size - 1, file);
    fclose(file);
    buffer[length] = '\0';
    return length;
}

/* Find this user's UU server (Wine) and Xwayland and their X credentials. */
static int discover(struct side *uu, struct side *desktop)
{
    DIR *processes = opendir("/proc");
    struct dirent *entry;
    int found_uu = 0, found_desktop = 0;

    if (processes == NULL)
        return 0;
    while ((entry = readdir(processes)) != NULL && !(found_uu && found_desktop)) {
        char cmdline[8192], proc_path[64];
        struct stat info;
        pid_t pid = (pid_t)strtol(entry->d_name, NULL, 10);
        size_t length;

        if (pid <= 0)
            continue;
        snprintf(proc_path, sizeof(proc_path), "/proc/%d", (int)pid);
        if (stat(proc_path, &info) != 0 || info.st_uid != getuid())
            continue;
        length = read_cmdline(pid, cmdline, sizeof(cmdline));
        if (length == 0)
            continue;
        if (!found_uu && strstr(cmdline, "\\bin\\GameViewerServer.exe") != NULL &&
            environment_value(pid, "DISPLAY", uu->display_name, sizeof(uu->display_name)) &&
            environment_value(pid, "XAUTHORITY", uu->authority, sizeof(uu->authority)))
            found_uu = 1;
        if (!found_desktop && strcmp(strrchr(cmdline, '/') ? strrchr(cmdline, '/') + 1 : cmdline,
                                     "Xwayland") == 0) {
            /* argv: Xwayland :N ... -auth PATH ... */
            char *argument = cmdline + strlen(cmdline) + 1;
            char *previous = NULL;

            desktop->display_name[0] = desktop->authority[0] = '\0';
            while (argument < cmdline + length) {
                if (argument[0] == ':' && desktop->display_name[0] == '\0' &&
                    strlen(argument) < sizeof(desktop->display_name))
                    strcpy(desktop->display_name, argument);
                if (previous != NULL && strcmp(previous, "-auth") == 0 &&
                    strlen(argument) < sizeof(desktop->authority))
                    strcpy(desktop->authority, argument);
                previous = argument;
                argument += strlen(argument) + 1;
            }
            found_desktop = desktop->display_name[0] != '\0' && desktop->authority[0] != '\0';
        }
    }
    closedir(processes);
    return found_uu && found_desktop && strcmp(uu->display_name, desktop->display_name) != 0;
}

static int open_side(struct side *side)
{
    int error_base;

    setenv("XAUTHORITY", side->authority, 1);
    side->display = XOpenDisplay(side->display_name);
    if (side->display == NULL)
        return 0;
    if (!XFixesQueryExtension(side->display, &side->xfixes_event, &error_base))
        return 0;
    side->window = XCreateSimpleWindow(side->display, DefaultRootWindow(side->display),
                                       -10, -10, 1, 1, 0, 0, 0);
    XSelectInput(side->display, side->window, PropertyChangeMask);
    clipboard = XInternAtom(side->display, "CLIPBOARD", False);
    XFixesSelectSelectionInput(side->display, side->window, clipboard,
                               XFixesSetSelectionOwnerNotifyMask);
    XFlush(side->display);
    return 1;
}

static void intern_atoms(Display *display)
{
    clipboard = XInternAtom(display, "CLIPBOARD", False);
    targets_atom = XInternAtom(display, "TARGETS", False);
    incr_atom = XInternAtom(display, "INCR", False);
    timestamp_atom = XInternAtom(display, "TIMESTAMP", False);
    transfer_atom = XInternAtom(display, "UURB_CLIPBOARD", False);
}

/* Wait for a specific event on one display, handling nothing else. */
static int wait_event(Display *display, int type, Window window, XEvent *event,
                      int64_t deadline)
{
    while (!stop_requested) {
        struct pollfd descriptor = {.fd = ConnectionNumber(display), .events = POLLIN};
        int64_t remaining;

        while (XPending(display)) {
            XNextEvent(display, event);
            if (event->type == type && event->xany.window == window)
                return 1;
        }
        remaining = deadline - now_ms();
        if (remaining <= 0)
            return 0;
        poll(&descriptor, 1, (int)remaining);
    }
    return 0;
}

/* Bytes Xlib returns for `items` of `format`: 32-bit items arrive as longs. */
static size_t property_bytes(unsigned long items, int format)
{
    return items * (format == 32 ? sizeof(long) : (size_t)(format / 8));
}

/* Fetch CLIPBOARD as `target` from `side` (atoms are per display). */
static unsigned char *fetch_target(struct side *side, Time when, Atom target, size_t *size)
{
    Display *display = side->display;
    int64_t deadline = now_ms() + TRANSFER_TIMEOUT_MS;
    unsigned char *result = NULL;
    XEvent event;
    Atom type;
    int format;
    unsigned long items, remaining;
    unsigned char *data = NULL;

    intern_atoms(display);
    *size = 0;
    XDeleteProperty(display, side->window, transfer_atom);
    XConvertSelection(display, clipboard, target, transfer_atom, side->window, when);
    XFlush(display);
    if (!wait_event(display, SelectionNotify, side->window, &event, deadline) ||
        event.xselection.property == None)
        return NULL;
    if (XGetWindowProperty(display, side->window, transfer_atom, 0, MAX_FORMAT_BYTES / 4 + 1, True,
                           AnyPropertyType, &type, &format, &items, &remaining,
                           &data) != Success)
        return NULL;
    if (type == incr_atom) {
        /* Chunked transfer: each PropertyNewValue carries the next piece. */
        XFree(data);
        XFlush(display);
        for (;;) {
            size_t chunk;

            do {
                if (!wait_event(display, PropertyNotify, side->window, &event, deadline)) {
                    free(result);
                    return NULL;
                }
            } while (event.xproperty.atom != transfer_atom ||
                     event.xproperty.state != PropertyNewValue);
            if (XGetWindowProperty(display, side->window, transfer_atom, 0, MAX_FORMAT_BYTES / 4 + 1,
                                   True, AnyPropertyType, &type, &format, &items,
                                   &remaining, &data) != Success) {
                free(result);
                return NULL;
            }
            XFlush(display);
            chunk = property_bytes(items, format);
            if (chunk == 0) {
                XFree(data);
                return result;
            }
            if (*size + chunk > MAX_FORMAT_BYTES) {
                XFree(data);
                free(result);
                return NULL;
            }
            result = realloc(result, *size + chunk + 1);
            memcpy(result + *size, data, chunk);
            *size += chunk;
            result[*size] = '\0';
            XFree(data);
        }
    }
    if (remaining == 0 && property_bytes(items, format) <= MAX_FORMAT_BYTES) {
        items = property_bytes(items, format);
        result = malloc(items + 1);
        memcpy(result, data, items);
        result[items] = '\0';
        *size = items;
    }
    XFree(data);
    return result;
}

static void clear_formats(struct side *side)
{
    for (int index = 0; index < side->format_count; index++)
        free(side->formats[index].data);
    side->format_count = 0;
}

static void log_event(const char *event, const struct side *side, const struct format *formats,
                      int count)
{
    char stamp[32];
    time_t now = time(NULL);

    strftime(stamp, sizeof(stamp), "%F %T", localtime(&now));
    fprintf(stderr, "%s clipboard %s on %s", stamp, event, side->label);
    for (int index = 0; index < count; index++)
        fprintf(stderr, " %s=%zu", formats[index].name, formats[index].size);
    fprintf(stderr, "\n");
}

static void offer_formats(struct side *side, struct format *formats, int count)
{
    clear_formats(side);
    memcpy(side->formats, formats, sizeof(*formats) * (size_t)count);
    side->format_count = count;
    intern_atoms(side->display);
    XSetSelectionOwner(side->display, clipboard, side->window, CurrentTime);
    XFlush(side->display);
    log_event(XGetSelectionOwner(side->display, clipboard) == side->window ? "offered" : "not-owned",
              side, formats, count);
}

static const struct format *find_format_in(const struct format *formats, int count,
                                           const char *name);

static const struct format *find_format(const struct side *side, const char *name)
{
    return find_format_in(side->formats, side->format_count, name);
}

static void answer_request(struct side *side, XSelectionRequestEvent *request)
{
    Display *display = side->display;
    XSelectionEvent reply;
    Atom property = request->property != None ? request->property : request->target;
    char *name;

    intern_atoms(display);
    memset(&reply, 0, sizeof(reply));
    reply.type = SelectionNotify;
    reply.requestor = request->requestor;
    reply.selection = request->selection;
    reply.target = request->target;
    reply.time = request->time;
    reply.property = None;
    if (request->selection == clipboard && side->format_count > 0) {
        if (request->target == targets_atom) {
            Atom offered[MAX_FORMATS + 3];
            int count = 0;

            offered[count++] = targets_atom;
            offered[count++] = timestamp_atom;
            for (int index = 0; index < side->format_count; index++)
                offered[count++] = XInternAtom(display, side->formats[index].name, False);
            if (find_format(side, "UTF8_STRING"))
                offered[count++] = XInternAtom(display, "text/plain;charset=utf-8", False);
            XChangeProperty(display, request->requestor, property, XA_ATOM, 32,
                            PropModeReplace, (unsigned char *)offered, count);
            reply.property = property;
        } else if ((name = XGetAtomName(display, request->target)) != NULL) {
            const struct format *format = find_format(
                side, strcmp(name, "text/plain;charset=utf-8") == 0 ? "UTF8_STRING" : name);

            if (format != NULL) {
                XChangeProperty(display, request->requestor, property, request->target, 8,
                                PropModeReplace, format->data, (int)format->size);
                reply.property = property;
            }
            XFree(name);
        }
    }
    XSendEvent(display, request->requestor, False, NoEventMask, (XEvent *)&reply);
    XFlush(display);
}

/* List CLIPBOARD's targets on `side` by name. */
static int fetch_target_names(struct side *side, Time when, char names[][48], int max)
{
    size_t size;
    unsigned char *atoms;
    int count = 0;

    intern_atoms(side->display);
    atoms = fetch_target(side, when, targets_atom, &size);
    if (atoms == NULL)
        return 0;
    for (size_t index = 0; index < size / sizeof(long) && count < max; index++) {
        char *name = XGetAtomName(side->display, (Atom)((unsigned long *)(void *)atoms)[index]);

        if (name != NULL && strlen(name) < 48)
            strcpy(names[count++], name);
        if (name != NULL)
            XFree(name);
    }
    free(atoms);
    return count;
}

/* "copy\nURI\nURI" from a text/uri-list ("URI\r\n", '#' comments). */
static struct format copied_files_from_uris(const struct format *uris)
{
    struct format result = {.name = "x-special/gnome-copied-files"};
    const unsigned char *line = uris->data, *end = uris->data + uris->size;

    result.data = malloc(uris->size + 5);
    memcpy(result.data, "copy", 4);
    result.size = 4;
    while (line < end) {
        const unsigned char *next = memchr(line, '\n', (size_t)(end - line));
        const unsigned char *stop = next != NULL ? next : end;

        if (stop > line && stop[-1] == '\r')
            stop--;
        if (stop > line && line[0] != '#') {
            result.data[result.size++] = '\n';
            memcpy(result.data + result.size, line, (size_t)(stop - line));
            result.size += (size_t)(stop - line);
        }
        line = next != NULL ? next + 1 : end;
    }
    return result;
}

static const struct format *find_format_in(const struct format *formats, int count,
                                           const char *name)
{
    for (int index = 0; index < count; index++)
        if (strcmp(formats[index].name, name) == 0)
            return &formats[index];
    return NULL;
}

/* Copy every understood format of `side`'s new clipboard to `other`. */
static void copy_clipboard(struct side *side, struct side *other, Time when)
{
    char names[256][48];
    struct format formats[MAX_FORMATS];
    int available = fetch_target_names(side, when, names, 256);
    int count = 0;

    for (size_t wanted = 0; wanted < sizeof(copied_targets) / sizeof(copied_targets[0]); wanted++) {
        const char *target = copied_targets[wanted];
        int present = 0;

        for (int index = 0; index < available; index++)
            present |= strcmp(names[index], target) == 0;
        /* Some owners answer no TARGETS; text is still worth asking for. */
        if (!present && !(available == 0 && wanted == 0))
            continue;
        intern_atoms(side->display);
        formats[count].data = fetch_target(side, when, XInternAtom(side->display, target, False),
                                           &formats[count].size);
        if (formats[count].data == NULL)
            continue;
        strcpy(formats[count].name, target);
        count++;
    }
    /* Nautilus pastes files only from its own list; derive it from URIs. */
    if (count < MAX_FORMATS && find_format_in(formats, count, "text/uri-list") &&
        !find_format_in(formats, count, "x-special/gnome-copied-files")) {
        formats[count] = copied_files_from_uris(find_format_in(formats, count, "text/uri-list"));
        count++;
    }
    if (count == 0) {
        log_event("unsupported", side, NULL, 0);
        return;
    }
    offer_formats(other, formats, count);
}

/* Handle one event from `side`; `other` receives what it copies. */
static void handle_event(struct side *side, struct side *other, XEvent *event)
{
    if (event->type == side->xfixes_event + XFixesSelectionNotify) {
        XFixesSelectionNotifyEvent *notify = (XFixesSelectionNotifyEvent *)event;

        if (notify->owner == None || notify->owner == side->window)
            return;
        copy_clipboard(side, other, notify->selection_timestamp);
    } else if (event->type == SelectionRequest) {
        answer_request(side, &event->xselectionrequest);
    } else if (event->type == SelectionClear) {
        clear_formats(side);
    }
}

static int run_bridge(void)
{
    struct side uu = {.label = "uu"}, desktop = {.label = "desktop"};
    struct pollfd descriptors[2];

    if (!discover(&uu, &desktop))
        return 3;
    if (!open_side(&uu) || !open_side(&desktop)) {
        fprintf(stderr, "clipboard bridge could not open uu=%s desktop=%s\n",
                uu.display_name, desktop.display_name);
        return 4;
    }
    fprintf(stderr, "clipboard bridge connected uu=%s desktop=%s\n",
            uu.display_name, desktop.display_name);
    descriptors[0] = (struct pollfd){.fd = ConnectionNumber(uu.display), .events = POLLIN};
    descriptors[1] = (struct pollfd){.fd = ConnectionNumber(desktop.display), .events = POLLIN};
    while (!stop_requested) {
        XEvent event;

        while (XPending(uu.display)) {
            XNextEvent(uu.display, &event);
            handle_event(&uu, &desktop, &event);
        }
        while (XPending(desktop.display)) {
            XNextEvent(desktop.display, &event);
            handle_event(&desktop, &uu, &event);
        }
        if (poll(descriptors, 2, 1000) < 0 && errno != EINTR)
            return 5;
        if ((descriptors[0].revents | descriptors[1].revents) & (POLLERR | POLLHUP | POLLNVAL))
            return 6;
    }
    return 0;
}

/* Xlib exits the process on a lost connection; the supervisor reconnects. */
int main(void)
{
    struct sigaction action;
    pid_t worker = 0;

    memset(&action, 0, sizeof(action));
    action.sa_handler = handle_stop;
    sigaction(SIGTERM, &action, NULL);
    sigaction(SIGINT, &action, NULL);
    while (!stop_requested) {
        int status;

        worker = fork();
        if (worker == 0) {
            signal(SIGTERM, SIG_DFL);
            _exit(run_bridge());
        }
        while (worker > 0 && waitpid(worker, &status, 0) < 0) {
            if (errno == EINTR && stop_requested)
                kill(worker, SIGTERM);
            else if (errno != EINTR)
                break;
        }
        if (!stop_requested)
            sleep(RETRY_SECONDS);
    }
    return 0;
}
