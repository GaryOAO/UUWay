#define _GNU_SOURCE
#include "native_input_settings.h"
#include <json-c/json.h>
#include <sys/stat.h>
#include <unistd.h>
#include <fcntl.h>
#include <limits.h>
#include <errno.h>
#include <ctype.h>

int uurb_input_settings_read(const char *path, struct uurb_input_settings *output)
{
    if (!path || !output || path[0] != '/' || strlen(path) >= PATH_MAX) return -1;
    char parent[PATH_MAX]; strcpy(parent, path);
    char *slash = strrchr(parent, '/');
    if (slash == parent || !slash[1]) return -1;
    *slash = 0;
    struct stat info;
    if (lstat(parent, &info) || !S_ISDIR(info.st_mode) || info.st_uid != getuid() || info.st_mode & 077) return -1;
    int fd = open(path, O_RDONLY | O_NOFOLLOW | O_NONBLOCK | O_CLOEXEC);
    if (fd < 0) return errno == ENOENT ? 0 : -1;
    char packet[2049]; ssize_t size = -1;
    if (!fstat(fd, &info) && S_ISREG(info.st_mode) && info.st_uid == getuid() && !(info.st_mode & 077) &&
        info.st_size > 0 && info.st_size <= 2048) size = read(fd, packet, sizeof(packet));
    close(fd);
    if (size <= 0 || size > 2048 || size != info.st_size) return -1;
    struct json_tokener *parser = json_tokener_new();
    if (!parser) return -1;
    json_tokener_set_flags(parser, JSON_TOKENER_STRICT);
    struct json_object *value = json_tokener_parse_ex(parser, packet, (int)size);
    size_t consumed = json_tokener_get_parse_end(parser);
    int valid = json_tokener_get_error(parser) == json_tokener_success;
    json_tokener_free(parser);
    while (consumed < (size_t)size && isspace((unsigned char)packet[consumed])) ++consumed;
    if (!valid || consumed != (size_t)size || !value || json_object_get_type(value) != json_type_object ||
        json_object_object_length(value) != 4) { if (value) json_object_put(value); return -1; }
    const char *names[] = {"version", "relative_percent", "wheel_percent", "invert_wheel"};
    struct json_object *items[4];
    for (unsigned i = 0; i < 4; ++i) {
        if (!json_object_object_get_ex(value, names[i], &items[i]) ||
            json_object_get_type(items[i]) != (i == 3 ? json_type_boolean : json_type_int)) {
            json_object_put(value); return -1;
        }
    }
    int64_t version = json_object_get_int64(items[0]), relative = json_object_get_int64(items[1]), wheel = json_object_get_int64(items[2]);
    int invert = json_object_get_boolean(items[3]);
    json_object_put(value);
    if (version != 1 || relative < 25 || relative > 400 || wheel < 25 || wheel > 400) return -1;
    *output = (struct uurb_input_settings){(int)relative, (int)wheel, invert};
    return 1;
}
