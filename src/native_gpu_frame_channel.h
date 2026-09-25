#ifndef UURB_GPU_FRAME_CHANNEL_H
#define UURB_GPU_FRAME_CHANNEL_H
#include <stdint.h>
/* Same-host, little-endian protocol. Pixels never appear in messages. One
 * dedicated opaque-memory FD is transferred with frame 1; subsequent frames
 * refer to the same allocation. Exactly one frame may be outstanding. */
#define UURB_GPU_CHANNEL_MAGIC 0x47525555u
#define UURB_GPU_CHANNEL_VERSION 3u
enum { UURB_GPU_FRAME = 1, UURB_GPU_ACK = 2, UURB_GPU_END = 3 };
struct uurb_gpu_message {
    uint32_t magic, version, kind, width, height, reserved;
    /* timestamp is producer PTS (possibly predicted presentation time).
     * ready_ns is CLOCK_MONOTONIC sampled after GPU production completes. */
    uint64_t sequence, timestamp, allocation_bytes, ready_ns;
    unsigned char uuid[16];
    /* Negotiated source cadence, NOT measured FPS or physical EDID timing.
     * 0/0 is absent, 0/1 is variable; max_rate applies only to 0/1. */
    uint32_t rate_num, rate_den, max_rate_num, max_rate_den;
};
_Static_assert(sizeof(struct uurb_gpu_message) == 88, "GPU channel ABI");
/* Only connected AF_UNIX SOCK_SEQPACKET sockets owned by our effective UID.
 * This is a private inherited channel, not a public socket/authorization API. */
int uurb_gpu_channel_check(int fd);
/* A fresh non-inheritable connection, not the legacy inherited descriptor.
 * Requires an owner-only directory/socket and a same-UID peer. */
int uurb_gpu_channel_connect(const char *path);
int uurb_gpu_message_valid(const struct uurb_gpu_message *, int has_fd);
int uurb_gpu_channel_send(int socket_fd, const struct uurb_gpu_message *, int memory_fd, unsigned timeout_ms);
/* On success the caller owns *memory_fd (possibly -1); on error output is
 * unchanged and any received descriptors are closed, including truncation. */
int uurb_gpu_channel_receive(int socket_fd, struct uurb_gpu_message *, int *memory_fd, unsigned timeout_ms);
struct uurb_gpu_publisher {
    int socket_fd, failed;
    uint64_t sequence, timestamp;
    uint32_t rate_num, rate_den, max_rate_num, max_rate_den;
};
int uurb_gpu_publish(void *, int memory_fd, uint64_t bytes, const unsigned char uuid[16],
                     uint32_t width, uint32_t height, uint64_t timestamp);
int uurb_gpu_publish_end(struct uurb_gpu_publisher *);
#endif
