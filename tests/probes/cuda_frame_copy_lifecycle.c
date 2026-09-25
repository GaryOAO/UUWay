/* Deterministic CUDA driver double: validates the real copier's ownership and
 * failure paths without substituting for the separate physical GPU test. */
#define _POSIX_C_SOURCE 200809L
#include <cuda.h>
#include <assert.h>
#include <fcntl.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include "native_cuda_frame_copy.h"
static const char *fail_operation;
static int contexts, memories, mipmaps, streams, events, depth, copies;
static CUcontext current = (CUcontext)(uintptr_t)0x42, stack[8];
static int fail(const char *name)
{
    if (fail_operation && !strcmp(fail_operation, name)) { fail_operation = NULL; return 1; }
    return 0;
}
#define FAIL(name) do { if (fail(name)) return CUDA_ERROR_UNKNOWN; } while (0)
CUresult CUDAAPI cuGetErrorName(CUresult result, const char **name)
{ (void)result; *name = "test injected failure"; return CUDA_SUCCESS; }
CUresult CUDAAPI cuInit(unsigned flags)
{ assert(!flags); FAIL("init"); return CUDA_SUCCESS; }
CUresult CUDAAPI cuDeviceGetCount(int *count)
{ FAIL("count"); *count = 1; return CUDA_SUCCESS; }
CUresult CUDAAPI cuDeviceGet(CUdevice *device, int ordinal)
{ assert(!ordinal); FAIL("device"); *device = 7; return CUDA_SUCCESS; }
CUresult CUDAAPI cuDeviceGetUuid(CUuuid *uuid, CUdevice device)
{ assert(device == 7); FAIL("uuid"); memset(uuid, 0, sizeof(*uuid)); uuid->bytes[0] = 1; return CUDA_SUCCESS; }
CUresult CUDAAPI cuCtxCreate(CUcontext *context, unsigned flags, CUdevice device)
{
    assert(!flags && device == 7); FAIL("context");
    *context = (CUcontext)(uintptr_t)0x77; stack[depth++] = current; current = *context;
    contexts++; return CUDA_SUCCESS;
}
CUresult CUDAAPI cuCtxPushCurrent(CUcontext context)
{ FAIL("push"); assert(depth < 8); stack[depth++] = current; current = context; return CUDA_SUCCESS; }
CUresult CUDAAPI cuCtxPopCurrent(CUcontext *context)
{ assert(depth > 0); *context = current; current = stack[--depth]; return CUDA_SUCCESS; }
CUresult CUDAAPI cuCtxDestroy(CUcontext context)
{ assert(context == (CUcontext)(uintptr_t)0x77); contexts--; return CUDA_SUCCESS; }
CUresult CUDAAPI cuImportExternalMemory(CUexternalMemory *memory, const CUDA_EXTERNAL_MEMORY_HANDLE_DESC *desc)
{
    assert(current == (CUcontext)(uintptr_t)0x77);
    assert(desc->type == CU_EXTERNAL_MEMORY_HANDLE_TYPE_OPAQUE_FD &&
           desc->flags == CUDA_EXTERNAL_MEMORY_DEDICATED && desc->size == 1048576);
    FAIL(memories ? "import2" : "import1"); assert(!close(desc->handle.fd));
    *memory = (CUexternalMemory)(uintptr_t)(++memories); return CUDA_SUCCESS;
}
CUresult CUDAAPI cuExternalMemoryGetMappedMipmappedArray(CUmipmappedArray *array, CUexternalMemory memory,
                                                       const CUDA_EXTERNAL_MEMORY_MIPMAPPED_ARRAY_DESC *desc)
{
    assert(memory && desc->offset == 0 && desc->numLevels == 1 &&
           desc->arrayDesc.Width == 640 && desc->arrayDesc.Height == 360 &&
           desc->arrayDesc.Depth == 0 && desc->arrayDesc.Format == CU_AD_FORMAT_UNSIGNED_INT8 &&
           desc->arrayDesc.NumChannels == 4 && desc->arrayDesc.Flags ==
           (CUDA_ARRAY3D_SURFACE_LDST | CUDA_ARRAY3D_COLOR_ATTACHMENT));
    FAIL(mipmaps ? "map2" : "map1"); *array = (CUmipmappedArray)(uintptr_t)(++mipmaps); return CUDA_SUCCESS;
}
CUresult CUDAAPI cuMipmappedArrayGetLevel(CUarray *array, CUmipmappedArray mipmap, unsigned level)
{ assert(!level); FAIL(mipmaps == 1 ? "level1" : "level2"); *array = (CUarray)mipmap; return CUDA_SUCCESS; }
CUresult CUDAAPI cuStreamCreate(CUstream *stream, unsigned flags)
{ assert(flags == CU_STREAM_NON_BLOCKING); FAIL("stream"); *stream = (CUstream)(uintptr_t)1; streams++; return CUDA_SUCCESS; }
CUresult CUDAAPI cuEventCreate(CUevent *event, unsigned flags)
{ assert(flags == CU_EVENT_DISABLE_TIMING); FAIL("event"); *event = (CUevent)(uintptr_t)1; events++; return CUDA_SUCCESS; }
CUresult CUDAAPI cuMemcpy3DAsync(const CUDA_MEMCPY3D *copy, CUstream stream)
{
    assert(current == (CUcontext)(uintptr_t)0x77 && stream);
    assert(copy->srcMemoryType == CU_MEMORYTYPE_ARRAY && copy->dstMemoryType == CU_MEMORYTYPE_ARRAY &&
           copy->srcArray && copy->dstArray && copy->srcArray != copy->dstArray &&
           !copy->srcHost && !copy->dstHost && !copy->srcDevice && !copy->dstDevice &&
           !copy->srcXInBytes && !copy->dstXInBytes && !copy->srcY && !copy->dstY &&
           copy->WidthInBytes == 2560 && copy->Height == 360 && copy->Depth == 1);
    FAIL("copy"); copies++; return CUDA_SUCCESS;
}
CUresult CUDAAPI cuEventRecord(CUevent event, CUstream stream)
{ assert(event && stream); FAIL("record"); return CUDA_SUCCESS; }
CUresult CUDAAPI cuEventQuery(CUevent event)
{
    assert(event); FAIL("query");
    if (fail_operation && !strcmp(fail_operation, "timeout")) return CUDA_ERROR_NOT_READY;
    return CUDA_SUCCESS;
}
CUresult CUDAAPI cuEventDestroy(CUevent event)
{ assert(event); events--; return CUDA_SUCCESS; }
CUresult CUDAAPI cuStreamDestroy(CUstream stream)
{ assert(stream); streams--; return CUDA_SUCCESS; }
CUresult CUDAAPI cuMipmappedArrayDestroy(CUmipmappedArray array)
{ assert(array); mipmaps--; return CUDA_SUCCESS; }
CUresult CUDAAPI cuDestroyExternalMemory(CUexternalMemory memory)
{ assert(memory); memories--; return CUDA_SUCCESS; }
static void clean(void)
{
    assert(!contexts && !memories && !mipmaps && !streams && !events && !depth);
    assert(current == (CUcontext)(uintptr_t)0x42);
}
int main(int argc, char **argv)
{
    assert(argc == 2);
    unsigned char uuid[16] = {1};
    int source = open("/dev/null", O_RDONLY), destination = open("/dev/null", O_RDONLY);
    assert(source >= 0 && destination >= 0);
    unsigned width = 640, height = 360;
    uint64_t size = 1048576;
    int invalid = !strncmp(argv[1], "invalid-", 8);
    if (!strcmp(argv[1], "invalid-same-fd")) { close(destination); destination = source; }
    else if (!strcmp(argv[1], "invalid-missing-fd")) { close(source); source = -1; }
    else if (!strcmp(argv[1], "invalid-odd")) width++;
    else if (!strcmp(argv[1], "invalid-small")) width = 0;
    else if (!strcmp(argv[1], "invalid-large")) height = 8192;
    else if (!strcmp(argv[1], "invalid-size")) size = 42;
    else if (!strcmp(argv[1], "invalid-oversize")) size = 1ull << 31;
    else if (!strcmp(argv[1], "invalid-uuid")) uuid[0] = 2;
    else if (strncmp(argv[1], "frame-", 6) && strcmp(argv[1], "success")) fail_operation = argv[1];
    struct uurb_gpu_copy *copy = uurb_gpu_copy_open(source, size, destination, size,
        !strcmp(argv[1], "invalid-null-uuid") ? NULL : uuid, width, height);
    assert(fcntl(source, F_GETFD) == -1 && fcntl(destination, F_GETFD) == -1);
    assert(current == (CUcontext)(uintptr_t)0x42 && !depth);
    if (invalid || (strncmp(argv[1], "frame-", 6) && strcmp(argv[1], "success"))) {
        assert(!copy); clean(); return 0;
    }
    assert(copy);
    if (!strncmp(argv[1], "frame-", 6)) fail_operation = argv[1] + 6;
    int status = uurb_gpu_copy_frame(copy);
    if (!strcmp(argv[1], "frame-push")) assert(status);
    else { assert(!status && copies == 1); assert(!uurb_gpu_copy_frame(copy) && copies == 2); }
    assert(current == (CUcontext)(uintptr_t)0x42 && !depth);
    assert(!uurb_gpu_copy_close(copy)); clean();
    assert(uurb_gpu_copy_frame(NULL) && !uurb_gpu_copy_close(NULL));
    return 0;
}
