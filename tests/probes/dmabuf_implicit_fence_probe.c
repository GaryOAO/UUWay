/* Does a GL-rendered GBM dma-buf carry an implicit fence for work still in flight?
 *
 * Mirrors Mutter's screen-cast path (GBM bo -> EGLImage -> FBO, glFlush only) and the UUWay
 * consumer (DMA_BUF_IOCTL_EXPORT_SYNC_FILE), with a second process reading the buffer. A heavy
 * draw is queued first so the GPU is busy, like a background job would keep it.
 *
 *   gcc -O1 -o dmabuf_implicit_fence_probe tests/probes/dmabuf_implicit_fence_probe.c \
 *       $(pkg-config --cflags --libs gbm egl glesv2 libdrm)
 *   ./dmabuf_implicit_fence_probe [shader_iterations=4000] [draws=8] [trials=6] [finish=0|1]
 *   ./dmabuf_implicit_fence_probe burn [shader_iterations=12000] [draws=8]   # saturate the GPU until killed
 *
 * finish=0 is what Mutter 46 does: on NVIDIA the fence already reads "signaled" and the reader
 * sees the previous (stale) contents. finish=1 waits for the GPU first and the reader always sees
 * the new contents; the "fence signaled right after" figure is only meaningful with finish=0.
 * Needs an NVIDIA render node; touches no desktop session. */
#define _GNU_SOURCE
#include <EGL/egl.h>
#include <EGL/eglext.h>
#include <GLES3/gl3.h>
#include <GLES2/gl2ext.h>
#include <gbm.h>
#include <drm_fourcc.h>
#include <linux/dma-buf.h>
#include <sys/ioctl.h>
#include <sys/socket.h>
#include <sys/wait.h>
#include <poll.h>
#include <fcntl.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <unistd.h>
#include <dirent.h>

#define W 1920
#define H 1080
static double now_ms(void){ struct timespec t; clock_gettime(CLOCK_MONOTONIC,&t); return t.tv_sec*1e3+t.tv_nsec/1e6; }
#define DIE(...) do{ fprintf(stderr,__VA_ARGS__); fputc('\n',stderr); exit(2);}while(0)

