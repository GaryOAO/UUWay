#ifndef UURB_NATIVE_EIS_CONTROL_PROTOCOL_H
#define UURB_NATIVE_EIS_CONTROL_PROTOCOL_H
#include <stdint.h>
#define UURB_EIS_CONTROL_MAGIC 0x45525555u
#define UURB_EIS_CONTROL_SUSPEND 1u
#define UURB_EIS_CONTROL_RESUME 2u
/* Private same-UID seqpacket channel, not the UU input wire protocol.
 * SUSPEND: header only, no FD. RESUME: header + original mapping bytes and
 * exactly one newly authorized EIS stream FD. One command in flight, no replay.
 * ACK means local release/close or new EI readiness, NOT compositor delivery. */
struct uurb_eis_control_request {
    uint32_t magic, version, sequence, command, mapping_bytes, reserved[3];
};
struct uurb_eis_control_reply {
    uint32_t magic, version, sequence, command, error, logical_width, logical_height, reserved;
};
_Static_assert(sizeof(struct uurb_eis_control_request) == 32, "EIS control ABI");
_Static_assert(sizeof(struct uurb_eis_control_reply) == 32, "EIS control ACK ABI");
#endif
