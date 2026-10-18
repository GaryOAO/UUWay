#define _GNU_SOURCE
#include "native_display_api.h"
#include <errno.h>
#include <json-c/json.h>
#include <math.h>
#include <poll.h>
#include <stdio.h>
#include <string.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <sys/un.h>
#include <time.h>
#include <unistd.h>
_Static_assert(sizeof(struct uurb_display_snapshot) == 4140, "display snapshot ABI v2");
_Static_assert(sizeof(struct uurb_display_request) == 32, "display request ABI");
_Static_assert(sizeof(struct uurb_display_reply) == 16, "display reply ABI");
static uint64_t clock_ms(void)
{
    struct timespec value;
    if (clock_gettime(CLOCK_MONOTONIC, &value)) return UINT64_MAX;
    return (uint64_t)value.tv_sec * 1000 + value.tv_nsec / 1000000;
}
static int ready(int fd, short events, uint64_t deadline)
{
    for (;;) {
        uint64_t now = clock_ms();
        if (now >= deadline) return 0;
        struct pollfd p = {.fd = fd, .events = events};
        int result = poll(&p, 1, (int)(deadline - now));
        if (result >= 0) return result && (p.revents & events);
        if (errno != EINTR) break;
    }
    return 0;
}
static int exchange(const char *path, const char *request, struct json_object **reply)
{
    struct sockaddr_un address = {.sun_family = AF_UNIX};
    if (!path || path[0] != '/' || strlen(path) >= sizeof(address.sun_path)) return UURB_DISPLAY_INVALID;
    char parent[sizeof(address.sun_path)]; strcpy(parent, path);
    char *slash = strrchr(parent, '/');
    if (!slash || slash == parent || !slash[1]) return UURB_DISPLAY_INVALID;
    *slash = 0;
    struct stat directory, endpoint;
    if (lstat(parent, &directory) || !S_ISDIR(directory.st_mode) || directory.st_uid != geteuid() || directory.st_mode & 077 ||
        lstat(path, &endpoint) || !S_ISSOCK(endpoint.st_mode) || endpoint.st_uid != geteuid() || endpoint.st_mode & 077)
        return UURB_DISPLAY_TRANSPORT;
    int fd = socket(AF_UNIX, SOCK_SEQPACKET | SOCK_NONBLOCK | SOCK_CLOEXEC, 0);
    if (fd < 0) return UURB_DISPLAY_TRANSPORT;
    int status = UURB_DISPLAY_TRANSPORT;
    strcpy(address.sun_path, path);
    if (connect(fd, (struct sockaddr *)&address, sizeof(address))) goto done;
    struct ucred peer; socklen_t length = sizeof(peer);
    if (getsockopt(fd, SOL_SOCKET, SO_PEERCRED, &peer, &length) || length != sizeof(peer) || peer.uid != geteuid()) goto done;
    uint64_t deadline = clock_ms() + 12000;
    size_t bytes = strlen(request);
    if (!ready(fd, POLLOUT, deadline) || send(fd, request, bytes, MSG_NOSIGNAL) != (ssize_t)bytes || !ready(fd, POLLIN, deadline)) goto done;
    char buffer[65537];
    ssize_t received = recv(fd, buffer, sizeof(buffer) - 1, MSG_TRUNC);
    if (received <= 0 || received > 65536) goto done;
    buffer[received] = 0;
    struct json_tokener *parser = json_tokener_new_ex(16);
    if (!parser) goto done;
    json_tokener_set_flags(parser, JSON_TOKENER_STRICT);
    struct json_object *object = json_tokener_parse_ex(parser, buffer, (int)received);
    int valid = json_tokener_get_error(parser) == json_tokener_success && json_tokener_get_parse_end(parser) == (size_t)received;
    json_tokener_free(parser);
    struct json_object *ok = NULL;
    if (!valid || !object || json_object_get_type(object) != json_type_object || !json_object_object_get_ex(object, "ok", &ok) ||
        json_object_get_type(ok) != json_type_boolean) {
        if (object) json_object_put(object);
        goto done;
    }
    if (!json_object_get_boolean(ok)) {
        struct json_object *error = NULL;
        status = UURB_DISPLAY_FAILED;
        if (json_object_object_get_ex(object, "error_type", &error) && json_object_get_type(error) == json_type_string &&
            !strcmp(json_object_get_string(error), "ValueError")) status = UURB_DISPLAY_BAD_MODE;
        if (error && json_object_get_type(error) == json_type_string &&
            !strcmp(json_object_get_string(error), "DisplayDefaultWriteError")) status = UURB_DISPLAY_DEFAULT_FAILED;
        json_object_put(object); goto done;
    }
    *reply = object; status = UURB_DISPLAY_OK;
done:
    close(fd);
    return status;
}
static int number(struct json_object *object, const char *name, double low, double high, double *value)
{
    struct json_object *item;
    if (!json_object_object_get_ex(object, name, &item) ||
        (json_object_get_type(item) != json_type_int && json_object_get_type(item) != json_type_double)) return 0;
    *value = json_object_get_double(item);
    return isfinite(*value) && *value >= low && *value <= high;
}
static int integer(struct json_object *object, const char *name, uint32_t *value)
{
    struct json_object *item;
    if (!json_object_object_get_ex(object, name, &item) || json_object_get_type(item) != json_type_int) return 0;
    int64_t v = json_object_get_int64(item);
    if (v < 0 || v > UINT32_MAX) return 0;
    *value = (uint32_t)v; return 1;
}
int uurb_native_virtual_control(const char *path, uint32_t code, const void *input, uint32_t bytes)
{
    if (bytes > 1024 || (bytes && !input) ||
        (code != 0 && code != UURB_VIRTUAL_TOPOLOGY_CONTROL &&
         (code < 0x30c004 || code > 0x30c014 || (code & 3))))
        return UURB_DISPLAY_INVALID;
    struct json_object *request = json_object_new_object();
    struct json_object *payload = json_object_new_array();
    if (!request || !payload) {
        if (request) json_object_put(request);
        if (payload) json_object_put(payload);
        return UURB_DISPLAY_FAILED;
    }
    json_object_object_add(request, "version", json_object_new_int(1));
    json_object_object_add(request, "op", json_object_new_string("virtual-control"));
    json_object_object_add(request, "code", json_object_new_int64(code));
    for (uint32_t i = 0; i < bytes; ++i)
        json_object_array_add(payload, json_object_new_int(((const unsigned char *)input)[i]));
    json_object_object_add(request, "payload", payload);
    struct json_object *reply = NULL;
    int status = exchange(path, json_object_to_json_string_ext(request, JSON_C_TO_STRING_PLAIN), &reply);
    json_object_put(request);
    if (reply) json_object_put(reply);
    return status;
}
static int mode(struct json_object *object, struct uurb_display_mode *value, int current)
{
    double refresh, scale = 0;
    if (!object || json_object_get_type(object) != json_type_object ||
        !integer(object, "width", &value->width) || !integer(object, "height", &value->height) ||
        value->width < 2 || value->height < 2 || value->width > 4096 || value->height > 4096 ||
        value->width % 2 || value->height % 2 || !number(object, "refresh", 1, UURB_DISPLAY_MAX_REFRESH_HZ, &refresh) ||
        (current && !number(object, "scale", 0.5, 4, &scale))) return 0;
    value->refresh_millihz = (uint32_t)(refresh * 1000 + 0.5);
    value->scale_milli = (uint32_t)(scale * 1000 + 0.5);
    return 1;
}
static int mode_scales(struct json_object *object, uint32_t *minimum, uint32_t *maximum, uint32_t *mask)
{
    struct json_object *scales;
    if (!object || !minimum || !maximum || !mask ||
        !json_object_object_get_ex(object, "scales", &scales) ||
        json_object_get_type(scales) != json_type_array ||
        !json_object_array_length(scales) || json_object_array_length(scales) > 64) return 0;
    uint32_t low = UINT32_MAX, high = 0, supported = 0;
    for (size_t i = 0; i < json_object_array_length(scales); ++i) {
        struct json_object *item = json_object_array_get_idx(scales, i);
        if (!item || (json_object_get_type(item) != json_type_int && json_object_get_type(item) != json_type_double)) return 0;
        double value = json_object_get_double(item);
        if (!isfinite(value) || value < 0.5 || value > 4.0) return 0;
        uint32_t milli = (uint32_t)(value * 1000.0 + 0.5);
        int index = uurb_display_scale_index(milli);
        /* Do not turn a nearby, unsupported fractional scale into a Windows
         * percentage merely by rounding it to a milliscale. */
        if (index >= 0 && fabs(value - milli / 1000.0) < 0.00001) supported |= 1u << index;
        if (milli < low) low = milli;
        if (milli > high) high = milli;
    }
    if (!low || high < low) return 0;
    *minimum = low; *maximum = high; *mask = supported; return 1;
}
int uurb_native_display_query(const char *path, void *output)
{
    (void)path; (void)output;
    return UURB_DISPLAY_INVALID;
}
int uurb_native_display_query_v2(const char *path, struct uurb_display_snapshot *output, uint32_t output_bytes)
{
    if (!output || output_bytes != sizeof(*output)) return UURB_DISPLAY_INVALID;
    struct json_object *reply = NULL;
    int status = exchange(path, "{\"version\":1,\"op\":\"inspect\"}", &reply);
    if (status) return status;
    status = UURB_DISPLAY_FAILED;
    struct uurb_display_snapshot temporary = {.version = UURB_DISPLAY_SNAPSHOT_VERSION};
    struct json_object *modes, *pending;
    if (!integer(reply, "serial", &temporary.serial) || !mode(reply, &temporary.current, 1) ||
        !json_object_object_get_ex(reply, "pending", &pending) || json_object_get_type(pending) != json_type_boolean ||
        !json_object_object_get_ex(reply, "modes", &modes) || json_object_get_type(modes) != json_type_array) goto done;
    size_t count = json_object_array_length(modes);
    if (!count || count > UURB_DISPLAY_MAX_MODES) goto done;
    temporary.count = (uint32_t)count; temporary.pending = json_object_get_boolean(pending);
    int found_current = 0;
    for (size_t i = 0; i < count; ++i) {
        struct json_object *item = json_object_array_get_idx(modes, i);
        if (!mode(item, &temporary.modes[i], 0)) goto done;
        if (temporary.modes[i].width == temporary.current.width &&
            temporary.modes[i].height == temporary.current.height &&
            temporary.modes[i].refresh_millihz == temporary.current.refresh_millihz) {
            uint32_t low, high, mask;
            if (!mode_scales(item, &low, &high, &mask)) goto done;
            /* The wire omits native mode IDs. Equal rounded geometry/rate
             * must not let a different scale list win by enumeration order. */
            if (found_current && (low != temporary.scale_min_milli || high != temporary.scale_max_milli ||
                                  mask != temporary.scale_mask)) goto done;
            temporary.scale_min_milli = low;
            temporary.scale_max_milli = high;
            temporary.scale_mask = mask;
            found_current = 1;
        }
    }
    if (!found_current || temporary.current.scale_milli < temporary.scale_min_milli ||
        temporary.current.scale_milli > temporary.scale_max_milli) goto done;
    *output = temporary; status = UURB_DISPLAY_OK;
done:
    json_object_put(reply); return status;
}
static int signed_integer(struct json_object *object, const char *name, int32_t *value)
{
    struct json_object *item;
    if (!object || !value || !json_object_object_get_ex(object, name, &item) ||
        json_object_get_type(item) != json_type_int) return 0;
    int64_t number = json_object_get_int64(item);
    if (number < INT32_MIN || number > INT32_MAX) return 0;
    *value = (int32_t)number;
    return 1;
}

