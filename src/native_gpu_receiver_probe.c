/* GPU-channel acceptance consumer. Native NVENC validates imported frames;
 * not a Windows/UU implementation. No capture permission is requested here. */
#define _GNU_SOURCE
#include "native_gpu_frame_channel.h"
#include "native_cuda_encode_session.h"
#include <sys/stat.h>
#include <fcntl.h>
#include <errno.h>
#include <limits.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
static int number(const char *s)
{
    if (!s || !*s || *s < '0' || *s > '9') return -1;
    char *end;
    errno = 0;
    long n = strtol(s, &end, 10);
    return errno || *end || n < 3 || n > INT_MAX ? -1 : (int)n;
}
static int write_packet(int fd, const void *data, size_t size)
{
    const unsigned char *position = data;
    while (size) {
        ssize_t n = write(fd, position, size);
        if (n < 0 && errno == EINTR) continue;
        if (n <= 0) return 1;
        position += n; size -= n;
    }
    return 0;
}
int main(int argc, char **argv)
{
    if (argc != 4 || (strcmp(argv[3], "h264") && strcmp(argv[3], "hevc"))) return 2;
    int channel = number(argv[1]), output = number(argv[2]), hevc = !strcmp(argv[3], "hevc");
    struct stat stat;
    if (channel < 0 || output < 0 || channel == output || uurb_gpu_channel_check(channel) ||
        fstat(output, &stat) || !S_ISREG(stat.st_mode) || (fcntl(output, F_GETFL) & O_ACCMODE) == O_RDONLY) return 2;
    struct uurb_encoder *encoder = NULL;
    struct uurb_gpu_message first = {0};
    uint64_t frames = 0, timestamp = 0, written = 0;
    int failed = 1, memory_fd = -1;
    for (;;) {
        struct uurb_gpu_message message;
        if (uurb_gpu_channel_receive(channel, &message, &memory_fd, 10000)) break;
        if (message.sequence != frames + 1) break;
        if (message.kind == UURB_GPU_END) { failed = !frames; break; }
        if (message.kind != UURB_GPU_FRAME || (frames && message.timestamp <= timestamp)) break;
        if (!frames) {
            first = message;
            int fd = memory_fd; memory_fd = -1;
            encoder = uurb_encoder_open(fd, message.allocation_bytes, message.uuid,
                                       message.width, message.height, hevc, 20000000);
            if (!encoder) break;
        } else if (memory_fd >= 0 || message.width != first.width || message.height != first.height ||
            message.allocation_bytes != first.allocation_bytes || memcmp(message.uuid, first.uuid, 16)) break;
        struct uurb_packet packet;
        if (uurb_encoder_encode(encoder, message.timestamp, !frames, &packet)) break;
        int invalid = packet.size > 64u * 1024u * 1024u - written;
        if (!invalid) invalid = write_packet(output, packet.data, packet.size);
        if (!invalid) written += packet.size;
        if (uurb_encoder_release_packet(encoder) || invalid) break;
        struct uurb_gpu_message ack = {.magic = UURB_GPU_CHANNEL_MAGIC, .version = UURB_GPU_CHANNEL_VERSION,
            .kind = UURB_GPU_ACK, .sequence = message.sequence};
        /* Explicit GPU completion and bitstream release precede acknowledgment. */
        if (uurb_gpu_channel_send(channel, &ack, -1, 2000)) break;
        timestamp = message.timestamp; frames++;
    }
    if (memory_fd >= 0) close(memory_fd);
    if (uurb_encoder_close(encoder)) failed = 1;
    if (!failed) printf("{\"frames\":%llu,\"width\":%u,\"height\":%u,\"codec\":\"%s\","
        "\"encoded\":true,\"gpu_channel_received\":true,\"encoded_bytes\":%llu,\"uu_session_tested\":false}\n",
        (unsigned long long)frames, first.width, first.height, hevc ? "hevc" : "h264", (unsigned long long)written);
    return failed;
}
