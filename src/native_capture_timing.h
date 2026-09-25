#ifndef UURB_CAPTURE_TIMING_H
#define UURB_CAPTURE_TIMING_H
#include <stdint.h>

/* Relative timestamps only: do not assume producer and consumer clocks share
 * an epoch. Invalid/discontinuous timestamps invalidate aggregate FPS. */
struct uurb_capture_timing {
    uint64_t first_ns, last_ns, maximum_gap_ns;
    unsigned samples, invalid, gaps[4];
};

static inline void uurb_capture_timing_add(struct uurb_capture_timing *s, int64_t timestamp)
{
    if (timestamp < 0 || (s->samples && (uint64_t)timestamp <= s->last_ns)) {
        s->invalid++;
        return;
    }
    if (!s->samples) s->first_ns = timestamp;
    else {
        uint64_t gap = timestamp - s->last_ns;
        if (gap > s->maximum_gap_ns) s->maximum_gap_ns = gap;
        s->gaps[gap < 12000000 ? 0 : gap < 24000000 ? 1 : gap < 40000000 ? 2 : 3]++;
    }
    s->last_ns = timestamp;
    s->samples++;
}

static inline double uurb_capture_timing_fps(const struct uurb_capture_timing *s)
{
    if (s->samples < 2 || s->invalid) return 0;
    return (s->samples - 1) * 1000000000.0 / (s->last_ns - s->first_ns);
}
#endif