struct gl { int fd; struct gbm_device *gbm; EGLDisplay dpy; EGLContext ctx; };
static int find_nvidia_node(char *out, size_t n){
    for (int i = 128; i < 140; i++) {
        char link[256], target[256];
        snprintf(link, sizeof link, "/sys/class/drm/renderD%d/device/driver", i);
        ssize_t r = readlink(link, target, sizeof target - 1);
        if (r > 0) { target[r] = 0; if (strstr(target, "nvidia")) { snprintf(out, n, "/dev/dri/renderD%d", i); return 0; } }
    }
    return 1;
}
static void gl_open(struct gl *g){
    char node[64]; if (find_nvidia_node(node, sizeof node)) DIE("no nvidia render node");
    g->fd = open(node, O_RDWR | O_CLOEXEC); if (g->fd < 0) DIE("open %s", node);
    g->gbm = gbm_create_device(g->fd); if (!g->gbm) DIE("gbm_create_device");
    PFNEGLGETPLATFORMDISPLAYEXTPROC get = (void *)eglGetProcAddress("eglGetPlatformDisplayEXT");
    g->dpy = get(EGL_PLATFORM_GBM_KHR, g->gbm, NULL); if (g->dpy == EGL_NO_DISPLAY) DIE("eglGetPlatformDisplay");
    EGLint maj, min; if (!eglInitialize(g->dpy, &maj, &min)) DIE("eglInitialize");
    eglBindAPI(EGL_OPENGL_ES_API);
    EGLint attr[] = {EGL_CONTEXT_CLIENT_VERSION, 3, EGL_NONE};
    g->ctx = eglCreateContext(g->dpy, EGL_NO_CONFIG_KHR, EGL_NO_CONTEXT, attr);
    if (g->ctx == EGL_NO_CONTEXT) DIE("eglCreateContext 0x%x", eglGetError());
    if (!eglMakeCurrent(g->dpy, EGL_NO_SURFACE, EGL_NO_SURFACE, g->ctx)) DIE("eglMakeCurrent 0x%x", eglGetError());
}
struct buf { int fd; uint32_t stride, offset; uint64_t modifier; };
static GLuint import_fbo(struct gl *g, struct buf *b, GLuint *tex_out){
    EGLint a[] = {EGL_WIDTH, W, EGL_HEIGHT, H, EGL_LINUX_DRM_FOURCC_EXT, DRM_FORMAT_ARGB8888,
        EGL_DMA_BUF_PLANE0_FD_EXT, b->fd, EGL_DMA_BUF_PLANE0_OFFSET_EXT, (EGLint)b->offset,
        EGL_DMA_BUF_PLANE0_PITCH_EXT, (EGLint)b->stride,
        EGL_DMA_BUF_PLANE0_MODIFIER_LO_EXT, (EGLint)(b->modifier & 0xffffffff),
        EGL_DMA_BUF_PLANE0_MODIFIER_HI_EXT, (EGLint)(b->modifier >> 32), EGL_NONE};
    PFNEGLCREATEIMAGEKHRPROC create = (void *)eglGetProcAddress("eglCreateImageKHR");
    PFNGLEGLIMAGETARGETTEXTURE2DOESPROC target = (void *)eglGetProcAddress("glEGLImageTargetTexture2DOES");
    EGLImageKHR img = create(g->dpy, EGL_NO_CONTEXT, EGL_LINUX_DMA_BUF_EXT, NULL, a);
    if (img == EGL_NO_IMAGE_KHR) DIE("eglCreateImage 0x%x", eglGetError());
    GLuint tex, fbo; glGenTextures(1, &tex); glBindTexture(GL_TEXTURE_2D, tex); target(GL_TEXTURE_2D, img);
    glGenFramebuffers(1, &fbo); glBindFramebuffer(GL_FRAMEBUFFER, fbo);
    glFramebufferTexture2D(GL_FRAMEBUFFER, GL_COLOR_ATTACHMENT0, GL_TEXTURE_2D, tex, 0);
    if (glCheckFramebufferStatus(GL_FRAMEBUFFER) != GL_FRAMEBUFFER_COMPLETE) DIE("fbo incomplete");
    if (tex_out) *tex_out = tex;
    return fbo;
}
static void send_fd(int s, int fd, const void *data, size_t n){
    struct iovec io = {(void *)data, n}; char c[CMSG_SPACE(sizeof(int))] = {0};
    struct msghdr m = {.msg_iov = &io, .msg_iovlen = 1, .msg_control = c, .msg_controllen = sizeof c};
    struct cmsghdr *h = CMSG_FIRSTHDR(&m); h->cmsg_level = SOL_SOCKET; h->cmsg_type = SCM_RIGHTS;
    h->cmsg_len = CMSG_LEN(sizeof(int)); memcpy(CMSG_DATA(h), &fd, sizeof fd);
    if (sendmsg(s, &m, 0) < 0) DIE("sendmsg");
}
static int recv_fd(int s, void *data, size_t n){
    struct iovec io = {data, n}; char c[CMSG_SPACE(sizeof(int))];
    struct msghdr m = {.msg_iov = &io, .msg_iovlen = 1, .msg_control = c, .msg_controllen = sizeof c};
    if (recvmsg(s, &m, 0) < 0) DIE("recvmsg");
    struct cmsghdr *h = CMSG_FIRSTHDR(&m); int fd; memcpy(&fd, CMSG_DATA(h), sizeof fd); return fd;
}
static const char *fs_heavy =
 "#version 300 es\nprecision highp float; uniform int n; out vec4 o;\n"
 "void main(){ float x = gl_FragCoord.x * 0.001; for (int i = 0; i < n; i++) x = sin(x) * cos(x + 1.0) + x * 0.999; o = vec4(x, 0.0, 0.0, 1.0); }\n";
static const char *vs =
 "#version 300 es\nvoid main(){ vec2 p = vec2((gl_VertexID << 1) & 2, gl_VertexID & 2); gl_Position = vec4(p * 2.0 - 1.0, 0.0, 1.0); }\n";
