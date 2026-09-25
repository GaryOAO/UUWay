#ifndef UURB_D3D11_FRAME_ADAPTER_H
#define UURB_D3D11_FRAME_ADAPTER_H
#include <d3d11.h>

/* One registered source/output pair. SDR BT.709 limited NV12 -> BGRA, or
 * BGRA -> BGRA, no scaling. Resources and recorded commands are retained.
 * No raw host pixel access. This is not an NVENC resource-registration API.
 * Sources without a shader-resource bind use a retained sampleable GPU copy;
 * existing sampleable inputs keep the direct path. No per-frame allocation.
 *
 * Caller serializes the immediate context with its producer, queues source
 * writes before submit, and owns the output exclusively until GPU completion.
 * Submit only queues work: the caller MUST fence before exporting the output
 * to Vulkan/CUDA, and reacquire ownership before submitting again. No internal
 * flush, wait, cross-API ownership transfer, or thread-safety is implied.
 * Destroy after outstanding work/consumers have completed. Resize requires
 * unregister/recreate; unsupported descriptors fail without changing *out.
 */
struct uurb_d3d11_frame_adapter;
HRESULT uurb_d3d11_frame_adapter_create(ID3D11Device *device,
    ID3D11Texture2D *source, ID3D11Texture2D *output,
    struct uurb_d3d11_frame_adapter **out);
HRESULT uurb_d3d11_frame_adapter_submit(struct uurb_d3d11_frame_adapter *adapter,
    ID3D11DeviceContext *immediate);
void uurb_d3d11_frame_adapter_destroy(struct uurb_d3d11_frame_adapter *adapter);
#endif
