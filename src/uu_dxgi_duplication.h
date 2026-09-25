#ifndef UURB_DXGI_DUPLICATION_H
#define UURB_DXGI_DUPLICATION_H
#include <d3d11.h>
#include <dxgi1_2.h>
/* Experimental constructor, not a hook into DXVK/UU. The Winelib backend
 * consumes a private Unix channel FD >= 3 on every path. A PE loader failure
 * before backend dispatch cannot close a Unix FD: terminate that worker.
 * Waits at most 10 seconds for first metadata.
 * Uses authorized BGRA SDR, fixed-geometry frames. The source cursor is
 * embedded or explicitly excluded; separate cursor metadata is not provided.
 * Calls are serialized internally; the caller must serialize its own D3D11
 * immediate-context usage. A texture reference is not a lease after ReleaseFrame.
 * GPU faults can terminate the process: only use inside an isolated worker.
 * IDXGIObject private-data slots are currently explicitly unimplemented. */
HRESULT WINAPI UurbCreateDuplication(ID3D11Device *, int channel_fd, IUnknown *parent,
                                    IDXGIOutputDuplication **);
HRESULT WINAPI UurbCreateDuplicationEndpoint(ID3D11Device *, const char *socket_path, IUnknown *,
                                            IDXGIOutputDuplication **);
#endif
