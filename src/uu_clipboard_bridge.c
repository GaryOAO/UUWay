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
 * While a transfer waits on one display, both displays stay served: requests
 * for what this bridge offers are answered and owner changes are queued, never
 * dropped. The newest change wins; one that a later change on the other side
 * overtook is not copied back over it, and content a side already holds is
 * not offered to it again, so the two clipboards cannot echo.
 *
 * Wine 11 takes CLIPBOARD for a Windows copy (a phone copy UU writes) with an
 * X request its clipboard thread leaves unflushed: winex11 does not flush
 * after acquire_selection, and win32u runs the driver only when its
 * connection has input. The request then waits for the next X event, which
 * held phone copies back until a desktop copy woke Wine and let the stale one
 * overwrite it. Every Wine thread watches root properties, so this bridge
 * touches one on UU's display a few times a second.
 *
 * Both displays are found at run time: UU's from GameViewerServer.exe's
 * environment, the desktop's from the Xwayland command line. A supervisor
 * loop reconnects after either X server goes away. Clipboard contents are
 * never logged, only format sizes and a short digest. */
#define _GNU_SOURCE

#include <X11/Xatom.h>
#include <X11/Xlib.h>
#include <X11/extensions/Xfixes.h>

#include <dirent.h>
#include <errno.h>
#include <inttypes.h>
#include <poll.h>
#include <signal.h>
#include <stdarg.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <sys/time.h>
#include <sys/types.h>
#include <sys/wait.h>
#include <time.h>
#include <unistd.h>

/* One X request must carry a whole format; Xwayland and Xvfb accept 16 MB. */
#define MAX_FORMAT_BYTES (12u << 20)
#define TRANSFER_TIMEOUT_MS 5000
#define RETRY_SECONDS 2
#define WINE_WAKE_MS 250

/* Targets copied between displays, by name; atoms differ per display. */
static const char *const copied_targets[] = {
    "UTF8_STRING", "text/uri-list", "x-special/gnome-copied-files", "image/png",
};
#define MAX_FORMATS 4

/* The native text service owns the desktop clipboard for a moment to paste
 * phone text and marks that selection with this target. It is not a copy. */
#define TRANSIENT_TARGET "application/x-uurb-transient"

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
    struct side *other;
    /* Atoms are per X server. */
    Atom clipboard, targets, incr, timestamp, transfer, wake;
    /* Formats this side currently offers as CLIPBOARD owner. */
    struct format formats[MAX_FORMATS];
    int format_count;
    Time owned_since;
    int64_t offered_ms;
    /* Digest of the content this side is known to hold; 0 when unknown. */
    uint64_t holds;
    /* The newest owner change not yet copied; a later one replaces it. */
    int pending;
    Window pending_owner;
    Time pending_time;
    int64_t pending_ms;
};

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

static void log_line(const char *format, ...)
{
    struct timeval now;
    char stamp[32];
    va_list arguments;

    gettimeofday(&now, NULL);
    strftime(stamp, sizeof(stamp), "%F %T", localtime(&now.tv_sec));
    fprintf(stderr, "%s.%03ld clipboard ", stamp, (long)(now.tv_usec / 1000));
    va_start(arguments, format);
    vfprintf(stderr, format, arguments);
    va_end(arguments);
    fputc('\n', stderr);
    fflush(stderr);
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

    if (side->authority[0] != '\0')
        setenv("XAUTHORITY", side->authority, 1);
    side->display = XOpenDisplay(side->display_name);
    if (side->display == NULL)
        return 0;
    if (!XFixesQueryExtension(side->display, &side->xfixes_event, &error_base))
        return 0;
    side->clipboard = XInternAtom(side->display, "CLIPBOARD", False);
    side->targets = XInternAtom(side->display, "TARGETS", False);
    side->incr = XInternAtom(side->display, "INCR", False);
    side->timestamp = XInternAtom(side->display, "TIMESTAMP", False);
    side->transfer = XInternAtom(side->display, "UURB_CLIPBOARD", False);
    side->wake = XInternAtom(side->display, "UURB_CLIPBOARD_WAKE", False);
    side->window = XCreateSimpleWindow(side->display, DefaultRootWindow(side->display),
                                       -10, -10, 1, 1, 0, 0, 0);
    XSelectInput(side->display, side->window, PropertyChangeMask);
    XFixesSelectSelectionInput(side->display, side->window, side->clipboard,
                               XFixesSetSelectionOwnerNotifyMask);
    XFlush(side->display);
    return 1;
}

static void clear_formats(struct side *side)
{
    for (int index = 0; index < side->format_count; index++)
        free(side->formats[index].data);
    side->format_count = 0;
}

