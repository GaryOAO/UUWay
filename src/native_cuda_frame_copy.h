#ifndef UURB_CUDA_FRAME_COPY_H
#define UURB_CUDA_FRAME_COPY_H
#include <stdint.h>
struct uurb_gpu_copy;
/* Consumes both owned FDs on every path. Dedicated BGRA allocations only.
 * Source and destination must already be EXTERNAL-owned, GPU-idle and on uuid.
 * Imports persist; each copy is GPU-array to GPU-array, no host pixel buffer.
 * An uncertain GPU completion terminates the isolated worker. */
struct uurb_gpu_copy *uurb_gpu_copy_open(int source_fd, uint64_t source_bytes,
    int destination_fd, uint64_t destination_bytes, const unsigned char uuid[16], unsigned width, unsigned height);
int uurb_gpu_copy_frame(struct uurb_gpu_copy *);
int uurb_gpu_copy_close(struct uurb_gpu_copy *);
#endif
