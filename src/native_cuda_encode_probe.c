/* Isolated hardware lifecycle tests. No production entry point. */
#define _GNU_SOURCE
#include "native_cuda_encode_session.h"
#include <cuda.h>
#include <dirent.h>
#include <fcntl.h>
#include <stdio.h>
#include <string.h>
#include <unistd.h>

static int fd_count(void)
{
    DIR *directory = opendir("/proc/self/fd");
    if (!directory) return -1;
    int count = 0;
    struct dirent *entry;
    while ((entry = readdir(directory))) if (entry->d_name[0] != '.') count++;
    closedir(directory);
    return count;
}
static int current_is(CUcontext expected)
{
    CUcontext actual;
    return cuCtxGetCurrent(&actual) == CUDA_SUCCESS && actual == expected;
}

/* Caller owns fd and has already released the image to VK_QUEUE_FAMILY_EXTERNAL.
 * A sentinel CUDA context detects accidental clobbering of the calling thread. */
int uurb_encoder_probe_lifecycle(int fd, uint64_t bytes, const unsigned char uuid[16],
    unsigned width, unsigned height)
{
    CUcontext sentinel = NULL, popped;
    struct uurb_encoder *s = NULL;
    int result = 1, count = 0, selected = -1, baseline = -1;
    if (cuInit(0) != CUDA_SUCCESS || cuDeviceGetCount(&count) != CUDA_SUCCESS) return 1;
    for (int i = 0; i < count; ++i) {
        CUuuid candidate;
        if (cuDeviceGetUuid(&candidate, i) != CUDA_SUCCESS) return 1;
        if (!memcmp(candidate.bytes, uuid, 16)) selected = i;
    }
    if (selected < 0 || cuCtxCreate(&sentinel, 0, selected) != CUDA_SUCCESS) return 1;
    for (int cycle = 0; cycle < 4; ++cycle) {
        for (int codec = 0; codec < 2; ++codec) {
            s = uurb_encoder_open(fcntl(fd, F_DUPFD_CLOEXEC, 0), bytes, uuid, width, height, codec, 40000000);
            if (!s || !current_is(sentinel)) goto done;
            struct uurb_packet packet, rejected;
            if (!uurb_encoder_release_packet(s) || !uurb_encoder_reconfigure(s, 0)) goto done;
            if (uurb_encoder_encode(s, 100, 1, &packet) || !packet.idr || !current_is(sentinel)) goto done;
            if (!uurb_encoder_encode(s, 101, 0, &rejected) ||
                !uurb_encoder_reconfigure(s, 20000000)) goto done; /* packet is still locked */
            if (uurb_encoder_release_packet(s) || !current_is(sentinel)) goto done;
            if (!uurb_encoder_release_packet(s) || !uurb_encoder_encode(s, 100, 0, &rejected)) goto done;
            if (uurb_encoder_reconfigure(s, 20000000) || !current_is(sentinel)) goto done;
            if (uurb_encoder_encode(s, 101, 0, &packet) || !packet.idr || !current_is(sentinel)) goto done;
            /* Last cycle intentionally closes with a locked output to exercise
             * cleanup on consumer failure. Other cycles follow normal release. */
            if (cycle != 3 && uurb_encoder_release_packet(s)) goto done;
            int closed = uurb_encoder_close(s);
            s = NULL;
            if (closed || !current_is(sentinel)) goto done;
        }
        int now = fd_count();
        if (now < 0) goto done;
        if (cycle == 0) baseline = now; /* exclude driver one-time initialization */
        fprintf(stderr, "Native lifecycle: cycle=%d fd_count=%d warm_baseline=%d context_restored=yes\n", cycle, now, baseline);
        if (now > baseline) { fprintf(stderr, "FD growth across encoder close\n"); goto done; }
    }
    result = 0;
done:
    if (uurb_encoder_close(s)) result = 1;
    if (!current_is(sentinel)) return 1; /* do not pop an unrelated context */
    if (cuCtxPopCurrent(&popped) != CUDA_SUCCESS || cuCtxDestroy(sentinel) != CUDA_SUCCESS) result = 1;
    if (result) fprintf(stderr, "Native lifecycle contract failed\n");
    return result;
}
