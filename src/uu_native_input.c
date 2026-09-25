#define _GNU_SOURCE
/* Explicitly authorized native Wayland input. Root is needed only to open
 * /dev/uinput; all configuration, authentication and event handling run after
 * irreversible privilege drop. No desktop capture, account data or RDP. */
#include "uu_native_input_translate.h"
#include "native_text_client.h"
#include "native_input_settings.h"
#include <arpa/inet.h>
#include <errno.h>
#include <fcntl.h>
#include <grp.h>
#include <linux/uinput.h>
#include <limits.h>
#include <poll.h>
#include <pwd.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <sys/prctl.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <time.h>
#include <unistd.h>
#ifdef UURB_INPUT_EIS
#include "native_eis_input.h"
static struct uurb_eis_input *eis_input;
static int eis_lease_fd = -1, eis_failed;
static const char *eis_stage = "identity";
#endif

_Static_assert(sizeof(uurb_x11_handshake) == 72, "handshake ABI");
_Static_assert(sizeof(uurb_x11_input_event) == 24, "event ABI");
_Static_assert(sizeof(uurb_x11_request) == 16, "request ABI");
_Static_assert(sizeof(uurb_x11_response) == 16, "response ABI");
#if __BYTE_ORDER__ != __ORDER_LITTLE_ENDIAN__
#error Wire format requires little-endian host
#endif
static volatile sig_atomic_t stopping;
static int keyboard_fd = -1, mouse_fd = -1;
static unsigned char keys[KEY_CNT], buttons[KEY_CNT];
static uint64_t lifetime;
static const char *text_socket;
static unsigned text_reports;
static const char *settings_path;
static struct uurb_input_settings input_settings = {100,100,0};
static unsigned settings_generation;
static uint64_t now_ms(void)
{
    struct timespec value;
    if (clock_gettime(CLOCK_MONOTONIC, &value)) return UINT64_MAX;
    return (uint64_t)value.tv_sec * 1000 + value.tv_nsec / 1000000;
}
static void stop(int sig) { (void)sig; stopping = 1; }
#ifdef UURB_INPUT_EIS
#include "uu_native_eis_control.h"
static int eis_alive(void)
{
    struct pollfd lease = {.fd = eis_lease_fd, .events = POLLIN};
    int result = poll(&lease, 1, 0);
    if (eis_failed || result > 0 || (result < 0 && errno != EINTR) ||
        eis_dispatch_control() < 0) {
        eis_failed = 1; stopping = 1; return 0;
    }
    return 1;
}
static int inherited_socket(const char *name, int type)
{
    const char *value = getenv(name);
    if (!value || !*value || *value < '0' || *value > '9') return -1;
    char *end;
    errno = 0;
    long fd = strtol(value, &end, 10);
    if (errno || *end || fd < 3 || fd > INT_MAX) return -1;
    struct sockaddr_storage address;
    socklen_t length = sizeof(address);
    int actual_type;
    socklen_t type_length = sizeof(actual_type);
    struct ucred peer;
    socklen_t peer_length = sizeof(peer);
    if (getpeername(fd, (struct sockaddr *)&address, &length) || address.ss_family != AF_UNIX ||
        getsockopt(fd, SOL_SOCKET, SO_TYPE, &actual_type, &type_length) || actual_type != type ||
        getsockopt(fd, SOL_SOCKET, SO_PEERCRED, &peer, &peer_length) || peer.uid != getuid() ||
        fcntl(fd, F_SETFD, FD_CLOEXEC)) return -1;
    return (int)fd;
}
#endif
static void reload_settings(void)
{
    static uint64_t check_at;
    static struct stat previous;
    static int known;
    if (!settings_path || now_ms() < check_at) return;
    check_at = now_ms() + 500;
    struct stat current = {0};
    if (lstat(settings_path, &current)) { if (errno != ENOENT) return; }
    if (known && current.st_dev == previous.st_dev && current.st_ino == previous.st_ino &&
        current.st_size == previous.st_size && current.st_mtim.tv_sec == previous.st_mtim.tv_sec &&
        current.st_mtim.tv_nsec == previous.st_mtim.tv_nsec && current.st_mode == previous.st_mode) return;
    previous = current; known = 1;
    struct uurb_input_settings next = {100,100,0};
    int result = uurb_input_settings_read(settings_path, &next);
    if (result < 0) { puts("UURB_NATIVE_INPUT {\"event\":\"settings_rejected\"}"); fflush(stdout); return; }
    input_settings = next; ++settings_generation;
    printf("UURB_NATIVE_INPUT {\"event\":\"settings_applied\",\"relative_percent\":%d,\"wheel_percent\":%d,\"invert_wheel\":%s}\n",
        next.relative_percent, next.wheel_percent, next.invert_wheel ? "true" : "false"); fflush(stdout);
}
static int wait_io(int fd, short flags, uint64_t deadline)
{
    while (!stopping && now_ms() < deadline && now_ms() < lifetime) {
        struct pollfd p[] = {{.fd = fd, .events = flags}
#ifdef UURB_INPUT_EIS
            , {.fd = uurb_eis_fd(eis_input), .events = POLLIN},
            {.fd = eis_lease_fd, .events = POLLIN},
            {.fd = eis_control_fd, .events = POLLIN}
#endif
        };
#ifdef UURB_INPUT_EIS
        if (!eis_alive()) return 0;
#endif
        int n = poll(p, sizeof(p) / sizeof(p[0]), 100);
#ifdef UURB_INPUT_EIS
        if (!eis_alive()) return 0;
#endif
        if (n > 0 && p[0].revents) return (p[0].revents & flags) != 0;
        if (n < 0 && errno != EINTR) return 0;
    }
    return 0;
}
static int transfer(int fd, void *bytes, size_t size, int send_data, unsigned timeout)
{
    uint64_t deadline = now_ms() + timeout;
    unsigned char *p = bytes;
    while (size) {
        if (!wait_io(fd, send_data ? POLLOUT : POLLIN, deadline)) return 0;
        ssize_t n = send_data ? send(fd, p, size, MSG_NOSIGNAL) : recv(fd, p, size, 0);
        if (n < 0 && (errno == EINTR || errno == EAGAIN)) continue;
        if (n <= 0) return 0;
        size -= (size_t)n; p += n;
    }
    return 1;
}
static int response(int fd, unsigned sequence, unsigned count, unsigned error)
{
    uurb_x11_response value = {UURB_X11_INPUT_MAGIC, sequence, count, error};
    return transfer(fd, &value, sizeof(value), 1, 1000);
}
static int emit_events(int fd, const struct input_event *events, size_t count)
{
#ifdef UURB_INPUT_EIS
    uint32_t transition = eis_transition;
    if (!eis_alive() || !eis_core_ready || transition != eis_transition || count > 20) return 0;
    struct uurb_native_events input = {.keyboard = fd == keyboard_fd, .count = count};
    memcpy(input.events, events, count * sizeof(*events));
    return uurb_eis_send(eis_input, &input);
#else
    /* input records are indivisible. Any ambiguous short write terminates the
     * connection and releases its held keys; never replay the batch. */
    if (!wait_io(fd, POLLOUT, now_ms() + 250)) return 0;
    ssize_t size = (ssize_t)(count * sizeof(*events));
    return write(fd, events, (size_t)size) == size;
#endif
}
static int release_device(int fd, unsigned char *held)
{
#ifdef UURB_INPUT_EIS
    int pending = 0;
    for (unsigned key = 0; key < KEY_CNT; ++key) pending |= held[key];
    int ok = !pending || (eis_alive() && uurb_eis_release(eis_input, fd == keyboard_fd));
    memset(held, 0, KEY_CNT);
    return ok;
#else
    struct input_event release[KEY_CNT + 1];
    size_t count = 0;
    for (unsigned key = 0; key < KEY_CNT; ++key) if (held[key])
        release[count++] = (struct input_event){.type = EV_KEY, .code = key, .value = 0};
    if (!count) return 1;
    release[count++] = (struct input_event){.type = EV_SYN, .code = SYN_REPORT};
    /* Cleanup must work after stopping/deadline too; nonblocking single write.
     * Destroying the owned device at exit is the final release boundary. */
    ssize_t bytes = (ssize_t)(count * sizeof(*release));
    int ok = write(fd, release, (size_t)bytes) == bytes;
    memset(held, 0, KEY_CNT);
    return ok;
#endif
}
#ifndef UURB_INPUT_EIS
static int configure_device(int fd, int keyboard)
{
#define SET(request, value) do { if (ioctl(fd, request, value) < 0) return 0; } while (0)
    SET(UI_SET_EVBIT, EV_KEY);
    if (keyboard) {
        for (unsigned key = 1; key < 256; ++key) SET(UI_SET_KEYBIT, key);
        SET(UI_SET_EVBIT, EV_REP);
    } else {
        for (unsigned key = BTN_LEFT; key <= BTN_EXTRA; ++key) SET(UI_SET_KEYBIT, key);
        SET(UI_SET_EVBIT, EV_REL);
        SET(UI_SET_RELBIT, REL_X); SET(UI_SET_RELBIT, REL_Y);
        SET(UI_SET_RELBIT, REL_WHEEL); SET(UI_SET_RELBIT, REL_HWHEEL);
        SET(UI_SET_RELBIT, REL_WHEEL_HI_RES); SET(UI_SET_RELBIT, REL_HWHEEL_HI_RES);
        SET(UI_SET_EVBIT, EV_ABS);
        SET(UI_SET_PROPBIT, INPUT_PROP_POINTER);
        for (unsigned axis = ABS_X; axis <= ABS_Y; ++axis) {
            struct uinput_abs_setup abs = {.code = axis,
                .absinfo = {.minimum = 0, .maximum = 65535}};
            SET(UI_ABS_SETUP, &abs);
        }
    }
    struct uinput_setup setup = {.id = {.bustype = BUS_VIRTUAL}};
    snprintf(setup.name, sizeof(setup.name), "UURB Native %s", keyboard ? "Keyboard" : "Pointer");
    SET(UI_DEV_SETUP, &setup); SET(UI_DEV_CREATE, 0);
#undef SET
    return 1;
}
#endif
static int load_token(const char *path, char token[64])
{
    int fd = open(path, O_RDONLY | O_NOFOLLOW | O_CLOEXEC | O_NONBLOCK);
    if (fd < 0) return 0;
    struct stat info;
    int ok = !fstat(fd, &info) && S_ISREG(info.st_mode) && info.st_uid == getuid() &&
        !(info.st_mode & 077) && info.st_size == 64 && read(fd, token, 64) == 64;
    close(fd);
    for (unsigned i = 0; ok && i < 64; ++i)
        if (!((token[i] >= '0' && token[i] <= '9') || (token[i] >= 'a' && token[i] <= 'f'))) ok = 0;
    return ok;
}
static int serve_client(int fd, const char token[64])
{
    uurb_x11_handshake hello;
    if (!transfer(fd, &hello, sizeof(hello), 0, 3000)) return 1;
    unsigned different = 0;
    for (unsigned i = 0; i < 64; ++i) different |= (unsigned char)hello.token[i] ^ (unsigned char)token[i];
    if (hello.magic != UURB_X11_INPUT_MAGIC || hello.version != UURB_X11_INPUT_VERSION || different ||
        !response(fd, 0, 1, 0)) return 1;
    puts("UURB_NATIVE_INPUT {\"event\":\"client_authenticated\"}"); fflush(stdout);
    unsigned previous = 0;
    int have_sequence = 0;
    struct uurb_native_scroll scroll = {0};
    struct uurb_input_fraction fraction = {0};
    unsigned applied_settings = settings_generation;
#ifdef UURB_INPUT_EIS
    uint32_t applied_transition = eis_transition;
#endif
    uint64_t last_activity = now_ms();
    while (!stopping && now_ms() < lifetime) {
        reload_settings();
        if (applied_settings != settings_generation) {
            scroll = (struct uurb_native_scroll){0}; fraction = (struct uurb_input_fraction){0};
            applied_settings = settings_generation;
        }
#ifdef UURB_INPUT_EIS
        if (applied_transition != eis_transition) {
            scroll = (struct uurb_native_scroll){0}; fraction = (struct uurb_input_fraction){0};
            applied_transition = eis_transition;
        }
#endif
#ifdef UURB_INPUT_EIS
        int available = wait_io(fd, POLLIN, now_ms() + 250);
        if (stopping) break;
#else
        struct pollfd pending = {.fd = fd, .events = POLLIN};
        int available = poll(&pending, 1, 250);
        if (available < 0) { if (errno == EINTR) continue; break; }
#endif
        if (!available) {
#ifdef UURB_INPUT_EIS
            struct pollfd pending = {.fd = fd, .events = POLLIN};
            if (poll(&pending, 1, 0) > 0 && pending.revents) break;
#endif
            /* Idle without held keys is normal and must not drop the first
             * input after a pause. Expire abandoned holds, not the socket. */
            if (now_ms() - last_activity >= 30000) {
                int released_keys = release_device(keyboard_fd, keys);
                int released_buttons = release_device(mouse_fd, buttons);
                if (!released_keys || !released_buttons) return 0;
                last_activity = now_ms();
            }
            continue;
        }
#ifndef UURB_INPUT_EIS
        if (!(pending.revents & POLLIN)) break;
#endif
        uurb_x11_request request;
#ifdef UURB_INPUT_EIS
        uint32_t batch_transition = eis_transition;
#endif
        /* Once a request starts, incomplete headers/bodies have a deadline. */
        if (!transfer(fd, &request, sizeof(request), 0, 1000)) break;
        if (request.magic != UURB_X11_INPUT_MAGIC || request.reserved || !request.count ||
            request.count > UURB_X11_INPUT_MAX_EVENTS ||
            (have_sequence && request.sequence != previous + 1)) break;
        uurb_x11_input_event input[UURB_X11_INPUT_MAX_EVENTS];
        if (!transfer(fd, input, request.count * sizeof(*input), 0, 1000)) break;
        previous = request.sequence; have_sequence = 1;
        last_activity = now_ms();
#ifdef UURB_INPUT_EIS
        if (!eis_alive()) return 0;
        if (!eis_core_ready || eis_transition != batch_transition) {
            if (!response(fd, request.sequence, 0, UURB_X11_ERROR_INJECTION)) break;
            continue; /* No buffer/replay of input received across a transition. */
        }
        if (applied_transition != eis_transition) {
            /* A transition can complete inside the socket wait, after the
             * loop-top reset. Do not carry an old fractional wheel/motion
             * remainder into the first input on the new mapping. */
            scroll = (struct uurb_native_scroll){0}; fraction = (struct uurb_input_fraction){0};
            applied_transition = eis_transition;
        }
#endif
        unsigned text_events = 0;
        for (unsigned k = 0; k < request.count; ++k)
            text_events += input[k].type == UURB_X11_INPUT_TEXT;
        if (text_events) {
            char utf8[UURB_TEXT_MAX_BYTES];
            size_t bytes = sizeof(utf8);
            uint32_t delete_before = 0;
            int held = 0;
            for (unsigned k = 0; k < KEY_CNT; ++k) held |= keys[k] | buttons[k];
            /* Entire batch validated before any clipboard/key action. The
             * daemon witnesses the old text and selects it, never blindly
             * emits deletion events for the requested prefix. */
            unsigned error = UURB_X11_ERROR_UNSUPPORTED;
            const char *reason = !text_socket ? "no_endpoint" : held ? "held_input" :
                text_events != request.count ? "mixed_batch" : "invalid_plan";
            if (text_socket && !held && text_events == request.count &&
                uurb_native_text_plan(input, request.count, utf8, &bytes, &delete_before)) {
#ifdef UURB_INPUT_EIS
                if (!eis_alive()) { memset(utf8, 0, sizeof(utf8)); return 0; }
                if (!eis_core_ready || eis_transition != batch_transition) {
                    memset(utf8, 0, sizeof(utf8));
                    if (!response(fd, request.sequence, 0, UURB_X11_ERROR_INJECTION)) break;
                    continue;
                }
#endif
                error = uurb_native_text_send_revision(text_socket, utf8, bytes, delete_before) ? 0 : UURB_X11_ERROR_INJECTION;
                reason = error ? "daemon_failed" : "dispatched";
            }
            if (text_reports < 128) {
                ++text_reports;
                printf("UURB_NATIVE_INPUT {\"event\":\"text_result\",\"reason\":\"%s\",\"error\":%u}\n", reason, error);
                fflush(stdout);
            }
            memset(utf8, 0, sizeof(utf8));
            if (!response(fd, request.sequence, error ? 0 : request.count, error)) break;
            continue;
        }
        struct uurb_native_events translated;
        unsigned i;
        for (i = 0; i < request.count; ++i) if (!uurb_native_translate(&input[i], &translated)) break;
        if (i != request.count) {
            if (!response(fd, request.sequence, 0, UURB_X11_ERROR_UNSUPPORTED)) break;
            continue;
        }
        for (i = 0; i < request.count; ++i) {
            uurb_native_translate(&input[i], &translated);
            struct uurb_native_scroll next_scroll = scroll;
            struct uurb_input_fraction next_fraction = fraction;
            uurb_input_scale(&translated, &input_settings, &next_fraction);
#ifndef UURB_INPUT_EIS
            uurb_native_scroll_legacy(&translated, &next_scroll);
#endif
            unsigned char *held = translated.keyboard ? keys : buttons;
            /* Track before write so cleanup also covers an ambiguous partial
             * write. Releasing a key not delivered is harmless. */
            for (size_t k = 0; k < translated.count; ++k)
                if (translated.events[k].type == EV_KEY && translated.events[k].value)
                    held[translated.events[k].code] = 1;
            if (!emit_events(translated.keyboard ? keyboard_fd : mouse_fd,
                             translated.events, translated.count)) break;
            scroll = next_scroll;
            fraction = next_fraction;
            for (size_t k = 0; k < translated.count; ++k)
                if (translated.events[k].type == EV_KEY && !translated.events[k].value)
                    held[translated.events[k].code] = 0;
        }
        if (i != request.count) {
#ifdef UURB_INPUT_EIS
            if (!eis_failed && (eis_suspended || eis_transition != batch_transition)) {
                if (!response(fd, request.sequence, i, UURB_X11_ERROR_INJECTION)) break;
                continue;
            }
#endif
            response(fd, request.sequence, i, UURB_X11_ERROR_INJECTION); return 0;
        }
        if (!response(fd, request.sequence, request.count, 0)) break;
    }
    return 1;
}
int main(int argc, char **argv)
{
    /* Single-output is a required explicit scope, not a claim that arbitrary
     * multi-monitor/DPI changes are already mapped. Launcher must enforce it. */
    if ((argc != 8 && argc != 10 && argc != 12) || strcmp(argv[1], "--user") || strcmp(argv[3], "--token-file") ||
        strcmp(argv[5], "--seconds") || strcmp(argv[7],
#ifdef UURB_INPUT_EIS
        "--mapped-output"
#else
        "--single-output"
#endif
        )) return 2;
    for (int i = 8; i < argc; i += 2) {
        if (argv[i+1][0] != '/') return 2;
        if (!strcmp(argv[i], "--text-socket") && !text_socket) text_socket = argv[i+1];
        else if (!strcmp(argv[i], "--settings-file") && !settings_path) settings_path = argv[i+1];
        else return 2;
    }
    char *end;
    unsigned long seconds = strtoul(argv[6], &end, 10);
    /* Zero is explicit service lifetime; never periodically drop live input. */
    if (!*argv[6] || *end || seconds > 3600 || argv[6][0] == '-') return 2;
    struct passwd *user = getpwnam(argv[2]);
    if (!user || !user->pw_uid || !user->pw_gid) return 2;
    uid_t uid = user->pw_uid;
    gid_t gid = user->pw_gid;
    int listener = -1, result = 1;
    char token[64] = {0};
#ifdef UURB_INPUT_EIS
    /* This variant must never acquire root/uinput authority. Only a previously
     * authorized joint Portal source may supply its two private descriptors. */
    if (!geteuid() || getuid() != geteuid() || getgid() != getegid()) goto done;
    keyboard_fd = 0; mouse_fd = 1; /* routing tags only, never read/write/close */
#else
    keyboard_fd = open("/dev/uinput", O_WRONLY | O_NONBLOCK | O_CLOEXEC | O_NOFOLLOW);
    mouse_fd = open("/dev/uinput", O_WRONLY | O_NONBLOCK | O_CLOEXEC | O_NOFOLLOW);
    if (keyboard_fd < 0 || mouse_fd < 0) goto done;
    if (!geteuid()) {
        if (setgroups(0, NULL) || setresgid(gid, gid, gid) || setresuid(uid, uid, uid)) goto done;
    }
#endif
    if (getuid() != uid || geteuid() != uid || getgid() != gid || getegid() != gid ||
        prctl(PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0) || prctl(PR_SET_DUMPABLE, 0) ||
        !load_token(argv[4], token)) goto done;
    lifetime = seconds ? now_ms() + seconds * 1000 : UINT64_MAX;
    signal(SIGTERM, stop); signal(SIGINT, stop); signal(SIGPIPE, SIG_IGN);
#ifdef UURB_INPUT_EIS
    eis_stage = "parent";
    const char *parent_value = getenv("UURB_EIS_PARENT_PID");
    if (!parent_value || !*parent_value || *parent_value < '1' || *parent_value > '9') goto done;
    errno = 0;
    long parent_pid = strtol(parent_value, &end, 10);
    if (errno || *end || parent_pid <= 1 || parent_pid > INT_MAX || getppid() != parent_pid ||
        prctl(PR_SET_PDEATHSIG, SIGTERM) || getppid() != parent_pid) goto done;
    unsetenv("UURB_EIS_PARENT_PID");
    eis_stage = "descriptors";
    int remote = inherited_socket("UURB_EIS_FD", SOCK_STREAM);
    eis_lease_fd = inherited_socket("UURB_EIS_LEASE_FD", SOCK_SEQPACKET);
    if (remote < 0 || eis_lease_fd < 0 || remote == eis_lease_fd) goto done;
    if (getenv("UURB_EIS_CONTROL_FD")) {
        eis_control_fd = inherited_socket("UURB_EIS_CONTROL_FD", SOCK_SEQPACKET);
        if (eis_control_fd < 0 || eis_control_fd == remote || eis_control_fd == eis_lease_fd) goto done;
    }
    const char *mapping = getenv("UURB_EIS_MAPPING_ID");
    if (!mapping || !*mapping || strnlen(mapping, sizeof(eis_mapping)) >= sizeof(eis_mapping)) goto done;
    strcpy(eis_mapping, mapping);
    eis_stage = "open";
    eis_input = uurb_eis_open(remote, eis_mapping);
    close(remote);
    unsetenv("UURB_EIS_FD"); unsetenv("UURB_EIS_LEASE_FD"); unsetenv("UURB_EIS_MAPPING_ID");
    unsetenv("UURB_EIS_CONTROL_FD");
    if (!eis_input) goto done;
    eis_stage = "negotiation";
    uint64_t negotiate_until = now_ms() + 5000;
    while (!stopping && now_ms() < negotiate_until && now_ms() < lifetime) {
        if (!eis_alive()) goto done;
        if (uurb_eis_dispatch(eis_input) == 1) break;
        wait_io(-1, 0, now_ms() + 100);
    }
    if (stopping || uurb_eis_dispatch(eis_input) != 1) goto done;
    eis_stage = "ready";
#else
    if (!configure_device(keyboard_fd, 1) || !configure_device(mouse_fd, 0)) goto done;
#endif
    reload_settings();
#ifndef UURB_INPUT_EIS
    /* Allow udev/compositor discovery before advertising ready. No input is
     * emitted by startup. Event-window acceptance is a separate real test. */
    struct timespec discover = {.tv_nsec = 500000000};
    nanosleep(&discover, NULL);
#endif
    listener = socket(AF_INET, SOCK_STREAM | SOCK_NONBLOCK | SOCK_CLOEXEC, 0);
    struct sockaddr_in address = {.sin_family = AF_INET, .sin_addr.s_addr = htonl(INADDR_LOOPBACK)};
    if (listener < 0 || bind(listener, (struct sockaddr *)&address, sizeof(address)) || listen(listener, 2)) goto done;
    socklen_t length = sizeof(address);
    if (getsockname(listener, (struct sockaddr *)&address, &length)) goto done;
    printf("UURB_NATIVE_INPUT {\"event\":\"ready\",\"port\":%u,\"uid\":%u,"
#ifdef UURB_INPUT_EIS
           "\"backend\":\"eis\",\"mapped_absolute_input\":true}\n",
#else
           "\"single_output_only\":true}\n",
#endif
           ntohs(address.sin_port), (unsigned)uid); fflush(stdout);
    result = 0;
    while (!stopping && now_ms() < lifetime) {
        if (!wait_io(listener, POLLIN, now_ms() + 250)) continue;
        int client = accept4(listener, NULL, NULL, SOCK_NONBLOCK | SOCK_CLOEXEC);
        if (client < 0) continue;
        int ok = serve_client(client, token);
        close(client);
        int released_keys = release_device(keyboard_fd, keys);
        int released_buttons = release_device(mouse_fd, buttons);
        puts("UURB_NATIVE_INPUT {\"event\":\"client_closed\"}"); fflush(stdout);
        if (!ok || !released_keys || !released_buttons) { result = 1; break; }
    }
done:
    if (listener >= 0) close(listener);
#ifdef UURB_INPUT_EIS
    printf("UURB_NATIVE_INPUT {\"event\":\"eis_exit\",\"stage\":\"%s\",\"reason\":\"%s\",\"lease_or_dispatch_failed\":%s}\n",
           eis_stage, uurb_eis_failure(eis_input), eis_failed ? "true" : "false");
    if (eis_input) {
        release_device(keyboard_fd, keys); release_device(mouse_fd, buttons);
        uurb_eis_close(eis_input);
    }
    if (eis_lease_fd >= 0) close(eis_lease_fd);
    if (eis_control_fd >= 0) close(eis_control_fd);
    memset(eis_mapping, 0, sizeof(eis_mapping));
    if (eis_failed) result = 1;
#else
    if (keyboard_fd >= 0) { release_device(keyboard_fd, keys); ioctl(keyboard_fd, UI_DEV_DESTROY); close(keyboard_fd); }
    if (mouse_fd >= 0) { release_device(mouse_fd, buttons); ioctl(mouse_fd, UI_DEV_DESTROY); close(mouse_fd); }
#endif
    memset(token, 0, sizeof(token));
    printf("UURB_NATIVE_INPUT {\"event\":\"stopped\",\"code\":%d}\n", result);
    return result;
}
