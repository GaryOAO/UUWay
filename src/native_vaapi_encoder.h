#ifndef UURB_VAAPI_ENCODER_H
#define UURB_VAAPI_ENCODER_H
#include "native_cuda_encode_session.h"
#include <ffnvcodec/nvEncodeAPI.h>
/* AMD compatibility backend. Pixels cross host memory; compression is VA-API. */
int uurb_vaapi_device(const unsigned char uuid[16], char node[128]);
struct uurb_encoder *uurb_vaapi_open(const unsigned char uuid[16], const NV_ENC_INITIALIZE_PARAMS *);
int uurb_vaapi_encode_bgra(struct uurb_encoder *, const void *, unsigned pitch,
                          uint64_t timestamp, int force_idr, struct uurb_packet *);
#endif