static int boolean_field(struct json_object *object, const char *name, int *value)
{
    struct json_object *item;
    if (!object || !value || !json_object_object_get_ex(object, name, &item) ||
        json_object_get_type(item) != json_type_boolean) return 0;
    *value = json_object_get_boolean(item);
    return 1;
}

static int string_field(struct json_object *object, const char *name, char *buffer, size_t size)
{
    struct json_object *item;
    if (!object || !buffer || size < 2 || !json_object_object_get_ex(object, name, &item) ||
        json_object_get_type(item) != json_type_string) return 0;
    const char *value = json_object_get_string(item);
    size_t length = (size_t)json_object_get_string_len(item);
    if (!value || !length || length >= size) return 0;
    for (size_t i = 0; i < length; ++i)
        if ((unsigned char)value[i] < 0x20) return 0;
    memcpy(buffer, value, length); buffer[length] = 0;
    return 1;
}

int uurb_native_display_topology(const char *path, struct uurb_display_topology *output, uint32_t output_bytes)
{
    if (!output || output_bytes != sizeof(*output)) return UURB_DISPLAY_INVALID;
    struct json_object *reply = NULL;
    int status = exchange(path, "{\"version\":1,\"op\":\"inspect\"}", &reply);
    if (status) return status;
    status = UURB_DISPLAY_FAILED;
    struct uurb_display_topology temporary = {.version = UURB_DISPLAY_TOPOLOGY_VERSION};
    struct json_object *monitors = NULL;
    if (!integer(reply, "serial", &temporary.serial) ||
        !json_object_object_get_ex(reply, "monitors", &monitors) ||
        json_object_get_type(monitors) != json_type_array) goto done;
    size_t count = json_object_array_length(monitors);
    if (!count || count > UURB_DISPLAY_MAX_MONITORS) goto done;
    temporary.monitor_count = (uint32_t)count;
    uint32_t mode_total = 0, primary_count = 0;
    for (size_t i = 0; i < count; ++i) {
        struct json_object *item = json_object_array_get_idx(monitors, i);
        if (!item || json_object_get_type(item) != json_type_object) goto done;
        uint32_t index, width, height;
        int32_t x, y;
        double refresh, scale;
        int primary = 0, virtual = 0;
        if (!integer(item, "index", &index) || index != i ||
            !signed_integer(item, "x", &x) || !signed_integer(item, "y", &y) ||
            x < -32768 || x > 32767 || y < -32768 || y > 32767 ||
            !integer(item, "width", &width) || !integer(item, "height", &height) ||
            width < 2 || height < 2 || width > 4096 || height > 4096 || width % 2 || height % 2 ||
            !number(item, "refresh", 1, UURB_DISPLAY_MAX_REFRESH_HZ, &refresh) ||
            !number(item, "scale", 0.5, 4, &scale) ||
            !boolean_field(item, "primary", &primary) || !boolean_field(item, "virtual", &virtual)) goto done;
        struct uurb_display_monitor *monitor = &temporary.monitors[i];
        if (!string_field(item, "gdi_name", monitor->gdi_name, sizeof(monitor->gdi_name)) ||
            strncmp(monitor->gdi_name, "\\\\.\\DISPLAY", 10) ||
            !string_field(item, "friendly", monitor->friendly, sizeof(monitor->friendly)) ||
            !string_field(item, "device_path", monitor->device_path, sizeof(monitor->device_path))) goto done;
        for (size_t previous = 0; previous < i; ++previous)
            if (!strcmp(temporary.monitors[previous].gdi_name, monitor->gdi_name)) goto done;
        monitor->index = index;
        monitor->flags = (primary ? UURB_DISPLAY_MONITOR_PRIMARY : 0) |
                         (virtual ? UURB_DISPLAY_MONITOR_VIRTUAL : 0);
        monitor->x = x; monitor->y = y; monitor->width = width; monitor->height = height;
        monitor->refresh_millihz = (uint32_t)(refresh * 1000 + 0.5);
        monitor->scale_milli = (uint32_t)(scale * 1000 + 0.5);
        monitor->mode_offset = mode_total;
        struct json_object *modes = NULL;
        if (!json_object_object_get_ex(item, "modes", &modes) ||
            json_object_get_type(modes) != json_type_array) goto done;
        size_t mode_count = json_object_array_length(modes);
        if (!mode_count || mode_count > UURB_DISPLAY_MAX_MONITOR_MODES ||
            mode_total + mode_count > UURB_DISPLAY_MAX_MONITORS * UURB_DISPLAY_MAX_MONITOR_MODES) goto done;
        monitor->mode_count = (uint32_t)mode_count;
        int found_current = 0;
        for (size_t j = 0; j < mode_count; ++j) {
            struct json_object *mode_item = json_object_array_get_idx(modes, j);
            struct uurb_display_mode *destination = &temporary.modes[mode_total];
            if (!mode(mode_item, destination, 0)) goto done;
            if (destination->width == width && destination->height == height &&
                destination->refresh_millihz == monitor->refresh_millihz) {
                uint32_t low, high, mask;
                if (!mode_scales(mode_item, &low, &high, &mask) ||
                    (found_current && (low != monitor->scale_min_milli ||
                                       high != monitor->scale_max_milli || mask != monitor->scale_mask)))
                    goto done;
                monitor->scale_min_milli = low; monitor->scale_max_milli = high; monitor->scale_mask = mask;
                found_current = 1;
            }
            ++mode_total;
        }
        if (!found_current || monitor->scale_milli < monitor->scale_min_milli ||
            monitor->scale_milli > monitor->scale_max_milli ||
            (monitor->scale_mask & ~UURB_DISPLAY_SCALE_MASK)) goto done;
        primary_count += primary != 0;
    }
    if (primary_count != 1 || mode_total > UURB_DISPLAY_MAX_MONITORS * UURB_DISPLAY_MAX_MONITOR_MODES) goto done;
    temporary.mode_count = mode_total;
    *output = temporary; status = UURB_DISPLAY_OK;
done:
    json_object_put(reply); return status;
}

