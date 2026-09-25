#define _GNU_SOURCE
#include "native_text_client.h"
#include <errno.h>
#include <poll.h>
#include <string.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <sys/un.h>
#include <time.h>
#include <unistd.h>
_Static_assert(sizeof(struct uurb_text_header) == 16, "text request ABI");

int uurb_native_text_encode(const uurb_x11_input_event *events, size_t count, char *output, size_t *bytes)
{
    if (!events || !output || !bytes || !count || count > UURB_X11_INPUT_MAX_EVENTS) return 0;
    char temporary[UURB_TEXT_MAX_BYTES];
    size_t used = 0;
    uint32_t high = 0;
    for (size_t i = 0; i < count; ++i) {
        const uurb_x11_input_event *event = &events[i];
        if (event->type != UURB_X11_INPUT_TEXT || event->flags || event->x || event->y ||
            event->virtual_key || event->scan_code || !event->data || event->data > 0xffff) return 0;
        uint32_t cp = event->data;
        if (high) {
            if (cp < 0xdc00 || cp > 0xdfff) return 0;
            cp = 0x10000 + ((high - 0xd800) << 10) + cp - 0xdc00;
            high = 0;
        } else if (cp >= 0xd800 && cp <= 0xdbff) {
            high = cp;
            continue;
        } else if (cp >= 0xdc00 && cp <= 0xdfff) return 0;
        /* Revision/control records need the semantic edit planner, not a
         * literal paste. Newline and tab are ordinary text, never key chords. */
        if (cp < 32 && cp != 9 && cp != 10 && cp != 13) return 0;
        unsigned n = cp < 0x80 ? 1 : cp < 0x800 ? 2 : cp < 0x10000 ? 3 : 4;
        if (n > sizeof(temporary) - used) return 0;
        if (n == 1) temporary[used++] = cp;
        else {
            temporary[used++] = (n == 2 ? 0xc0 : n == 3 ? 0xe0 : 0xf0) | (cp >> (6 * (n - 1)));
            for (unsigned j = n - 1; j; --j) temporary[used++] = 0x80 | ((cp >> (6 * (j - 1))) & 0x3f);
        }
    }
    if (high || !used || used > *bytes) return 0;
    memcpy(output, temporary, used);
    *bytes = used;
    return 1;
}

static uint64_t milliseconds(void)
{
    struct timespec now;
    if (clock_gettime(CLOCK_MONOTONIC, &now)) return UINT64_MAX;
    return (uint64_t)now.tv_sec * 1000 + now.tv_nsec / 1000000;
}

int uurb_native_text_plan(const uurb_x11_input_event *events, size_t count, char *output, size_t *bytes, uint32_t *delete_before)
{
    if (!events || !count || count > UURB_X11_INPUT_MAX_EVENTS || !delete_before) return 0;
    size_t prefix = 0;
    while (prefix < count && events[prefix].data == 8) {
        const uurb_x11_input_event *event = &events[prefix];
        if (event->type != UURB_X11_INPUT_TEXT || event->flags || event->x || event->y || event->virtual_key || event->scan_code) return 0;
        ++prefix;
    }
    /* A replacement must be complete and validated before selecting old text.
     * Backspaces embedded after new text need a richer edit plan, not partial
     * native injection. This supported prefix form never emits Backspace. */
    if (!uurb_native_text_encode(events + prefix, count - prefix, output, bytes)) return 0;
    *delete_before = (uint32_t)prefix;
    return 1;
}
static int ready(int fd, short events, uint64_t deadline)
{
    for (;;) {
        uint64_t now = milliseconds();
        if (now >= deadline) return 0;
        struct pollfd item = {.fd = fd, .events = events};
        int result = poll(&item, 1, (int)(deadline - now));
        if (result >= 0) return result && (item.revents & events);
        if (errno != EINTR) return 0;
    }
}
int uurb_native_text_send(const char *path, const char *utf8, size_t bytes)
{
    return uurb_native_text_send_revision(path, utf8, bytes, 0);
}
int uurb_native_text_send_revision(const char *path, const char *utf8, size_t bytes, uint32_t delete_before)
{
    struct sockaddr_un address = {.sun_family = AF_UNIX};
    if (!path || path[0] != '/' || strlen(path) >= sizeof(address.sun_path) || !utf8 || !bytes || bytes > UURB_TEXT_MAX_BYTES ||
        delete_before > UURB_X11_INPUT_MAX_EVENTS) return 0;
    char parent[sizeof(address.sun_path)];
    strcpy(parent, path);
    char *slash = strrchr(parent, '/');
    if (!slash || slash == parent || !slash[1]) return 0;
    *slash = 0;
    struct stat directory, endpoint;
    if (lstat(parent, &directory) || !S_ISDIR(directory.st_mode) || directory.st_uid != geteuid() || (directory.st_mode & 077) ||
        lstat(path, &endpoint) || !S_ISSOCK(endpoint.st_mode) || endpoint.st_uid != geteuid() || (endpoint.st_mode & 077)) return 0;
    int fd = socket(AF_UNIX, SOCK_SEQPACKET | SOCK_CLOEXEC | SOCK_NONBLOCK, 0);
    if (fd < 0) return 0;
    int ok = 0;
    strcpy(address.sun_path, path);
    if (connect(fd, (struct sockaddr *)&address, sizeof(address))) goto done;
    struct ucred peer;
    socklen_t peer_size = sizeof(peer);
    if (getsockopt(fd, SOL_SOCKET, SO_PEERCRED, &peer, &peer_size) || peer_size != sizeof(peer) || peer.uid != geteuid()) goto done;
    uint8_t packet[sizeof(struct uurb_text_header) + 4 + UURB_TEXT_MAX_BYTES];
    size_t extra = delete_before ? 4 : 0;
    struct uurb_text_header header = {UURB_TEXT_MAGIC, delete_before ? 2 : UURB_TEXT_VERSION, 1, bytes + extra};
    memcpy(packet, &header, sizeof(header));
    if (extra) memcpy(packet + sizeof(header), &delete_before, 4);
    memcpy(packet + sizeof(header) + extra, utf8, bytes);
    uint64_t deadline = milliseconds() + 4000;
    if (!ready(fd, POLLOUT, deadline) || send(fd, packet, sizeof(header) + extra + bytes, MSG_NOSIGNAL) != (ssize_t)(sizeof(header) + extra + bytes) ||
        !ready(fd, POLLIN, deadline)) goto done;
    struct uurb_text_header response;
    ssize_t received = recv(fd, &response, sizeof(response), MSG_TRUNC);
    ok = received == sizeof(response) && response.magic == UURB_TEXT_MAGIC && response.version == UURB_TEXT_VERSION &&
        response.sequence == 1 && !response.size_or_error;
done:
    close(fd);
    return ok;
}
