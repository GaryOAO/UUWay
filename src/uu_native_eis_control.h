#ifndef UURB_NATIVE_EIS_CONTROL_H
#define UURB_NATIVE_EIS_CONTROL_H
#include "native_eis_control_protocol.h"
/* Included only by the opt-in EIS worker, after its single-threaded state. */
static int eis_control_fd = -1, eis_suspended, eis_core_ready;
static uint32_t eis_control_sequence, eis_resume_sequence, eis_transition;
static uint64_t eis_resume_deadline;
static char eis_mapping[4097];

static int eis_control_ack(uint32_t sequence, uint32_t command)
{
    struct uurb_eis_control_reply reply = {UURB_EIS_CONTROL_MAGIC, 1, sequence, command, 0, 0, 0, 0};
    if (command == UURB_EIS_CONTROL_RESUME &&
        !uurb_eis_dimensions(eis_input, &reply.logical_width, &reply.logical_height)) return -1;
    return send(eis_control_fd, &reply, sizeof(reply), MSG_DONTWAIT | MSG_NOSIGNAL) == sizeof(reply) ? 0 : -1;
}
static int eis_control_receive(void)
{
    uint8_t bytes[sizeof(struct uurb_eis_control_request) + 4096];
    union { struct cmsghdr alignment; uint8_t bytes[CMSG_SPACE(4 * sizeof(int))]; } ancillary;
    struct iovec iov = {.iov_base = bytes, .iov_len = sizeof(bytes)};
    struct msghdr message = {.msg_iov = &iov, .msg_iovlen = 1,
        .msg_control = ancillary.bytes, .msg_controllen = sizeof(ancillary)};
    ssize_t size = recvmsg(eis_control_fd, &message, MSG_DONTWAIT | MSG_CMSG_CLOEXEC);
    if (size < 0 && (errno == EINTR || errno == EAGAIN)) return 0;
    int received[4], count = 0, invalid = 0, result = -1;
    if (size <= 0) return -1;
    for (struct cmsghdr *c = CMSG_FIRSTHDR(&message); c; c = CMSG_NXTHDR(&message, c)) {
        if (c->cmsg_level != SOL_SOCKET || c->cmsg_type != SCM_RIGHTS || c->cmsg_len < CMSG_LEN(0)) {
            invalid = 1; continue;
        }
        size_t n = (c->cmsg_len - CMSG_LEN(0)) / sizeof(int);
        for (size_t i = 0; i < n; ++i) {
            int fd; memcpy(&fd, (uint8_t *)CMSG_DATA(c) + i * sizeof(fd), sizeof(fd));
            if (count < 4) received[count++] = fd;
            else { close(fd); invalid = 1; }
        }
    }
    struct uurb_eis_control_request request;
    if (invalid || message.msg_flags & (MSG_TRUNC | MSG_CTRUNC) || size < (ssize_t)sizeof(request)) goto done;
    memcpy(&request, bytes, sizeof(request));
    if (request.magic != UURB_EIS_CONTROL_MAGIC || request.version != 1 || !request.sequence ||
        request.sequence != eis_control_sequence + 1 || request.reserved[0] || request.reserved[1] || request.reserved[2] ||
        request.mapping_bytes > 4096 || size != (ssize_t)(sizeof(request) + request.mapping_bytes) || eis_resume_sequence) goto done;
    if (request.command == UURB_EIS_CONTROL_SUSPEND) {
        if (count || request.mapping_bytes || eis_suspended || !eis_input ||
            !uurb_eis_release(eis_input, 1) || !uurb_eis_release(eis_input, 0)) goto done;
        uurb_eis_close(eis_input); eis_input = NULL;
        memset(keys, 0, sizeof(keys)); memset(buttons, 0, sizeof(buttons));
        eis_suspended = 1; eis_core_ready = 0; ++eis_transition;
        eis_stage = "suspended";
        result = eis_control_ack(request.sequence, request.command);
    } else if (request.command == UURB_EIS_CONTROL_RESUME) {
        if (count != 1 || !eis_suspended || eis_input || request.mapping_bytes != strlen(eis_mapping) ||
            memcmp(bytes + sizeof(request), eis_mapping, request.mapping_bytes)) goto done;
        struct ucred peer; socklen_t peer_size = sizeof(peer);
        if (getsockopt(received[0], SOL_SOCKET, SO_PEERCRED, &peer, &peer_size) || peer.uid != getuid()) goto done;
        eis_input = uurb_eis_open(received[0], eis_mapping);
        if (!eis_input) goto done;
        eis_resume_sequence = request.sequence; eis_resume_deadline = now_ms() + 5000;
        eis_stage = "resuming";
        result = 0; /* ACK only after the new mapping is ready. */
    }
    if (!result) eis_control_sequence = request.sequence;
done:
    for (int i = 0; i < count; ++i) close(received[i]);
    return result;
}
static int eis_dispatch_control(void)
{
    if (eis_control_fd >= 0) {
        struct pollfd p = {.fd = eis_control_fd, .events = POLLIN};
        int ready = poll(&p, 1, 0);
        if ((ready < 0 && errno != EINTR) || (ready > 0 && eis_control_receive() < 0)) return -1;
    }
    int state = eis_input ? uurb_eis_dispatch(eis_input) : (eis_suspended ? 0 : -1);
    if (state < 0) return -1;
    eis_core_ready = state == 1 && !eis_suspended;
    if (eis_resume_sequence) {
        if (state == 1) {
            if (eis_control_ack(eis_resume_sequence, UURB_EIS_CONTROL_RESUME)) return -1;
            eis_resume_sequence = 0; eis_suspended = 0; eis_core_ready = 1; eis_stage = "ready";
        } else if (now_ms() >= eis_resume_deadline) return -1;
    }
    return 0;
}
#endif
