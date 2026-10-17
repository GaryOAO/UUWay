/* AMD compatibility transport. Protocol v4 carries tightly packed BGRA in a
 * size-sealed memfd; never interpret an opaque GPU FD as host memory. */
#define _GNU_SOURCE
#define COBJMACROS
#include <windows.h>
#include <d3d11.h>
#include <sys/mman.h>
#include <sys/stat.h>
#include <fcntl.h>
#include <unistd.h>
#include <stdlib.h>
#include "uu_d3d11_capture_texture.h"
struct uurb_capture_texture {
    ID3D11Texture2D *texture;
    ID3D11DeviceContext *context;
    void *pixels;
    size_t bytes;
    unsigned pitch;
};
HRESULT uurb_capture_texture_open(ID3D11Device *device, int fd,
    const struct uurb_gpu_message *frame, struct uurb_capture_texture **out)
{
    HRESULT result = E_INVALIDARG;
    struct uurb_capture_texture *s = NULL;
    struct stat st;
    if (!device || !out || fd < 0 || !uurb_gpu_message_valid(frame, 1) || frame->kind != UURB_GPU_FRAME ||
        frame->allocation_bytes != (uint64_t)frame->width * frame->height * 4 || fstat(fd, &st) ||
        !S_ISREG(st.st_mode) || st.st_size != (off_t)frame->allocation_bytes) goto done;
    int seals = fcntl(fd, F_GET_SEALS);
    if (seals < 0 || (seals & (F_SEAL_SHRINK | F_SEAL_GROW)) != (F_SEAL_SHRINK | F_SEAL_GROW)) goto done;
    s = calloc(1, sizeof(*s));
    if (!s) { result = E_OUTOFMEMORY; goto done; }
    s->bytes = frame->allocation_bytes; s->pitch = frame->width * 4;
    s->pixels = mmap(NULL, s->bytes, PROT_READ, MAP_SHARED, fd, 0);
    if (s->pixels == MAP_FAILED) { s->pixels = NULL; goto done; }
    ID3D11Device_GetImmediateContext(device, &s->context);
    D3D11_TEXTURE2D_DESC desc = {.Width = frame->width, .Height = frame->height, .MipLevels = 1, .ArraySize = 1,
        .Format = DXGI_FORMAT_B8G8R8A8_UNORM, .SampleDesc = {1, 0}, .Usage = D3D11_USAGE_DEFAULT,
        .BindFlags = D3D11_BIND_RENDER_TARGET | D3D11_BIND_SHADER_RESOURCE};
    result = ID3D11Device_CreateTexture2D(device, &desc, NULL, &s->texture);
    if (FAILED(result)) goto done;
    *out = s; s = NULL;
done:
    if (fd >= 0) close(fd);
    uurb_capture_texture_close(s);
    return result;
}
HRESULT uurb_capture_texture_update(struct uurb_capture_texture *s)
{
    if (!s) return E_INVALIDARG;
    ID3D11DeviceContext_UpdateSubresource(s->context, (ID3D11Resource *)s->texture, 0, NULL, s->pixels, s->pitch, 0);
    return S_OK; /* UpdateSubresource copies caller memory before returning. */
}
ID3D11Texture2D *uurb_capture_texture_get(struct uurb_capture_texture *s)
{ return s ? s->texture : NULL; }
void uurb_capture_texture_close(struct uurb_capture_texture *s)
{
    if (!s) return;
    if (s->pixels) munmap(s->pixels, s->bytes);
    if (s->texture) ID3D11Texture2D_Release(s->texture);
    if (s->context) ID3D11DeviceContext_Release(s->context);
    free(s);
}
