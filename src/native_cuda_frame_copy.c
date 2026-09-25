#define _POSIX_C_SOURCE 200809L
#include "native_cuda_frame_copy.h"
#include <cuda.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <unistd.h>
struct imported_image { CUexternalMemory memory; CUmipmappedArray mipmap; CUarray array; };
struct uurb_gpu_copy {
    CUcontext context;
    CUstream stream;
    CUevent completed;
    struct imported_image images[2];
    unsigned width, height;
};
static int check(CUresult status, const char *operation)
{
    if (status == CUDA_SUCCESS) return 0;
    const char *name = NULL;
    cuGetErrorName(status, &name);
    fprintf(stderr, "GPU copy %s: %s (%d)\n", operation, name ? name : "CUDA error", status);
    return 1;
}
#define CU(call) do { if (check((call), #call)) goto done; } while (0)
static int import_image(struct imported_image *image, int *fd, uint64_t bytes, unsigned width, unsigned height)
{
    CUDA_EXTERNAL_MEMORY_HANDLE_DESC external = {0};
    external.type = CU_EXTERNAL_MEMORY_HANDLE_TYPE_OPAQUE_FD;
    external.handle.fd = *fd; external.size = bytes; external.flags = CUDA_EXTERNAL_MEMORY_DEDICATED;
    CU(cuImportExternalMemory(&image->memory, &external));
    *fd = -1;
    CUDA_EXTERNAL_MEMORY_MIPMAPPED_ARRAY_DESC desc = {0};
    desc.arrayDesc.Width = width; desc.arrayDesc.Height = height;
    desc.arrayDesc.Format = CU_AD_FORMAT_UNSIGNED_INT8; desc.arrayDesc.NumChannels = 4;
    desc.arrayDesc.Flags = CUDA_ARRAY3D_SURFACE_LDST | CUDA_ARRAY3D_COLOR_ATTACHMENT;
    desc.numLevels = 1;
    CU(cuExternalMemoryGetMappedMipmappedArray(&image->mipmap, image->memory, &desc));
    CU(cuMipmappedArrayGetLevel(&image->array, image->mipmap, 0));
    return 0;
done:
    return 1;
}
struct uurb_gpu_copy *uurb_gpu_copy_open(int source_fd, uint64_t source_bytes,
    int destination_fd, uint64_t destination_bytes, const unsigned char uuid[16], unsigned width, unsigned height)
{
    struct uurb_gpu_copy *s = NULL;
    int ready = 0, current = 0;
    if (source_fd < 0 || destination_fd < 0 || source_fd == destination_fd || !uuid ||
        width < 2 || height < 2 || width > 4096 || height > 4096 || ((width | height) & 1) ||
        source_bytes < (uint64_t)width * height * 4 || destination_bytes < (uint64_t)width * height * 4 ||
        source_bytes > (1ull << 30) || destination_bytes > (1ull << 30)) goto done;
    s = calloc(1, sizeof(*s));
    if (!s) goto done;
    CU(cuInit(0));
    int devices = 0, selected = -1;
    CU(cuDeviceGetCount(&devices));
    for (int i = 0; i < devices; ++i) {
        CUdevice device;
        CUuuid candidate;
        CU(cuDeviceGet(&device, i));
        CU(cuDeviceGetUuid(&candidate, device));
        if (!memcmp(candidate.bytes, uuid, 16)) { selected = device; break; }
    }
    if (selected < 0) goto done;
    CU(cuCtxCreate(&s->context, 0, selected));
    current = 1;
    if (import_image(&s->images[0], &source_fd, source_bytes, width, height) ||
        import_image(&s->images[1], &destination_fd, destination_bytes, width, height)) goto done;
    CU(cuStreamCreate(&s->stream, CU_STREAM_NON_BLOCKING));
    CU(cuEventCreate(&s->completed, CU_EVENT_DISABLE_TIMING));
    s->width = width; s->height = height; ready = 1;
done:
    if (source_fd >= 0) close(source_fd);
    if (destination_fd >= 0 && destination_fd != source_fd) close(destination_fd);
    if (current) {
        CUcontext previous;
        if (check(cuCtxPopCurrent(&previous), "open pop context")) ready = 0;
    }
    if (!ready) { if (uurb_gpu_copy_close(s)) _Exit(4); return NULL; }
    return s;
}
int uurb_gpu_copy_frame(struct uurb_gpu_copy *s)
{
    if (!s) return 1;
    CU(cuCtxPushCurrent(s->context));
    CUDA_MEMCPY3D copy = {0};
    copy.srcMemoryType = CU_MEMORYTYPE_ARRAY; copy.srcArray = s->images[0].array;
    copy.dstMemoryType = CU_MEMORYTYPE_ARRAY; copy.dstArray = s->images[1].array;
    copy.WidthInBytes = (size_t)s->width * 4; copy.Height = s->height; copy.Depth = 1;
    /* No fallthrough cleanup after a possibly submitted operation: the caller
     * must not acknowledge/reuse either image if completion is uncertain. */
    if (check(cuMemcpy3DAsync(&copy, s->stream), "device array copy") ||
        check(cuEventRecord(s->completed, s->stream), "record completion")) _Exit(4);
    struct timespec start, now;
    if (clock_gettime(CLOCK_MONOTONIC, &start)) _Exit(4);
    for (;;) {
        CUresult status = cuEventQuery(s->completed);
        if (status == CUDA_SUCCESS) break;
        if (status != CUDA_ERROR_NOT_READY || clock_gettime(CLOCK_MONOTONIC, &now) ||
            (now.tv_sec - start.tv_sec) * 1000000000ll + now.tv_nsec - start.tv_nsec >= 2000000000ll) {
            fprintf(stderr, "GPU array copy completion failed; exiting isolated worker\n"); _Exit(4);
        }
        struct timespec pause = {.tv_nsec = 100000};
        nanosleep(&pause, NULL);
    }
    CUcontext previous;
    if (check(cuCtxPopCurrent(&previous), "copy pop context")) _Exit(4);
    return 0;
done:
    return 1;
}
int uurb_gpu_copy_close(struct uurb_gpu_copy *s)
{
    if (!s) return 0;
    int failed = 0;
    if (s->context && check(cuCtxPushCurrent(s->context), "close push context")) return 1;
    if (s->completed) failed |= check(cuEventDestroy(s->completed), "destroy event");
    if (s->stream) failed |= check(cuStreamDestroy(s->stream), "destroy stream");
    for (unsigned i = 0; i < 2; ++i) {
        if (s->images[i].mipmap) failed |= check(cuMipmappedArrayDestroy(s->images[i].mipmap), "destroy mipmap");
        if (s->images[i].memory) failed |= check(cuDestroyExternalMemory(s->images[i].memory), "destroy memory");
    }
    if (s->context) {
        CUcontext previous;
        failed |= check(cuCtxPopCurrent(&previous), "close pop context");
        failed |= check(cuCtxDestroy(s->context), "destroy context");
    }
    free(s);
    return failed;
}