static const struct format *find_format_in(const struct format *formats, int count,
                                           const char *name)
{
    for (int index = 0; index < count; index++)
        if (strcmp(formats[index].name, name) == 0)
            return &formats[index];
    return NULL;
}

static void answer_request(struct side *side, XSelectionRequestEvent *request)
{
    Display *display = side->display;
    XSelectionEvent reply;
    Atom property = request->property != None ? request->property : request->target;
    char *name;

    memset(&reply, 0, sizeof(reply));
    reply.type = SelectionNotify;
    reply.requestor = request->requestor;
    reply.selection = request->selection;
    reply.target = request->target;
    reply.time = request->time;
    reply.property = None;
    if (request->selection == side->clipboard && side->format_count > 0) {
        if (request->target == side->targets) {
            Atom offered[MAX_FORMATS + 3];
            int count = 0;

            offered[count++] = side->targets;
            offered[count++] = side->timestamp;
            for (int index = 0; index < side->format_count; index++)
                offered[count++] = XInternAtom(display, side->formats[index].name, False);
            if (find_format_in(side->formats, side->format_count, "UTF8_STRING"))
                offered[count++] = XInternAtom(display, "text/plain;charset=utf-8", False);
            XChangeProperty(display, request->requestor, property, XA_ATOM, 32,
                            PropModeReplace, (unsigned char *)offered, count);
            reply.property = property;
        } else if (request->target == side->timestamp) {
            long owned_since = (long)side->owned_since;

            XChangeProperty(display, request->requestor, property, XA_INTEGER, 32,
                            PropModeReplace, (unsigned char *)&owned_since, 1);
            reply.property = property;
        } else if ((name = XGetAtomName(display, request->target)) != NULL) {
            const struct format *format = find_format_in(
                side->formats, side->format_count,
                strcmp(name, "text/plain;charset=utf-8") == 0 ? "UTF8_STRING" : name);

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

/* Serve one event from `side` that no transfer is waiting for: answer
 * requests, and remember the newest foreign owner for the main loop. */
static void service_event(struct side *side, XEvent *event)
{
    if (event->type == side->xfixes_event + XFixesSelectionNotify) {
        XFixesSelectionNotifyEvent *notify = (XFixesSelectionNotifyEvent *)event;

        if (notify->owner == side->window) {
            side->owned_since = notify->selection_timestamp;
        } else if (notify->owner != None) {
            side->pending = 1;
            side->pending_owner = notify->owner;
            side->pending_time = notify->selection_timestamp;
            side->pending_ms = now_ms();
        }
    } else if (event->type == SelectionRequest) {
        answer_request(side, &event->xselectionrequest);
    } else if (event->type == SelectionClear) {
        clear_formats(side);
    }
}

/* Serve both displays until `side`'s window gets an event of `type`. */
static int wait_event(struct side *side, int type, XEvent *event, int64_t deadline)
{
    struct side *sides[2] = {side, side->other};

    while (!stop_requested) {
        struct pollfd descriptors[2];
        int64_t remaining;

        for (int index = 0; index < 2; index++) {
            while (XPending(sides[index]->display)) {
                XNextEvent(sides[index]->display, event);
                if (index == 0 && event->type == type && event->xany.window == side->window)
                    return 1;
                service_event(sides[index], event);
            }
        }
        remaining = deadline - now_ms();
        if (remaining <= 0)
            return 0;
        for (int index = 0; index < 2; index++)
            descriptors[index] = (struct pollfd){.fd = ConnectionNumber(sides[index]->display),
                                                 .events = POLLIN};
        poll(descriptors, 2, (int)remaining);
    }
    return 0;
}

/* Bytes Xlib returns for `items` of `format`: 32-bit items arrive as longs. */
static size_t property_bytes(unsigned long items, int format)
{
    return items * (format == 32 ? sizeof(long) : (size_t)(format / 8));
}

/* Fetch CLIPBOARD as `target` from `side`. */
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

    *size = 0;
    XDeleteProperty(display, side->window, side->transfer);
    XConvertSelection(display, side->clipboard, target, side->transfer, side->window, when);
    XFlush(display);
    /* A reply to an earlier request that timed out may still arrive. */
    do {
        if (!wait_event(side, SelectionNotify, &event, deadline))
            return NULL;
    } while (event.xselection.selection != side->clipboard || event.xselection.target != target);
    if (event.xselection.property == None)
        return NULL;
    if (XGetWindowProperty(display, side->window, side->transfer, 0, MAX_FORMAT_BYTES / 4 + 1,
                           True, AnyPropertyType, &type, &format, &items, &remaining,
                           &data) != Success)
        return NULL;
    if (type == side->incr) {
        /* Chunked transfer: each PropertyNewValue carries the next piece. */
        XFree(data);
        XFlush(display);
        for (;;) {
            size_t chunk;

            do {
                if (!wait_event(side, PropertyNotify, &event, deadline)) {
                    free(result);
                    return NULL;
                }
            } while (event.xproperty.atom != side->transfer ||
                     event.xproperty.state != PropertyNewValue);
            if (XGetWindowProperty(display, side->window, side->transfer, 0,
                                   MAX_FORMAT_BYTES / 4 + 1, True, AnyPropertyType, &type,
                                   &format, &items, &remaining, &data) != Success) {
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

/* List CLIPBOARD's targets on `side` by name. */
static int fetch_target_names(struct side *side, Time when, char names[][48], int max)
{
    size_t size;
    unsigned char *atoms = fetch_target(side, when, side->targets, &size);
    int count = 0;

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

/* FNV-1a over names and bytes; never 0, which means "unknown". */
static uint64_t formats_digest(const struct format *formats, int count)
{
    uint64_t hash = 0xcbf29ce484222325ull;

    for (int index = 0; index < count; index++) {
        const unsigned char *parts[2] = {(const unsigned char *)formats[index].name,
                                         formats[index].data};
        size_t sizes[2] = {strlen(formats[index].name) + 1, formats[index].size};

        for (int part = 0; part < 2; part++)
            for (size_t byte = 0; byte < sizes[part]; byte++)
                hash = (hash ^ parts[part][byte]) * 0x100000001b3ull;
    }
    return hash != 0 ? hash : 1;
}

static void describe_formats(const struct format *formats, int count, char *text, size_t size)
{
    size_t used = 0;

    text[0] = '\0';
    for (int index = 0; index < count && used < size; index++)
        used += (size_t)snprintf(text + used, size - used, " %s=%zu", formats[index].name,
                                 formats[index].size);
}

static void free_formats(struct format *formats, int count)
{
    for (int index = 0; index < count; index++)
        free(formats[index].data);
}

static void offer_formats(struct side *side, struct format *formats, int count, uint64_t digest)
{
    clear_formats(side);
    memcpy(side->formats, formats, sizeof(*formats) * (size_t)count);
    side->format_count = count;
    side->holds = digest;
    side->offered_ms = now_ms();
    XSetSelectionOwner(side->display, side->clipboard, side->window, CurrentTime);
    XFlush(side->display);
}

/* Copy the newest owner change of `source` to the other side. */
static void copy_change(struct side *source)
{
    struct side *target = source->other;
    Time when = source->pending_time;
    Window owner = source->pending_owner;
    int64_t changed_ms = source->pending_ms;
    char names[256][48], sizes[256];
    struct format formats[MAX_FORMATS];
    int available, count = 0;
    uint64_t digest;

    source->pending = 0;
    /* This change is newer than any queued on the other side. */
    if (target->pending && target->pending_ms <= changed_ms)
        target->pending = 0;
    available = fetch_target_names(source, when, names, 256);
    for (int index = 0; index < available; index++) {
        if (strcmp(names[index], TRANSIENT_TARGET) == 0) {
            log_line("transient on %s owner=0x%lx", source->label, owner);
            return;
        }
    }
    for (size_t wanted = 0; wanted < sizeof(copied_targets) / sizeof(copied_targets[0]); wanted++) {
        const char *name = copied_targets[wanted];
        int present = 0;

        for (int index = 0; index < available; index++)
            present |= strcmp(names[index], name) == 0;
        /* Some owners answer no TARGETS; text is still worth asking for. */
        if (!present && !(available == 0 && wanted == 0))
            continue;
        formats[count].data = fetch_target(source, when, XInternAtom(source->display, name, False),
                                           &formats[count].size);
        if (formats[count].data == NULL)
            continue;
        strcpy(formats[count].name, name);
        count++;
    }
    /* Nautilus pastes files only from its own list; derive it from URIs. */
    if (count < MAX_FORMATS && find_format_in(formats, count, "text/uri-list") &&
        !find_format_in(formats, count, "x-special/gnome-copied-files")) {
        formats[count] = copied_files_from_uris(find_format_in(formats, count, "text/uri-list"));
        count++;
    }
    if (count == 0) {
        source->holds = 0;
        log_line("unsupported on %s owner=0x%lx targets=%d", source->label, owner, available);
        return;
    }
    digest = formats_digest(formats, count);
    source->holds = digest;
    describe_formats(formats, count, sizes, sizeof(sizes));
    if (source->pending || (target->pending && target->pending_ms > changed_ms)) {
        /* Either side changed while this was fetched; that change wins. */
        free_formats(formats, count);
        log_line("superseded %s->%s owner=0x%lx digest=%08" PRIx32 "%s", source->label,
                 target->label, owner, (uint32_t)digest, sizes);
        return;
    }
    if (!target->pending && target->holds == digest) {
        free_formats(formats, count);
        log_line("unchanged %s->%s owner=0x%lx digest=%08" PRIx32 "%s", source->label,
                 target->label, owner, (uint32_t)digest, sizes);
        return;
    }
    target->pending = 0;
    offer_formats(target, formats, count, digest);
    /* since-offer: how soon the source changed after this bridge offered
     * there; a few ms points at a reaction rather than a user copy. */
    log_line("copied %s->%s owner=0x%lx digest=%08" PRIx32 "%s after=%" PRId64
             "ms since-offer=%" PRId64 "ms",
             source->label, target->label, owner, (uint32_t)digest, sizes, now_ms() - changed_ms,
             source->offered_ms ? changed_ms - source->offered_ms : -1);
}

/* Wake every Wine thread on UU's display so pending X requests go out. */
static void wake_wine(struct side *uu)
{
    long nothing = 0;

    XChangeProperty(uu->display, DefaultRootWindow(uu->display), uu->wake, XA_CARDINAL, 32,
                    PropModeAppend, (unsigned char *)&nothing, 0);
    XFlush(uu->display);
}

static int run_bridge(const char *uu_display, const char *desktop_display)
{
    struct side uu = {.label = "uu"}, desktop = {.label = "desktop"};
    int64_t next_wake = 0;

    if (uu_display != NULL) {
        snprintf(uu.display_name, sizeof(uu.display_name), "%s", uu_display);
        snprintf(desktop.display_name, sizeof(desktop.display_name), "%s", desktop_display);
    } else if (!discover(&uu, &desktop)) {
        return 3;
    }
    uu.other = &desktop;
    desktop.other = &uu;
    if (!open_side(&uu) || !open_side(&desktop)) {
        log_line("bridge could not open uu=%s desktop=%s", uu.display_name, desktop.display_name);
        return 4;
    }
    log_line("bridge connected uu=%s desktop=%s", uu.display_name, desktop.display_name);
    while (!stop_requested) {
        struct pollfd descriptors[2] = {
            {.fd = ConnectionNumber(uu.display), .events = POLLIN},
            {.fd = ConnectionNumber(desktop.display), .events = POLLIN},
        };
        struct side *newest;
        XEvent event;
        int64_t now = now_ms();

        if (now >= next_wake) {
            wake_wine(&uu);
            next_wake = now + WINE_WAKE_MS;
        }
        while (XPending(uu.display)) {
            XNextEvent(uu.display, &event);
            service_event(&uu, &event);
        }
        while (XPending(desktop.display)) {
            XNextEvent(desktop.display, &event);
            service_event(&desktop, &event);
        }
        newest = uu.pending && (!desktop.pending || uu.pending_ms >= desktop.pending_ms) ? &uu
                 : desktop.pending ? &desktop : NULL;
        if (newest != NULL) {
            copy_change(newest);
            continue;
        }
        /* Round trips may have queued events that poll cannot see. */
        if (XEventsQueued(uu.display, QueuedAlready) || XEventsQueued(desktop.display, QueuedAlready))
            continue;
        now = now_ms();
        if (poll(descriptors, 2, next_wake > now ? (int)(next_wake - now) : 0) < 0 && errno != EINTR)
            return 5;
        if ((descriptors[0].revents | descriptors[1].revents) & (POLLERR | POLLHUP | POLLNVAL))
            return 6;
    }
    return 0;
}

/* Xlib exits the process on a lost connection; the supervisor reconnects.
 * Tests may name both displays: uu-clipboard-bridge UU_DISPLAY DESKTOP_DISPLAY. */
int main(int argc, char **argv)
{
    struct sigaction action;
    pid_t worker = 0;

    if (argc != 1 && argc != 3)
        return 2;
    memset(&action, 0, sizeof(action));
    action.sa_handler = handle_stop;
    sigaction(SIGTERM, &action, NULL);
    sigaction(SIGINT, &action, NULL);
    while (!stop_requested) {
        int status;

        worker = fork();
        if (worker == 0) {
            signal(SIGTERM, SIG_DFL);
            _exit(run_bridge(argc == 3 ? argv[1] : NULL, argc == 3 ? argv[2] : NULL));
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
