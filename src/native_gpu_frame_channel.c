#define _GNU_SOURCE
#include "native_gpu_frame_channel.h"
#include <sys/socket.h>
#include <sys/un.h>
#include <unistd.h>
#include <poll.h>
#include <errno.h>
#include <limits.h>
#include <string.h>
#include <time.h>
#include <fcntl.h>
#include <sys/stat.h>
#include <stdio.h>

int uurb_gpu_channel_connect(const char *path)
{
    struct sockaddr_un address = {.sun_family = AF_UNIX};
    if (!path || path[0] != '/' || strlen(path) >= sizeof(address.sun_path)) return -1;
    char parent[sizeof(address.sun_path)];
    strcpy(parent, path);
    char *slash = strrchr(parent, '/');
    if (!slash || slash == parent || !slash[1]) return -1;
    *slash = 0;
    struct stat directory, endpoint;
    if (lstat(parent, &directory) || !S_ISDIR(directory.st_mode) || directory.st_uid != geteuid() ||
        (directory.st_mode & 077) || lstat(path, &endpoint) || !S_ISSOCK(endpoint.st_mode) ||
        endpoint.st_uid != geteuid() || (endpoint.st_mode & 077)) return -1;
    int fd = socket(AF_UNIX, SOCK_SEQPACKET | SOCK_CLOEXEC | SOCK_NONBLOCK, 0);
    if (fd < 0) return -1;
    if (fd < 3) {
        int replacement = fcntl(fd, F_DUPFD_CLOEXEC, 3);
        close(fd); fd = replacement;
        if (fd < 0) return -1;
    }
    strcpy(address.sun_path, path);
    /* Full backlog fails promptly; never block a UU thread on connect. */
    if (connect(fd, (struct sockaddr *)&address, sizeof(address)) || uurb_gpu_channel_check(fd)) {
        close(fd); return -1;
    }
    return fd;
}

