#ifndef UURB_NATIVE_CAPTURE_STATUS_H
#define UURB_NATIVE_CAPTURE_STATUS_H
#include <stdint.h>
#include <sys/socket.h>
#include <time.h>

/* Separate inherited, same-host status channel. No pixels or GPU FDs. This
 * describes sink ACK, not NVENC completion or remote presentation. */
#define UURB_CAPTURE_STATUS_MAGIC 0x53525555u
struct uurb_capture_status {
    uint32_t magic, version, width, height;
    uint64_t sequence, accepted_ns;
};
_Static_assert(sizeof(struct uurb_capture_status) == 32, "capture status ABI");

static inline int uurb_capture_status_send(int fd, uint32_t width, uint32_t height)
{
    struct timespec now;
    if (fd < 3 || width < 2 || height < 2 || width > 8192 || height > 8192 ||
        clock_gettime(CLOCK_MONOTONIC, &now)) return -1;
    const struct uurb_capture_status status = {
        UURB_CAPTURE_STATUS_MAGIC, 1, width, height, 1,
        (uint64_t)now.tv_sec * 1000000000ull + now.tv_nsec};
    /* Never stall capture, raise SIGPIPE, or retry an ambiguous send. */
    return send(fd, &status, sizeof(status), MSG_DONTWAIT | MSG_NOSIGNAL) == sizeof(status) ? 0 : -1;
}
#endif
