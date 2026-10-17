/* Real AMD hardware acceptance: synthetic colors, headers, IDR, reconfigure,
 * invalid handles/timestamps, and repeatable cleanup. Decode independently. */
#include "native_vaapi_encoder.h"
#include "native_nvenc_reference_config.h"
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#define CHECK(x) do { if (!(x)) { fprintf(stderr, "FAIL line %d: %s\n", __LINE__, #x); return 1; } } while (0)
int main(int argc, char **argv)
{
    CHECK(argc == 2);
    unsigned width = 640, height = 360;
    unsigned char *pixels = malloc(width * height * 4);
    CHECK(pixels);
    for (int hevc = 0; hevc < 2; ++hevc) {
        char path[4096];
        snprintf(path, sizeof(path), "%s/%s.bin", argv[1], hevc ? "hevc" : "h264");
        FILE *file = fopen(path, "wb"); CHECK(file);
        NV_ENC_CONFIG preset = {.version = NV_ENC_CONFIG_VER}, config;
        NV_ENC_INITIALIZE_PARAMS init;
        uurb_nvenc_reference_config(&preset, width, height, hevc, 4000000, &config, &init);
        config.rcParams.enableAQ = 1;
        struct uurb_encoder *s = uurb_vaapi_open(NULL, &init); CHECK(s);
        unsigned char headers[65536]; uint32_t bytes = 0;
        CHECK(uurb_encoder_sequence(s, headers, sizeof(headers), &bytes) == NV_ENC_SUCCESS && bytes > 0);
        snprintf(path, sizeof(path), "%s/%s.headers", argv[1], hevc ? "hevc" : "h264");
        FILE *header_file = fopen(path, "wb"); CHECK(header_file);
        CHECK(fwrite(headers, 1, bytes, header_file) == bytes); fclose(header_file);
        for (unsigned i = 0; i < 64; ++i) {
            for (unsigned y = 0; y < height; ++y) for (unsigned x = 0; x < width; ++x) {
                unsigned char *p = pixels + (y * width + x) * 4;
                p[0] = x < width/2 ? 0 : 255; p[1] = 0; p[2] = x < width/2 ? 255 : 0; p[3] = 255;
            }
            if (i == 32) CHECK(!uurb_encoder_reconfigure(s, 2000000));
            struct uurb_packet packet = {0};
            CHECK(!uurb_vaapi_encode_bgra(s, pixels, width * 4, 1000 + i, i == 21, &packet));
            CHECK(packet.timestamp == 1000 + i && packet.size && packet.data);
            if (i == 0 || i == 21 || i == 32) CHECK(packet.idr);
            CHECK(uurb_encoder_reconfigure(s, 2000000));
            CHECK(fwrite(packet.data, 1, packet.size, file) == packet.size);
            CHECK(!uurb_encoder_release_packet(s));
            CHECK(uurb_encoder_release_packet(s));
            CHECK(uurb_vaapi_encode_bgra(s, pixels, width * 4, 1000 + i, 0, &packet));
        }
        CHECK(!uurb_encoder_close(s)); fclose(file);
        printf("%s: 64 hardware frames; forced IDR, headers, bitrate change, lifecycle OK\n", hevc ? "hevc" : "h264");
    }
    free(pixels);
    return 0;
}
