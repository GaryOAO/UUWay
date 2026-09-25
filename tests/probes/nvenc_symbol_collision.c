/* Test-only ELF exports. Linking the encoder directly against these names
 * must fail loudly; handle-scoped dlsym must select the real driver instead. */
#include <ffnvcodec/nvEncodeAPI.h>
#include <stdio.h>

NVENCSTATUS NVENCAPI NvEncodeAPIGetMaxSupportedVersion(uint32_t *version)
{
    (void)version;
    fprintf(stderr, "ERROR: native encoder called interposed NVENC version export\n");
    return NV_ENC_ERR_GENERIC;
}

NVENCSTATUS NVENCAPI NvEncodeAPICreateInstance(NV_ENCODE_API_FUNCTION_LIST *api)
{
    (void)api;
    fprintf(stderr, "ERROR: native encoder called interposed NVENC instance export\n");
    return NV_ENC_ERR_GENERIC;
}
