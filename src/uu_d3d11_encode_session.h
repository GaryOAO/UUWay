#ifndef UURB_D3D11_ENCODE_SESSION_H
#define UURB_D3D11_ENCODE_SESSION_H
#include <d3d11.h>
#include "native_cuda_encode_session.h"
/* Internal Winelib bridge, NOT the public Windows NVENC ABI. Must run in an
 * isolated worker: a GPU fence timeout terminates the worker, not live UU.
 * Single caller, serialized with the producer's immediate D3D11 context.
 * Up to 16 NV12/BGRA sources share ONE persistent BGRA output and ONE encoder.
 * No per-frame registration, raw pixel readback, or per-texture encoder.
 * NV12 is SDR BT.709 limited; caller must negotiate those parameters first.
 * Legacy lazy-open uses P1/ULL/CBR, 60 Hz, no B frames. Explicit initialize
 * transports the caller-validated synchronous P1/ULL configuration verbatim.
 * Packet bytes are valid until release/close. While a packet is locked all
 * mutation is refused. Failed GPU operations poison the session; close it.
 */
struct uurb_d3d11_encoder;
HRESULT uurb_d3d11_encoder_open(ID3D11Device *, unsigned width, unsigned height,
    int hevc, unsigned bitrate, struct uurb_d3d11_encoder **);
/* Must precede any encode. Config is validated by the Windows ABI boundary;
 * native initialization/import completes now, not on the first frame. */
HRESULT uurb_d3d11_encoder_initialize(struct uurb_d3d11_encoder *, const struct _NV_ENC_INITIALIZE_PARAMS *);
int uurb_d3d11_encoder_sequence(struct uurb_d3d11_encoder *, void *, uint32_t, uint32_t *);
HRESULT uurb_d3d11_encoder_register(struct uurb_d3d11_encoder *, ID3D11Texture2D *, uint64_t *token);
HRESULT uurb_d3d11_encoder_unregister(struct uurb_d3d11_encoder *, uint64_t token);
HRESULT uurb_d3d11_encoder_encode(struct uurb_d3d11_encoder *, uint64_t token,
    uint64_t timestamp, int force_idr, struct uurb_packet *);
HRESULT uurb_d3d11_encoder_release(struct uurb_d3d11_encoder *);
HRESULT uurb_d3d11_encoder_reconfigure(struct uurb_d3d11_encoder *, unsigned bitrate);
struct _NV_ENC_RECONFIGURE_PARAMS;
HRESULT uurb_d3d11_encoder_reconfigure_config(struct uurb_d3d11_encoder *, const struct _NV_ENC_RECONFIGURE_PARAMS *);
/* Frees the handle, including on error; never retry close on the old pointer. */
HRESULT uurb_d3d11_encoder_close(struct uurb_d3d11_encoder *);
#endif