static GLuint prog_heavy(void){
    GLuint v = glCreateShader(GL_VERTEX_SHADER), f = glCreateShader(GL_FRAGMENT_SHADER), p = glCreateProgram();
    glShaderSource(v, 1, &vs, NULL); glCompileShader(v); glShaderSource(f, 1, &fs_heavy, NULL); glCompileShader(f);
    GLint ok; glGetShaderiv(f, GL_COMPILE_STATUS, &ok); if (!ok) DIE("shader compile");
    glAttachShader(p, v); glAttachShader(p, f); glLinkProgram(p); glGetProgramiv(p, GL_LINK_STATUS, &ok); if (!ok) DIE("link");
    return p;
}
static void fill(GLuint fbo, float r, float gr, float b){
    glBindFramebuffer(GL_FRAMEBUFFER, fbo); glViewport(0, 0, W, H);
    glClearColor(r, gr, b, 1); glClear(GL_COLOR_BUFFER_BIT);
}
static void queue_heavy(GLuint prog, GLuint scratch_fbo, int iters, int draws){
    glBindFramebuffer(GL_FRAMEBUFFER, scratch_fbo); glViewport(0, 0, W, H); glUseProgram(prog);
    glUniform1i(glGetUniformLocation(prog, "n"), iters);
    for (int i = 0; i < draws; i++) glDrawArrays(GL_TRIANGLES, 0, 3);
}
static void child_main(int s){
    /* Consumer stand-in: separate process, own GL context, imports the same dma-buf and reads one pixel. */
    struct buf b; int fd = recv_fd(s, &b, sizeof b); b.fd = fd;
    struct gl g; gl_open(&g);
    GLuint fbo = import_fbo(&g, &b, NULL);
    char ready = 1; if (write(s, &ready, 1) != 1) _exit(3);
    for (;;) {
        char cmd; if (read(s, &cmd, 1) != 1 || cmd == 'q') _exit(0);
        double t0 = now_ms(); unsigned char px[4] = {0};
        glBindFramebuffer(GL_FRAMEBUFFER, fbo);
        glReadPixels(W / 2, H / 2, 1, 1, GL_RGBA, GL_UNSIGNED_BYTE, px);   /* blocks until the read is executed */
        double t1 = now_ms();
        struct { unsigned char px[4]; double ms; } r; memcpy(r.px, px, 4); r.ms = t1 - t0;
        if (write(s, &r, sizeof r) != sizeof r) _exit(3);
    }
}
static void burn(int iters, int draws){
    struct gl g; gl_open(&g);
    GLuint tex, fbo; glGenTextures(1, &tex); glBindTexture(GL_TEXTURE_2D, tex);
    glTexImage2D(GL_TEXTURE_2D, 0, GL_RGBA8, W, H, 0, GL_RGBA, GL_UNSIGNED_BYTE, NULL);
    glGenFramebuffers(1, &fbo); glBindFramebuffer(GL_FRAMEBUFFER, fbo);
    glFramebufferTexture2D(GL_FRAMEBUFFER, GL_COLOR_ATTACHMENT0, GL_TEXTURE_2D, tex, 0);
    GLuint prog = prog_heavy();
    for (;;) { queue_heavy(prog, fbo, iters, draws); glFinish(); }
}
int main(int argc, char **argv){
    if (argc > 1 && !strcmp(argv[1], "burn")) burn(argc > 2 ? atoi(argv[2]) : 12000, argc > 3 ? atoi(argv[3]) : 8);
    int iters = argc > 1 ? atoi(argv[1]) : 4000, draws = argc > 2 ? atoi(argv[2]) : 8, trials = argc > 3 ? atoi(argv[3]) : 6, finish = argc > 4 ? atoi(argv[4]) : 0;
    int sp[2]; if (socketpair(AF_UNIX, SOCK_STREAM, 0, sp)) DIE("socketpair");
    pid_t pid = fork(); if (!pid) { close(sp[0]); child_main(sp[1]); }
    close(sp[1]);
    struct gl g; gl_open(&g);
    struct gbm_bo *bo = gbm_bo_create(g.gbm, W, H, GBM_FORMAT_ARGB8888, GBM_BO_USE_RENDERING);
    if (!bo) DIE("gbm_bo_create");
    struct buf b = {.fd = gbm_bo_get_fd(bo), .stride = gbm_bo_get_stride(bo), .offset = gbm_bo_get_offset(bo, 0),
                    .modifier = gbm_bo_get_modifier(bo)};
    printf("driver node ok, gbm bo %dx%d stride=%u modifier=0x%llx\n", W, H, b.stride, (unsigned long long)b.modifier);
    GLuint dma_fbo = import_fbo(&g, &b, NULL);
    GLuint scratch_tex, scratch_fbo; glGenTextures(1, &scratch_tex); glBindTexture(GL_TEXTURE_2D, scratch_tex);
    glTexImage2D(GL_TEXTURE_2D, 0, GL_RGBA8, W, H, 0, GL_RGBA, GL_UNSIGNED_BYTE, NULL);
    glGenFramebuffers(1, &scratch_fbo); glBindFramebuffer(GL_FRAMEBUFFER, scratch_fbo);
    glFramebufferTexture2D(GL_FRAMEBUFFER, GL_COLOR_ATTACHMENT0, GL_TEXTURE_2D, scratch_tex, 0);
    GLuint prog = prog_heavy();
    send_fd(sp[0], b.fd, &b, sizeof b);
    char ready; if (read(sp[0], &ready, 1) != 1) DIE("child init failed");
    queue_heavy(prog, scratch_fbo, iters, draws); glFinish();  /* warm up + calibrate */
    double c0 = now_ms(); queue_heavy(prog, scratch_fbo, iters, draws); glFinish();
    printf("calibration: heavy work = %.1f ms of GPU time (iters=%d draws=%d)\n", now_ms() - c0, iters, draws);
    int fence_early = 0, stale = 0;
    for (int t = 0; t < trials; t++) {
        fill(dma_fbo, 1, 0, 0); glFinish();                          /* old content: RED  */
        queue_heavy(prog, scratch_fbo, iters, draws);                 /* GPU is busy (like the OCR job)       */
        fill(dma_fbo, 0, 1, 0);                                       /* new frame: GREEN, queued behind it   */
        double t0 = now_ms(); if (finish) glFinish(); else glFlush(); /* flush == cogl_framebuffer_flush      */
        struct dma_buf_export_sync_file sync = {.flags = DMA_BUF_SYNC_READ, .fd = -1};
        if (ioctl(b.fd, DMA_BUF_IOCTL_EXPORT_SYNC_FILE, &sync) < 0) DIE("EXPORT_SYNC_FILE");
        struct pollfd p = {.fd = sync.fd, .events = POLLIN};
        int signaled_now = poll(&p, 1, 0) > 0;
        if (write(sp[0], "r", 1) != 1) DIE("write");                  /* tell the consumer to read right now  */
        struct { unsigned char px[4]; double ms; } r; if (read(sp[0], &r, sizeof r) != sizeof r) DIE("child died");
        double t_read = now_ms() - t0;
        glFinish(); double t_gpu = now_ms() - t0;
        struct pollfd p2 = {.fd = sync.fd, .events = POLLIN};
        int signaled_after = poll(&p2, 1, 0) > 0; close(sync.fd);
        const char *what = r.px[1] > 200 && r.px[0] < 50 ? "NEW(green)" : r.px[0] > 200 && r.px[1] < 50 ? "STALE(red)" : "other";
        printf("trial %d: fence signaled right after glFlush=%s | consumer read returned after %.1f ms -> %s | "
               "GPU actually finished after %.1f ms | fence signaled after glFinish=%s\n",
               t, signaled_now ? "YES" : "no", t_read, what, t_gpu, signaled_after ? "yes" : "NO");
        fence_early += signaled_now && t_gpu > 20; stale += !strcmp(what, "STALE(red)");
    }
    if (write(sp[0], "q", 1) != 1) DIE("write q");
    waitpid(pid, NULL, 0);
    printf("mode=%s\n", finish ? "producer glFinish before notify" : "producer glFlush only (current Mutter)");
    printf("RESULT: fence reported complete while GPU work still pending in %d/%d trials; consumer read stale pixels in %d/%d\n",
           fence_early, trials, stale, trials);
    return 0;
}
