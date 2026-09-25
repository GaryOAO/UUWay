/* Included after shared GTK helpers. The GTK thread never performs display RPC. */
#include <math.h>
typedef struct { uint32_t width, height; double refresh; uint32_t scale_count; double scales[64]; } DisplayMode;
typedef struct {
    uint32_t serial, width, height, pending, count;
    double refresh, scale;
    DisplayMode modes[256];
} DisplaySnapshot;
typedef struct { uint32_t changed, serial, seconds; unsigned char transaction[33]; } DisplayReply;
_Static_assert(sizeof(DisplayMode) == 536, "Rust display mode ABI");
_Static_assert(sizeof(DisplaySnapshot) == 137256, "Rust display snapshot ABI");
_Static_assert(sizeof(DisplayReply) == 48, "Rust display reply ABI");
extern int uurb_display_inspect(DisplaySnapshot *);
extern int uurb_display_apply(uint32_t, uint32_t, uint32_t, double, double, DisplayReply *);
extern int uurb_display_finish(uint32_t, uint32_t, const unsigned char *);
static GtkWidget *display_modes, *display_scales, *display_message, *display_current;
static GtkWidget *display_apply, *display_refresh, *display_keep, *display_revert;
static DisplaySnapshot *display_snapshot;
static DisplayReply display_owned;
static gboolean display_busy, display_valid, display_has_transaction;
static gint64 display_deadline;
typedef struct {
    unsigned operation; /* 0 inspect, 1 apply, 2 keep, 3 revert */
    int failed;
    uint32_t serial;
    DisplayMode mode;
    double scale;
    DisplayReply reply;
    DisplaySnapshot *snapshot;
    gint64 started;
} DisplayJob;

