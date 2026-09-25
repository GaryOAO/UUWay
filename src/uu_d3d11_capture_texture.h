#ifndef UURB_D3D11_CAPTURE_TEXTURE_H
#define UURB_D3D11_CAPTURE_TEXTURE_H
#include <d3d11.h>
#include "native_gpu_frame_channel.h"
struct uurb_capture_texture;
/* Consumes source_fd on every path. Only call with a validated first GPU
 * channel frame. This single-caller object belongs to an isolated worker. */
HRESULT uurb_capture_texture_open(ID3D11Device *, int source_fd, const struct uurb_gpu_message *, struct uurb_capture_texture **);
/* Source lease must remain valid until this synchronous GPU copy completes.
 * Call before giving the texture to a consumer, never while it is in use. */
HRESULT uurb_capture_texture_update(struct uurb_capture_texture *);
/* Borrowed texture, owned by this object. Retaining COM refs does not grant
 * permission to sample outside the caller-managed frame lease. */
ID3D11Texture2D *uurb_capture_texture_get(struct uurb_capture_texture *);
void uurb_capture_texture_close(struct uurb_capture_texture *);
#endif
