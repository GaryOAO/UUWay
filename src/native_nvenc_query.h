/* Internal SysV query boundary. This is NOT the Windows function table. */
#ifndef UURB_NVENC_QUERY_H
#define UURB_NVENC_QUERY_H
#include <stdint.h>
#include <ffnvcodec/nvEncodeAPI.h>
enum uurb_nvenc_query_op {
    UURB_CODEC_COUNT, UURB_CODECS, UURB_PROFILE_COUNT, UURB_PROFILES,
    UURB_FORMAT_COUNT, UURB_FORMATS, UURB_PRESET_COUNT, UURB_PRESETS, UURB_CAP
};
int uurb_nvenc_query_version(uint32_t *);
int uurb_nvenc_query_open(const unsigned char uuid[16], void **session);
int uurb_nvenc_query_close(void *session);
int uurb_nvenc_query(void *session, unsigned op, const GUID *codec,
                     void *output, uint32_t capacity_or_cap, uint32_t *count);
int uurb_nvenc_query_preset(void *session, const GUID *codec, const GUID *preset,
                            int extended, NV_ENC_TUNING_INFO tuning,
                            NV_ENC_PRESET_CONFIG *config);
/* Out-of-band headers before initialize; no frame allocation or encoder
 * initialization. Caller validates the full supported configuration first.
 * Inputs are deep-copied and output is committed only on success. */
int uurb_nvenc_query_sequence(void *session, const NV_ENC_INITIALIZE_PARAMS *,
                             void *bytes, uint32_t capacity, uint32_t *size);
#endif
