#version 450
layout(push_constant) uniform Geometry {
    vec2 origin;
    vec2 extent;
    vec2 screen;
} p;
layout(set=0, binding=0, std430) readonly buffer Cursor { uint bgra[]; } cursor;
layout(location=0) out vec4 color;
void main() {
    ivec2 pixel = ivec2(floor(gl_FragCoord.xy - p.origin));
    ivec2 size = ivec2(p.extent);
    if (any(lessThan(pixel, ivec2(0))) || any(greaterThanEqual(pixel, size))) discard;
    // Cursor metadata is premultiplied BGRA; the attachment converts RGBA.
    color = unpackUnorm4x8(cursor.bgra[pixel.y * size.x + pixel.x]).bgra;
}
