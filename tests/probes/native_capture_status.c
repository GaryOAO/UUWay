#define _GNU_SOURCE
#include "native_capture_status.h"
#include <assert.h>
#include <unistd.h>
int main(void)
{
    int pair[2];
    assert(!socketpair(AF_UNIX, SOCK_SEQPACKET | SOCK_CLOEXEC, 0, pair));
    assert(uurb_capture_status_send(pair[0], 0, 1080) == -1);
    assert(!uurb_capture_status_send(pair[0], 1920, 1080));
    struct uurb_capture_status received;
    assert(recv(pair[1], &received, sizeof(received), 0) == sizeof(received));
    assert(received.magic == UURB_CAPTURE_STATUS_MAGIC && received.version == 1);
    assert(received.width == 1920 && received.height == 1080 && received.sequence == 1 && received.accepted_ns);
    close(pair[1]);
    assert(uurb_capture_status_send(pair[0], 1920, 1080) == -1); /* No SIGPIPE. */
    close(pair[0]);
    return 0;
}
