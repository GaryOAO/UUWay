#ifndef UURB_SPATIAL_AQ_H
#define UURB_SPATIAL_AQ_H
#include <stdint.h>
/* Coarse spatial AQ: allocate more bits to low-variance areas, where
 * quantization artifacts are more visible. Unlike NVENC's proprietary AQ,
 * this uses 32 non-overlapping VA-API QP-delta regions. */
static inline void uurb_spatial_aq(const uint8_t *y, unsigned pitch,
    unsigned width, unsigned height, unsigned strength, int delta[32])
{
    uint64_t variance[32], mean = 0;
    for (unsigned i = 0; i < 32; ++i) {
        unsigned left = (width / 8 * (i % 8)) & ~15u;
        unsigned right = i % 8 == 7 ? width : (width / 8 * (i % 8 + 1)) & ~15u;
        unsigned top = (height / 4 * (i / 8)) & ~15u;
        unsigned bottom = i / 8 == 3 ? height : (height / 4 * (i / 8 + 1)) & ~15u;
        uint64_t sum = 0, square = 0, count = 0;
        for (unsigned row = top; row < bottom; row += 4)
            for (unsigned col = left; col < right; col += 4) {
                unsigned v = y[row * pitch + col]; sum += v; square += v * v; ++count;
            }
        variance[i] = count ? square / count - (sum / count) * (sum / count) : 0;
        mean += variance[i];
    }
    mean /= 32;
    unsigned amplitude = strength ? (strength + 2) / 3 : 3;
    for (unsigned i = 0; i < 32; ++i)
        delta[i] = (int)(((int64_t)variance[i] - (int64_t)mean) * amplitude * 2 / (int64_t)(mean + 256));
    for (unsigned i = 0; i < 32; ++i) {
        if (delta[i] > (int)amplitude) delta[i] = amplitude;
        if (delta[i] < -(int)amplitude) delta[i] = -(int)amplitude;
    }
}
#endif