static void display_sensitivity(void)
{
    gboolean editable = display_valid && !display_busy && !display_snapshot->pending && !display_has_transaction;
    gtk_widget_set_sensitive(display_modes, editable);
    gtk_widget_set_sensitive(display_scales, editable);
    gtk_widget_set_sensitive(display_apply, editable);
    gtk_widget_set_sensitive(display_refresh, !display_busy);
    gboolean confirmable = display_has_transaction && !display_busy && g_get_monotonic_time() < display_deadline;
    gtk_widget_set_sensitive(display_keep, confirmable);
    gtk_widget_set_sensitive(display_revert, confirmable);
}
static void display_scale_options(GtkComboBox *combo, gpointer unused)
{
    (void)unused;
    gtk_combo_box_text_remove_all(GTK_COMBO_BOX_TEXT(display_scales));
    int i = gtk_combo_box_get_active(combo);
    if (!display_valid || i < 0 || (unsigned)i >= display_snapshot->count) return;
    const DisplayMode *mode = &display_snapshot->modes[i];
    int selected = 0;
    for (unsigned j=0;j<mode->scale_count;++j) {
        char text[64]; g_snprintf(text, sizeof(text), "%.0f%%", mode->scales[j] * 100);
        gtk_combo_box_text_append_text(GTK_COMBO_BOX_TEXT(display_scales), text);
        if (fabs(mode->scales[j] - display_snapshot->scale) < 0.00001) selected = (int)j;
    }
    gtk_combo_box_set_active(GTK_COMBO_BOX(display_scales), selected);
}
static void display_populate(void)
{
    char text[256];
    g_snprintf(text, sizeof(text), "当前：%u × %u · %.2f Hz · 缩放 %.0f%%%s", display_snapshot->width,
        display_snapshot->height, display_snapshot->refresh, display_snapshot->scale * 100,
        display_snapshot->pending ? " · 有待确认切换" : "");
    gtk_label_set_text(GTK_LABEL(display_current), text);
    gtk_combo_box_text_remove_all(GTK_COMBO_BOX_TEXT(display_modes));
    int selected = 0;
    for (unsigned i=0;i<display_snapshot->count;++i) {
        DisplayMode *mode = &display_snapshot->modes[i];
        g_snprintf(text, sizeof(text), "%u × %u · %.2f Hz", mode->width, mode->height, mode->refresh);
        gtk_combo_box_text_append_text(GTK_COMBO_BOX_TEXT(display_modes), text);
        if (mode->width == display_snapshot->width && mode->height == display_snapshot->height &&
            fabs(mode->refresh - display_snapshot->refresh) < 0.00001) selected = (int)i;
    }
    gtk_combo_box_set_active(GTK_COMBO_BOX(display_modes), selected);
}
static void display_job_free(gpointer data)
{
    DisplayJob *job = data; g_free(job->snapshot); g_free(job);
}
static void display_worker(GTask *task, gpointer source, gpointer data, GCancellable *cancel)
{
    (void)source; (void)cancel;
    DisplayJob *job = data;
    if (job->operation == 0) {
        job->snapshot = g_new0(DisplaySnapshot, 1);
        job->failed = uurb_display_inspect(job->snapshot);
    } else if (job->operation == 1) {
        job->failed = uurb_display_apply(job->serial, job->mode.width, job->mode.height, job->mode.refresh, job->scale, &job->reply);
    } else {
        job->failed = uurb_display_finish(job->operation == 2, job->reply.serial, job->reply.transaction);
    }
    g_task_return_boolean(task, TRUE);
}
static void display_start(unsigned operation);
static void display_done(GObject *source, GAsyncResult *result, gpointer unused)
{
    (void)source; (void)unused;
    DisplayJob *job = g_task_get_task_data(G_TASK(result));
    (void)g_task_propagate_boolean(G_TASK(result), NULL);
    display_busy = FALSE;
    if (!window_alive) return;
    if (job->failed) {
        if (job->operation == 0) {
            display_valid = FALSE;
            gtk_label_set_text(GTK_LABEL(display_current), "无法读取显示服务；可能尚未启动或当前显示布局不支持。");
        }
        gtk_label_set_text(GTK_LABEL(display_message), job->operation == 0 ? "刷新失败，未改变屏幕。" :
            "操作被拒绝或回复丢失，未自动重试。屏幕可能已经改变；请等待回滚保护处理，再刷新状态。");
    } else if (job->operation == 0) {
        *display_snapshot = *job->snapshot; display_valid = TRUE;
        if (!display_snapshot->pending) display_has_transaction = FALSE;
        display_populate();
    } else if (job->operation == 1) {
        display_valid = FALSE; /* Require a fresh enumeration before another apply. */
        if (job->reply.changed) {
            display_owned = job->reply; display_has_transaction = TRUE;
            display_deadline = job->started + (gint64)job->reply.seconds * G_USEC_PER_SEC;
            gtk_label_set_text(GTK_LABEL(display_message), "已申请临时切换。确认画面与点击位置正常后，点击“保留此设置”；不确认会自动回滚。");
        } else {
            gtk_label_set_text(GTK_LABEL(display_message), "所选模式已经生效，无需切换。");
        }
    } else {
        gtk_label_set_text(GTK_LABEL(display_message), job->operation == 2 ? "显示设置已保留（当前桌面会话）。" : "已恢复切换前的显示设置。");
    }
    display_sensitivity();
    if (job->operation != 0) display_start(0);
}
static void display_start(unsigned operation)
{
    if (display_busy || !window_alive) return;
    DisplayJob *job = g_new0(DisplayJob, 1);
    job->operation = operation; job->started = g_get_monotonic_time();
    if (operation == 1) {
        int i = gtk_combo_box_get_active(GTK_COMBO_BOX(display_modes));
        int j = gtk_combo_box_get_active(GTK_COMBO_BOX(display_scales));
        if (!display_valid || display_snapshot->pending || display_has_transaction || i < 0 ||
            (unsigned)i >= display_snapshot->count || j < 0 || (unsigned)j >= display_snapshot->modes[i].scale_count) { g_free(job); return; }
        job->serial = display_snapshot->serial; job->mode = display_snapshot->modes[i];
        job->scale = job->mode.scales[j];
    } else if (operation >= 2) {
        if (!display_has_transaction || job->started >= display_deadline) { g_free(job); return; }
        job->reply = display_owned;
        display_has_transaction = FALSE; /* Never replay an ambiguous confirmation. */
    }
    display_busy = TRUE; display_sensitivity();
    GTask *task = g_task_new(NULL, NULL, display_done, NULL);
    g_task_set_task_data(task, job, display_job_free);
    g_task_run_in_thread(task, display_worker); g_object_unref(task);
}
static void display_action(GtkButton *button, gpointer data)
{
    unsigned operation = GPOINTER_TO_UINT(data);
    if (operation == 1) {
        GtkWidget *dialog = gtk_message_dialog_new(GTK_WINDOW(gtk_widget_get_toplevel(GTK_WIDGET(button))),
            GTK_DIALOG_MODAL | GTK_DIALOG_DESTROY_WITH_PARENT, GTK_MESSAGE_WARNING, GTK_BUTTONS_OK_CANCEL,
            "切换分辨率、刷新率或缩放可能短暂断开 UU。30 秒内不点击“保留此设置”将自动回滚；不会重启桌面或 RustDesk。继续吗？");
        int result = gtk_dialog_run(GTK_DIALOG(dialog)); gtk_widget_destroy(dialog);
        if (result != GTK_RESPONSE_OK) return;
    }
    display_start(operation);
}
static gboolean display_tick(gpointer unused)
{
    (void)unused;
    if (!window_alive) return G_SOURCE_REMOVE;
    if (display_has_transaction) {
        gint64 remaining = display_deadline - g_get_monotonic_time();
        char text[96];
        g_snprintf(text, sizeof(text), "保留此设置（剩余 %d 秒）", (int)MAX(0, (remaining + G_USEC_PER_SEC - 1) / G_USEC_PER_SEC));
        gtk_button_set_label(GTK_BUTTON(display_keep), text);
        if (remaining <= 0) {
            display_has_transaction = FALSE;
            gtk_label_set_text(GTK_LABEL(display_message), "确认时间已到，回滚由独立显示服务执行；点击刷新查看最终状态。");
            display_start(0);
        }
        display_sensitivity();
    }
    return G_SOURCE_CONTINUE;
}
static GtkWidget *display_page(void)
{
    display_snapshot = g_new0(DisplaySnapshot, 1);
    GtkWidget *box = gtk_box_new(GTK_ORIENTATION_VERTICAL, 12);
    gtk_container_set_border_width(GTK_CONTAINER(box), 16);
    gtk_box_pack_start(GTK_BOX(box), label("主机显示设置 · 原生 Wayland"), FALSE, FALSE, 0);
    gtk_box_pack_start(GTK_BOX(box), label("仅列出显示器实际公布、bridge 支持的模式和缩放。这里控制 Linux 显示缩放，不代表 UU 手机端 DPI 或“超级屏”已经适配。"), FALSE, FALSE, 0);
    display_current = label("正在读取显示模式…"); gtk_box_pack_start(GTK_BOX(box), display_current, FALSE, FALSE, 0);
    gtk_box_pack_start(GTK_BOX(box), label("分辨率与刷新率"), FALSE, FALSE, 0);
    display_modes = gtk_combo_box_text_new(); gtk_box_pack_start(GTK_BOX(box), display_modes, FALSE, FALSE, 0);
    gtk_box_pack_start(GTK_BOX(box), label("Linux 桌面缩放"), FALSE, FALSE, 0);
    display_scales = gtk_combo_box_text_new(); gtk_box_pack_start(GTK_BOX(box), display_scales, FALSE, FALSE, 0);
    g_signal_connect(display_modes, "changed", G_CALLBACK(display_scale_options), NULL);
    GtkWidget *buttons = gtk_box_new(GTK_ORIENTATION_HORIZONTAL, 8);
    display_refresh = gtk_button_new_with_label("刷新模式"); display_apply = gtk_button_new_with_label("临时应用（30 秒保护）");
    gtk_box_pack_start(GTK_BOX(buttons), display_refresh, FALSE, FALSE, 0);
    gtk_box_pack_start(GTK_BOX(buttons), display_apply, FALSE, FALSE, 0);
    gtk_box_pack_start(GTK_BOX(box), buttons, FALSE, FALSE, 0);
    display_keep = gtk_button_new_with_label("保留此设置"); display_revert = gtk_button_new_with_label("立即恢复");
    gtk_box_pack_start(GTK_BOX(box), display_keep, FALSE, FALSE, 0);
    gtk_box_pack_start(GTK_BOX(box), display_revert, FALSE, FALSE, 0);
    GtkWidget *all[] = {display_refresh, display_apply, display_keep, display_revert};
    for (unsigned i=0;i<4;++i) g_signal_connect(all[i], "clicked", G_CALLBACK(display_action), GUINT_TO_POINTER(i));
    display_message = label("关闭控制台不会取消回滚保护。保留只作用于当前桌面会话，不写入永久显示配置。");
    gtk_box_pack_start(GTK_BOX(box), display_message, FALSE, FALSE, 0);
    display_sensitivity();
    g_timeout_add(250, display_tick, NULL);
    return box;
}
