/* Internal SysV interface, NOT the Windows NVENC ABI. Single caller at a time.
 * Caller releases Vulkan ownership before open/encode/close and only reuses
 * the image after release_packet succeeds. Input pixels remain GPU-side. */
#ifndef UURB_CUDA_ENCODE_SESSION_H
#define UURB_CUDA_ENCODE_SESSION_H
#include <stdint.h>
struct uurb_encoder;
struct _NV_ENC_INITIALIZE_PARAMS;
struct _NV_ENC_RECONFIGURE_PARAMS;
struct uurb_packet {
    const void *data; /* valid until release_packet/close */
    uint32_t size;
    uint64_t timestamp;
    int idr;
    uint32_t average_qp;
    uint32_t picture_type;
};
/* Consumes fd on success AND failure. hevc is 0 or 1; bitrate is bits/sec. */
struct uurb_encoder *uurb_encoder_open(int fd, uint64_t bytes,
    const unsigned char uuid[16], unsigned width, unsigned height, int hevc, unsigned bitrate);
/* Internal only: caller validates the complete SDK contract first. Deep-copies
 * the config, initializes hardware before returning, and consumes fd always. */
struct uurb_encoder *uurb_encoder_open_config(int fd, uint64_t bytes,
    const unsigned char uuid[16], const struct _NV_ENC_INITIALIZE_PARAMS *);
/* Codec headers only; no input frame is needed. Returns an NVENCSTATUS integer.
 * Output bytes/count are committed only on success. Capacity <= 64 KiB. */
int uurb_encoder_sequence(struct uurb_encoder *, void *bytes, uint32_t capacity, uint32_t *size);
int uurb_encoder_encode(struct uurb_encoder *, uint64_t timestamp, int force_idr, struct uurb_packet *);
int uurb_encoder_release_packet(struct uurb_encoder *);
/* Bitrate only. Resize requires a separate texture/resource transaction. */
int uurb_encoder_reconfigure(struct uurb_encoder *, unsigned bitrate);
int uurb_encoder_reconfigure_config(struct uurb_encoder *, const struct _NV_ENC_RECONFIGURE_PARAMS *);
/* A cleanup failure requires terminating the owning isolated worker: device
 * loss may prevent safe release. Do not reuse the image or retry a freed handle. */
int uurb_encoder_close(struct uurb_encoder *);
#endif
