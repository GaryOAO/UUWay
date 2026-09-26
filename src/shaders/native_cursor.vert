#version 450
layout(push_constant) uniform Geometry {
    vec2 origin;
    vec2 extent;
    vec2 screen;
} p;
void main() {
    const vec2 corners[6] = vec2[6](vec2(0,0), vec2(1,0), vec2(0,1),
                                    vec2(0,1), vec2(1,0), vec2(1,1));
    gl_Position = vec4(2.0 * (p.origin + corners[gl_VertexIndex] * p.extent) / p.screen - 1.0, 0, 1);
}