int uurb_native_display_default(const char *path, struct uurb_display_mode *output)
{
    if (!output) return UURB_DISPLAY_INVALID;
    struct json_object *reply = NULL, *registry = NULL;
    int status = exchange(path, "{\"version\":1,\"op\":\"inspect\"}", &reply);
    if (status) return status;
    struct uurb_display_mode temporary;
    if (!json_object_object_get_ex(reply, "registry", &registry) || !mode(registry, &temporary, 1)) status = UURB_DISPLAY_FAILED;
    else *output = temporary;
    json_object_put(reply); return status;
}
int uurb_native_display_change(const char *path, const struct uurb_display_request *request, struct uurb_display_reply *output)
{
    if (!request || !output || request->version != 1 ||
        (request->reserved & ~(UURB_DISPLAY_FORCE_RESET | UURB_DISPLAY_OUTPUT_SELECTED | UURB_DISPLAY_OUTPUT_MASK)) ||
        ((request->reserved & UURB_DISPLAY_OUTPUT_MASK) && !(request->reserved & UURB_DISPLAY_OUTPUT_SELECTED)) ||
        request->operation < UURB_DISPLAY_VERIFY || request->operation > UURB_DISPLAY_APPLY_GLOBAL_DEFAULT ||
        (request->operation == UURB_DISPLAY_VERIFY && (request->reserved & UURB_DISPLAY_FORCE_RESET)) ||
        request->width < 2 || request->height < 2 || request->width > 4096 || request->height > 4096 ||
        request->width % 2 || request->height % 2 || request->refresh_millihz > UURB_DISPLAY_MAX_REFRESH_MILLIHZ ||
        (request->scale_milli && (request->scale_milli < 500 || request->scale_milli > 4000))) return UURB_DISPLAY_INVALID;
    char packet[384], scale[48] = "", selected[32] = "";
    if (request->reserved & UURB_DISPLAY_OUTPUT_SELECTED)
        snprintf(selected, sizeof(selected), ",\"output\":%u", (request->reserved & UURB_DISPLAY_OUTPUT_MASK) >> UURB_DISPLAY_OUTPUT_SHIFT);
    if (request->scale_milli) snprintf(scale, sizeof(scale), ",\"scale\":%u.%03u", request->scale_milli / 1000, request->scale_milli % 1000);
    snprintf(packet, sizeof(packet), "{\"version\":1,\"op\":\"%s\",\"serial\":%u,\"width\":%u,\"height\":%u,\"refresh\":%u.%03u%s%s%s%s%s}",
        request->operation == UURB_DISPLAY_VERIFY ? "verify" : "apply", request->serial, request->width, request->height,
        request->refresh_millihz / 1000, request->refresh_millihz % 1000, scale,
        request->operation != UURB_DISPLAY_VERIFY ? ",\"confirmation\":\"gpu_frame\"" : "",
        request->operation == UURB_DISPLAY_APPLY_USER_DEFAULT ? ",\"default_scope\":\"user\"" :
        request->operation == UURB_DISPLAY_APPLY_GLOBAL_DEFAULT ? ",\"default_scope\":\"global\"" : "",
        request->reserved & UURB_DISPLAY_FORCE_RESET ? ",\"force_reset\":true" : "", selected);
    struct json_object *reply = NULL;
    int status = exchange(path, packet, &reply);
    if (status) return status;
    struct uurb_display_reply temporary = {.version = 1, .serial = request->serial};
    if (request->operation != UURB_DISPLAY_VERIFY) {
        struct json_object *changed;
        if (!integer(reply, "serial", &temporary.serial) || !json_object_object_get_ex(reply, "changed", &changed) ||
            json_object_get_type(changed) != json_type_boolean) status = UURB_DISPLAY_FAILED;
        else temporary.changed = json_object_get_boolean(changed);
    } else {
        struct json_object *verified;
        if (!json_object_object_get_ex(reply, "verified", &verified) || json_object_get_type(verified) != json_type_boolean ||
            !json_object_get_boolean(verified)) status = UURB_DISPLAY_FAILED;
    }
    if (!status) *output = temporary;
    json_object_put(reply); return status;
}
