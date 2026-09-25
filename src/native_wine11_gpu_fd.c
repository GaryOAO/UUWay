/* Isolated Unix-side Wine 11.0 adapter, server protocol 930.
 * Compiled separately from Winelib to avoid mixing Windows/SysV call ABIs.
 * This private interface is not suitable for unpinned Wine versions. */
#define _GNU_SOURCE
#define __WINESRC__
#include <wine/server.h>
#include <dlfcn.h>
#include <stdio.h>

int uurb_wine11_gpu_fd(void *shared, void **opened)
{
    _Static_assert(SERVER_PROTOCOL_VERSION == 930, "Wine protocol version mismatch");
    void *ntdll = dlopen("/opt/wine-stable/lib/wine/x86_64-unix/ntdll.so", RTLD_NOW | RTLD_NOLOAD);
    if (!ntdll) { fprintf(stderr, "Pinned Wine Unix runtime is not loaded\n"); return -1; }
    unsigned int (*server_call)(void *) = dlsym(ntdll, "wine_server_call");
    unsigned int (*to_fd)(HANDLE, unsigned int, int *, unsigned int *) =
        dlsym(ntdll, "wine_server_handle_to_fd");
    dlclose(ntdll); /* Wine itself retains the module for the process lifetime. */
    *opened = NULL;
    if (!server_call || !to_fd) {
        fprintf(stderr, "Wine Unix server exports unavailable\n"); return -1;
    }
    struct __server_request_info request = {0};
    request.u.req.d3dkmt_object_open_request.__header.req = REQ_d3dkmt_object_open;
    request.u.req.d3dkmt_object_open_request.type = D3DKMT_RESOURCE;
    request.u.req.d3dkmt_object_open_request.handle = wine_server_obj_handle(shared);
    unsigned int status = server_call(&request);
    if (status) {
        fprintf(stderr, "Wine 11 D3DKMT resource open failed: %08x\n", status); return -1;
    }
    *opened = wine_server_ptr_handle(request.u.reply.d3dkmt_object_open_reply.handle);
    int fd = -1;
    status = to_fd(*opened, GENERIC_ALL, &fd, NULL);
    if (status) {
        fprintf(stderr, "Wine Unix FD conversion failed: %08x\n", status); return -1;
    }
    return fd;
}