#if __BYTE_ORDER__ != __ORDER_LITTLE_ENDIAN__
#error GPU channel requires little-endian peers
#endif
static int64_t milliseconds(void)
{
    struct timespec now;
    if (clock_gettime(CLOCK_MONOTONIC, &now)) return -1;
    return (int64_t)now.tv_sec * 1000 + now.tv_nsec / 1000000;
}
static int wait_ready(int fd, short events, unsigned timeout)
{
    if (timeout > 10000) return 1;
    int64_t now = milliseconds();
    if (now < 0) return 1;
    int64_t deadline = now + timeout;
    for (;;) {
        struct pollfd descriptor = {.fd = fd, .events = events};
        int result = poll(&descriptor, 1, (int)(deadline > now ? deadline - now : 0));
        if (result >= 0) return !(result && (descriptor.revents & events));
        if (errno != EINTR || (now = milliseconds()) < 0 || now >= deadline) return 1;
    }
}
int uurb_gpu_channel_check(int fd)
{
    int type = 0;
    socklen_t size = sizeof(type);
    if (getsockopt(fd, SOL_SOCKET, SO_TYPE, &type, &size) || type != SOCK_SEQPACKET) return 1;
    struct sockaddr_un address;
    size = sizeof(address);
    if (getpeername(fd, (struct sockaddr *)&address, &size) || address.sun_family != AF_UNIX) return 1;
    struct ucred credentials;
    size = sizeof(credentials);
    return getsockopt(fd, SOL_SOCKET, SO_PEERCRED, &credentials, &size) ||
        size != sizeof(credentials) || credentials.uid != geteuid();
}
int uurb_gpu_message_valid(const struct uurb_gpu_message *m, int has_fd)
{
    if (!m || m->magic != UURB_GPU_CHANNEL_MAGIC || m->version != UURB_GPU_CHANNEL_VERSION ||
        !m->sequence || m->reserved) return 0;
    if (m->kind == UURB_GPU_FRAME) {
        if ((m->rate_num && !m->rate_den) || (!m->rate_num && m->rate_den > 1) ||
            (m->max_rate_num && !m->max_rate_den) || (!m->max_rate_num && m->max_rate_den > 1)) return 0;
        unsigned nonzero = 0;
        for (unsigned i = 0; i < 16; ++i) nonzero |= m->uuid[i];
        return (has_fd == (m->sequence == 1)) && nonzero && m->ready_ns && m->width >= 2 && m->height >= 2 &&
            m->width <= 4096 && m->height <= 4096 && !((m->width | m->height) & 1) &&
            m->allocation_bytes >= (uint64_t)m->width * m->height * 4 && m->allocation_bytes <= (1ull << 30);
    }
    struct uurb_gpu_message expected = {.magic = UURB_GPU_CHANNEL_MAGIC, .version = UURB_GPU_CHANNEL_VERSION,
        .kind = m->kind, .sequence = m->sequence};
    return !has_fd && (m->kind == UURB_GPU_ACK || m->kind == UURB_GPU_END) && !memcmp(m, &expected, sizeof(expected));
}
int uurb_gpu_channel_send(int fd, const struct uurb_gpu_message *m, int memory_fd, unsigned timeout)
{
    if (!uurb_gpu_message_valid(m, memory_fd >= 0) || wait_ready(fd, POLLOUT, timeout)) return 1;
    struct iovec data = {.iov_base = (void *)m, .iov_len = sizeof(*m)};
    union { struct cmsghdr align; unsigned char bytes[CMSG_SPACE(sizeof(int))]; } control = {0};
    struct msghdr message = {.msg_iov = &data, .msg_iovlen = 1};
    if (memory_fd >= 0) {
        message.msg_control = control.bytes; message.msg_controllen = sizeof(control);
        struct cmsghdr *c = CMSG_FIRSTHDR(&message);
        c->cmsg_level = SOL_SOCKET; c->cmsg_type = SCM_RIGHTS; c->cmsg_len = CMSG_LEN(sizeof(int));
        memcpy(CMSG_DATA(c), &memory_fd, sizeof(memory_fd));
    }
    /* A nonblocking single seqpacket send cannot silently write half a frame. */
    return sendmsg(fd, &message, MSG_NOSIGNAL | MSG_DONTWAIT) != (ssize_t)sizeof(*m);
}
int uurb_gpu_channel_receive(int fd, struct uurb_gpu_message *out, int *memory_fd, unsigned timeout)
{
    if (!out || !memory_fd || wait_ready(fd, POLLIN, timeout)) return 1;
    struct uurb_gpu_message received;
    struct iovec data = {.iov_base = &received, .iov_len = sizeof(received)};
    union { struct cmsghdr align; unsigned char bytes[CMSG_SPACE(4 * sizeof(int))]; } control = {0};
    struct msghdr message = {.msg_iov = &data, .msg_iovlen = 1,
        .msg_control = control.bytes, .msg_controllen = sizeof(control)};
    ssize_t bytes = recvmsg(fd, &message, MSG_CMSG_CLOEXEC | MSG_DONTWAIT);
    if (bytes < 0) return 1;
    int descriptors[4], count = 0, invalid = bytes != sizeof(received) || (message.msg_flags & (MSG_TRUNC | MSG_CTRUNC));
    for (struct cmsghdr *c = CMSG_FIRSTHDR(&message); c; c = CMSG_NXTHDR(&message, c)) {
        if (c->cmsg_level != SOL_SOCKET || c->cmsg_type != SCM_RIGHTS || c->cmsg_len < CMSG_LEN(0)) { invalid = 1; continue; }
        size_t payload = c->cmsg_len - CMSG_LEN(0);
        if (payload % sizeof(int)) invalid = 1;
        for (size_t i = 0; i < payload / sizeof(int); ++i) {
            int value;
            memcpy(&value, (unsigned char *)CMSG_DATA(c) + i * sizeof(int), sizeof(value));
            if (count == 4) { close(value); invalid = 1; }
            else descriptors[count++] = value;
        }
    }
    if (invalid || count > 1 || !uurb_gpu_message_valid(&received, count == 1)) {
        for (int i = 0; i < count; ++i) close(descriptors[i]);
        return 1;
    }
    *out = received; *memory_fd = count ? descriptors[0] : -1;
    return 0;
}
int uurb_gpu_publish(void *opaque, int memory_fd, uint64_t bytes, const unsigned char uuid[16],
    uint32_t width, uint32_t height, uint64_t timestamp)
{
    struct uurb_gpu_publisher *s = opaque;
    if (!s || s->failed || s->sequence == UINT64_MAX || (s->sequence && timestamp <= s->timestamp)) return 1;
    struct uurb_gpu_message m = {.magic = UURB_GPU_CHANNEL_MAGIC, .version = UURB_GPU_CHANNEL_VERSION,
        .kind = UURB_GPU_FRAME, .width = width, .height = height, .sequence = s->sequence + 1,
        .timestamp = timestamp, .allocation_bytes = bytes,
        .rate_num = s->rate_num, .rate_den = s->rate_den,
        .max_rate_num = s->max_rate_num, .max_rate_den = s->max_rate_den};
    struct timespec ready;
    if (clock_gettime(CLOCK_MONOTONIC, &ready)) { s->failed = 1; return 1; }
    m.ready_ns = (uint64_t)ready.tv_sec * 1000000000ull + ready.tv_nsec;
    memcpy(m.uuid, uuid, 16);
    struct uurb_gpu_message ack;
    int unexpected = -1;
    s->failed = 1;
    int64_t started = milliseconds();
    const char *stage = NULL;
    if (uurb_gpu_channel_send(s->socket_fd, &m, s->sequence ? -1 : memory_fd, 2000)) stage = "send";
    else if (uurb_gpu_channel_receive(s->socket_fd, &ack, &unexpected, 2000)) stage = "receive";
    if (stage) {
        int64_t ended = milliseconds();
        fprintf(stderr, "UURB_GPU_FAILURE {\"stage\":\"%s\",\"sequence\":%llu,\"wait_ms\":%lld}\n",
            stage, (unsigned long long)m.sequence, (long long)(started >= 0 && ended >= started ? ended - started : -1));
        return 1;
    }
    if (unexpected >= 0) close(unexpected);
    if (unexpected >= 0 || ack.kind != UURB_GPU_ACK || ack.sequence != m.sequence) return 1;
    s->sequence = m.sequence; s->timestamp = timestamp; s->failed = 0;
    return 0;
}
int uurb_gpu_publish_end(struct uurb_gpu_publisher *s)
{
    if (!s || s->failed || !s->sequence || s->sequence == UINT64_MAX) return 1;
    struct uurb_gpu_message m = {.magic = UURB_GPU_CHANNEL_MAGIC, .version = UURB_GPU_CHANNEL_VERSION,
        .kind = UURB_GPU_END, .sequence = s->sequence + 1};
    s->failed = 1; /* Terminal even if the peer disappeared. */
    return uurb_gpu_channel_send(s->socket_fd, &m, -1, 2000);
}
