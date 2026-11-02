#ifndef UURB_VAAPI_AQ_H
#define UURB_VAAPI_AQ_H

/* Spatial AQ is optional. A VA-API encoder remains usable when its driver
 * cannot submit the 32 QP-delta ROI regions used by UUWay. */
static inline int uurb_vaapi_spatial_aq_enabled(int requested, unsigned width,
                                                 unsigned height, int roi_supported)
{
    return requested && width >= 128 && height >= 64 && roi_supported;
}

#endif
