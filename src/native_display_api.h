#ifndef UURB_NATIVE_DISPLAY_API_H
#define UURB_NATIVE_DISPLAY_API_H
#include <stdint.h>
#define UURB_DISPLAY_VERSION 1u
#define UURB_DISPLAY_SNAPSHOT_VERSION 2u
#define UURB_DISPLAY_MAX_MODES 256u
#define UURB_DISPLAY_SCALE_COUNT 12u
#define UURB_DISPLAY_SCALE_MASK ((1u << UURB_DISPLAY_SCALE_COUNT) - 1u)
static inline uint32_t uurb_display_scale_milli(unsigned index)
{
    static const uint32_t values[UURB_DISPLAY_SCALE_COUNT] =
        {1000,1250,1500,1750,2000,2250,2500,3000,3500,4000,4500,5000};
    return index < UURB_DISPLAY_SCALE_COUNT ? values[index] : 0;
}
static inline int uurb_display_scale_index(uint32_t milli)
{
    for (unsigned i = 0; i < UURB_DISPLAY_SCALE_COUNT; ++i)
        if (uurb_display_scale_milli(i) == milli) return (int)i;
    return -1;
}
struct uurb_display_mode { uint32_t width, height, refresh_millihz, scale_milli; };
struct uurb_display_snapshot {
    uint32_t version, serial, count, pending;
    struct uurb_display_mode current, modes[UURB_DISPLAY_MAX_MODES];
    /* Advertised logical scales for the current geometry. These are derived
     * from compositor-advertised values; they are not invented physical DPI. */
    uint32_t scale_min_milli, scale_max_milli;
    /* Exact representable scales; min/max alone cannot describe e.g. [1,2].
     * Bit i corresponds to uurb_display_scale_milli(i), never an interval. */
    uint32_t scale_mask;
};
struct uurb_display_request { uint32_t version, operation, serial, width, height, refresh_millihz, scale_milli, reserved; };
struct uurb_display_reply { uint32_t version, serial, changed, reserved; };
enum { UURB_DISPLAY_VERIFY = 1, UURB_DISPLAY_APPLY = 2,
       UURB_DISPLAY_APPLY_USER_DEFAULT = 3, UURB_DISPLAY_APPLY_GLOBAL_DEFAULT = 4 };
#define UURB_DISPLAY_FORCE_RESET 1u /* request.reserved, apply operations only */
enum { UURB_DISPLAY_OK, UURB_DISPLAY_INVALID, UURB_DISPLAY_TRANSPORT, UURB_DISPLAY_BAD_MODE, UURB_DISPLAY_FAILED,
       UURB_DISPLAY_DEFAULT_FAILED };
/* The unsized historical Query had both 4128- and 4136-byte consumers.
 * Retain its symbol only as a no-write failure; never guess caller capacity. */
int uurb_native_display_query(const char *, void *);
int uurb_native_display_query_v2(const char *, struct uurb_display_snapshot *, uint32_t);
int uurb_native_display_default(const char *, struct uurb_display_mode *);
int uurb_native_display_change(const char *, const struct uurb_display_request *, struct uurb_display_reply *);
#endif
