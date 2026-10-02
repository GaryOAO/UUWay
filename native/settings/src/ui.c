#include <gtk/gtk.h>
#include <stdint.h>
typedef struct { uint32_t relative_percent, wheel_percent, invert_wheel, cursor_metadata; } Preferences;
extern int uurb_preferences_load(Preferences *);
extern int uurb_preferences_save(uint32_t, uint32_t, uint32_t);
extern int uurb_cursor_save(uint32_t);
extern int uurb_console_status(unsigned char *, size_t);
extern const char *uurb_cursor_follow_help(void);
static GtkWidget *relative, *wheel, *invert, *cursor, *status, *reconnect;
static GtkWidget *overview;
static gboolean status_busy;
static GtkWidget *service_buttons[3], *service_notice;
static gboolean settings_available;
static gboolean window_alive;

static void status_worker(GTask *task, gpointer source, gpointer data, GCancellable *cancel)
{
    (void)source; (void)data; (void)cancel;
    char *text = g_malloc0(8192);
    if (uurb_console_status((unsigned char *)text, 8192)) g_strlcpy(text, "状态读取失败，未修改任何配置。", 8192);
    g_task_return_pointer(task, text, g_free);
}
static void status_done(GObject *source, GAsyncResult *result, gpointer data)
{
    (void)source; (void)data;
    char *text = g_task_propagate_pointer(G_TASK(result), NULL);
    if (text) { if (window_alive) gtk_label_set_text(GTK_LABEL(overview), text); g_free(text); }
    status_busy = FALSE;
}
static gboolean refresh_status(gpointer unused)
{
    (void)unused;
    if (!window_alive) return G_SOURCE_REMOVE;
    if (!status_busy) {
        status_busy = TRUE;
        GTask *task = g_task_new(NULL, NULL, status_done, NULL);
        g_task_run_in_thread(task, status_worker); g_object_unref(task);
    }
    return G_SOURCE_CONTINUE;
}

static void message(const char *text) { gtk_label_set_text(GTK_LABEL(status), text); }
static void save(GtkButton *button, gpointer unused)
{
    (void)button; (void)unused;
    int failed = uurb_preferences_save((uint32_t)gtk_range_get_value(GTK_RANGE(relative)),
        (uint32_t)gtk_range_get_value(GTK_RANGE(wheel)), gtk_toggle_button_get_active(GTK_TOGGLE_BUTTON(invert)));
    message(failed ? "保存失败：请检查配置权限或稍后重试；原配置未主动清除。" :
        "速度设置已保存。支持热加载的 bridge 会在后续输入时应用，无需重连。 ");
}
static void defaults(GtkButton *button, gpointer unused)
{
    (void)button; (void)unused;
    gtk_range_set_value(GTK_RANGE(relative), 100); gtk_range_set_value(GTK_RANGE(wheel), 100);
    gtk_toggle_button_set_active(GTK_TOGGLE_BUTTON(invert), FALSE);
    message("已恢复面板默认值，点击“保存速度设置”后生效。");
}
static void restart_finished(GObject *source, GAsyncResult *result, gpointer unused)
{
    (void)unused; GError *error = NULL;
    gboolean ok = g_subprocess_wait_check_finish(G_SUBPROCESS(source), result, &error);
    if (!window_alive) { g_clear_error(&error); g_object_unref(source); return; }
    const char *text = ok ? "UU 服务操作已完成。请查看运行状态；未触碰 RustDesk、桌面或 Portal。" :
        "UU 服务操作未成功。请查看运行状态；已保存的配置仍保留。";
    message(text); gtk_label_set_text(GTK_LABEL(service_notice), text);
    g_clear_error(&error); gtk_widget_set_sensitive(reconnect, settings_available);
    for (unsigned i=0;i<3;++i) gtk_widget_set_sensitive(service_buttons[i], TRUE);
    g_object_unref(source);
}
static void begin_service_action(const char *operation)
{
    GError *error = NULL;
    GSubprocess *process = g_subprocess_new(G_SUBPROCESS_FLAGS_STDOUT_SILENCE | G_SUBPROCESS_FLAGS_STDERR_SILENCE,
        &error, "/usr/bin/systemctl", "--user", operation, "uu-native-bridge.service", NULL);
    if (!process) { g_clear_error(&error); message("无法发起 UU 服务操作。"); return; }
    gtk_widget_set_sensitive(reconnect, FALSE);
    for (unsigned i=0;i<3;++i) gtk_widget_set_sensitive(service_buttons[i], FALSE);
    message("正在执行 UU 服务操作…"); gtk_label_set_text(GTK_LABEL(service_notice), "正在执行… 重连或停止会断开 UU 会话。");
    g_subprocess_wait_check_async(process, NULL, restart_finished, NULL);
}
static void service_control(GtkButton *button, gpointer operation)
{
    const char *verb = operation;
    if (!g_str_equal(verb,"start") && !g_str_equal(verb,"restart") && !g_str_equal(verb,"stop")) return;
    if (g_str_equal(verb,"stop")) {
        GtkWidget *dialog = gtk_message_dialog_new(GTK_WINDOW(gtk_widget_get_toplevel(GTK_WIDGET(button))),
            GTK_DIALOG_MODAL | GTK_DIALOG_DESTROY_WITH_PARENT, GTK_MESSAGE_WARNING, GTK_BUTTONS_OK_CANCEL,
            "停止 UU 会断开当前 UU 连接。之后需要本机或其他远程通道重新启动；RustDesk 不会被停止。继续吗？");
        int response = gtk_dialog_run(GTK_DIALOG(dialog)); gtk_widget_destroy(dialog);
        if (response != GTK_RESPONSE_OK) return;
    }
    begin_service_action(verb);
}
static void apply_cursor(GtkButton *button, gpointer unused)
{
    (void)button; (void)unused;
    int mode = gtk_combo_box_get_active(GTK_COMBO_BOX(cursor));
    if (mode < 0 || uurb_cursor_save((uint32_t)mode)) { message("光标设置保存失败，未发起重连。"); return; }
    begin_service_action("restart");
}
static GtkWidget *label(const char *text)
{
    GtkWidget *widget = gtk_label_new(text); gtk_label_set_xalign(GTK_LABEL(widget), 0);
    gtk_label_set_line_wrap(GTK_LABEL(widget), TRUE); return widget;
}
static GtkWidget *slider(GtkWidget *box, const char *title, unsigned value)
{
    gtk_box_pack_start(GTK_BOX(box), label(title), FALSE, FALSE, 0);
    GtkWidget *scale = gtk_scale_new_with_range(GTK_ORIENTATION_HORIZONTAL, 25, 400, 5);
    gtk_scale_set_digits(GTK_SCALE(scale), 0); gtk_scale_add_mark(GTK_SCALE(scale), 100, GTK_POS_BOTTOM, "100% 默认");
    gtk_range_set_value(GTK_RANGE(scale), value); gtk_box_pack_start(GTK_BOX(box), scale, FALSE, FALSE, 0); return scale;
}
static gboolean close_smoke(gpointer window) { gtk_widget_destroy(window); return G_SOURCE_REMOVE; }
#include "display_ui.h"
static void window_destroyed(GtkWidget *widget, gpointer unused)
{
    (void)widget; (void)unused; window_alive = FALSE;
}
static void activate(GtkApplication *application, gpointer smoke)
{
    GtkWindow *existing = gtk_application_get_active_window(application);
    if (existing) { gtk_window_present(existing); return; }
    Preferences prefs = {100,100,0,0}; int loaded = uurb_preferences_load(&prefs);
    GtkWidget *window = gtk_application_window_new(application);
    window_alive = TRUE;
    gtk_window_set_title(GTK_WINDOW(window), "UUWay · Linux 客户端"); gtk_window_set_default_size(GTK_WINDOW(window), 760, 760);
    gtk_container_set_border_width(GTK_CONTAINER(window), 24);
    GtkWidget *shell = gtk_box_new(GTK_ORIENTATION_VERTICAL, 12); gtk_container_add(GTK_CONTAINER(window), shell);
    GtkWidget *toolbar = gtk_box_new(GTK_ORIENTATION_HORIZONTAL, 8);
    const char *titles[]={"启动 UU", "重连 UU", "停止 UU"};
    const char *operations[]={"start", "restart", "stop"};
    for (unsigned i=0;i<3;++i) {
        service_buttons[i]=gtk_button_new_with_label(titles[i]);
        gtk_box_pack_start(GTK_BOX(toolbar), service_buttons[i], FALSE, FALSE, 0);
        g_signal_connect(service_buttons[i], "clicked", G_CALLBACK(service_control), (gpointer)operations[i]);
    }
    gtk_box_pack_start(GTK_BOX(shell), toolbar, FALSE, FALSE, 0);
    service_notice=label("管理这台 Linux 主机的 UU 原生 bridge；其他远程通道不受这些按钮控制。");
    gtk_box_pack_start(GTK_BOX(shell), service_notice, FALSE, FALSE, 0);
    GtkWidget *tabs = gtk_notebook_new(); gtk_box_pack_start(GTK_BOX(shell), tabs, TRUE, TRUE, 0);
    GtkWidget *summary = gtk_scrolled_window_new(NULL, NULL);
    gtk_scrolled_window_set_policy(GTK_SCROLLED_WINDOW(summary), GTK_POLICY_NEVER, GTK_POLICY_AUTOMATIC);
    overview = label("正在读取本机 bridge 状态…");
    gtk_widget_set_margin_start(overview, 16); gtk_widget_set_margin_end(overview, 16);
    gtk_widget_set_margin_top(overview, 16); gtk_widget_set_margin_bottom(overview, 16);
    gtk_label_set_selectable(GTK_LABEL(overview), TRUE); gtk_label_set_yalign(GTK_LABEL(overview), 0);
    gtk_container_add(GTK_CONTAINER(summary), overview);
    gtk_notebook_append_page(GTK_NOTEBOOK(tabs), summary, gtk_label_new("状态与主机"));
    GtkWidget *box = gtk_box_new(GTK_ORIENTATION_VERTICAL, 12);
    GtkWidget *settings_scroll = gtk_scrolled_window_new(NULL, NULL);
    gtk_scrolled_window_set_policy(GTK_SCROLLED_WINDOW(settings_scroll), GTK_POLICY_NEVER, GTK_POLICY_AUTOMATIC);
    gtk_widget_set_margin_start(box, 16); gtk_widget_set_margin_end(box, 16);
    gtk_widget_set_margin_top(box, 16); gtk_widget_set_margin_bottom(box, 16);
    gtk_container_add(GTK_CONTAINER(settings_scroll), box);
    gtk_notebook_append_page(GTK_NOTEBOOK(tabs), settings_scroll, gtk_label_new("输入与光标"));
    GtkWidget *display_scroll = gtk_scrolled_window_new(NULL, NULL);
    gtk_scrolled_window_set_policy(GTK_SCROLLED_WINDOW(display_scroll), GTK_POLICY_NEVER, GTK_POLICY_AUTOMATIC);
    gtk_container_add(GTK_CONTAINER(display_scroll), display_page());
    gtk_notebook_append_page(GTK_NOTEBOOK(tabs), display_scroll, gtk_label_new("显示设置"));
    GtkWidget *heading = label("UUWay · 原生设置");
    gtk_style_context_add_class(gtk_widget_get_style_context(heading), "title");
    gtk_box_pack_start(GTK_BOX(box), heading, FALSE, FALSE, 0);
    gtk_box_pack_start(GTK_BOX(box), label("仅调整本机 bridge。关闭此面板不影响远程服务；无需管理员权限。"), FALSE, FALSE, 0);
    relative = slider(box, "相对鼠标速度（%）", prefs.relative_percent);
    gtk_box_pack_start(GTK_BOX(box), label("仅影响相对移动；手机绝对定位保持原坐标，避免点击位置偏移。"), FALSE, FALSE, 0);
    wheel = slider(box, "滚轮速度（%）", prefs.wheel_percent);
    invert = gtk_check_button_new_with_label("反转滚轮方向（横向和纵向）");
    gtk_toggle_button_set_active(GTK_TOGGLE_BUTTON(invert), prefs.invert_wheel); gtk_box_pack_start(GTK_BOX(box), invert, FALSE, FALSE, 0);
    GtkWidget *buttons = gtk_box_new(GTK_ORIENTATION_HORIZONTAL, 8), *save_button = gtk_button_new_with_label("保存速度设置"),
        *reset_button = gtk_button_new_with_label("恢复默认值");
    gtk_box_pack_start(GTK_BOX(buttons), save_button, FALSE, FALSE, 0); gtk_box_pack_start(GTK_BOX(buttons), reset_button, FALSE, FALSE, 0);
    gtk_box_pack_start(GTK_BOX(box), buttons, FALSE, FALSE, 0);
    gtk_box_pack_start(GTK_BOX(box), gtk_separator_new(GTK_ORIENTATION_HORIZONTAL), FALSE, FALSE, 0);
    gtk_box_pack_start(GTK_BOX(box), label("光标显示方式"), FALSE, FALSE, 0);
    cursor = gtk_combo_box_text_new();
    gtk_combo_box_text_append_text(GTK_COMBO_BOX_TEXT(cursor), "视频内的 Ubuntu 真实光标（手机画面跟随待实测）");
    gtk_combo_box_text_append_text(GTK_COMBO_BOX_TEXT(cursor), "独立光标元数据（实验性，手机端效果未通过验证）");
    gtk_combo_box_text_append_text(GTK_COMBO_BOX_TEXT(cursor), "GPU 合成真实光标（手机画面跟随待实测）");
    gtk_combo_box_set_active(GTK_COMBO_BOX(cursor), prefs.cursor_metadata); gtk_box_pack_start(GTK_BOX(box), cursor, FALSE, FALSE, 0);
    GtkWidget *follow_help = gtk_expander_new("手机画面跟随 · 客户端设置说明");
    gtk_container_add(GTK_CONTAINER(follow_help), label(uurb_cursor_follow_help()));
    gtk_box_pack_start(GTK_BOX(box), follow_help, FALSE, FALSE, 0);
    reconnect = gtk_button_new_with_label("应用光标方式并重连 UU"); gtk_box_pack_start(GTK_BOX(box), reconnect, FALSE, FALSE, 0);
    status = label(loaded ? "无法读取现有 bridge 配置。为保护原配置，保存功能已禁用。" : "就绪。速度设置无需重连；更换光标方式会短暂断开 UU。");
    gtk_box_pack_start(GTK_BOX(box), status, FALSE, FALSE, 0);
    gtk_widget_set_sensitive(save_button, !loaded); gtk_widget_set_sensitive(reconnect, !loaded);
    settings_available = !loaded;
    g_signal_connect(save_button, "clicked", G_CALLBACK(save), NULL);
    g_signal_connect(reset_button, "clicked", G_CALLBACK(defaults), NULL);
    g_signal_connect(reconnect, "clicked", G_CALLBACK(apply_cursor), NULL);
    g_signal_connect(window, "destroy", G_CALLBACK(window_destroyed), NULL);
    gtk_widget_show_all(window);
    refresh_status(NULL); g_timeout_add_seconds(3, refresh_status, NULL);
    display_start(0);
    if (GPOINTER_TO_INT(smoke)) g_timeout_add(800, close_smoke, window);
}
int uurb_settings_gui(int smoke)
{
    /* Smoke tests never activate or close a user's existing console. Normal
       launches use one session-bus application ID across binary upgrades. */
    GtkApplication *application = gtk_application_new("io.uuway.Console",
        smoke ? G_APPLICATION_NON_UNIQUE : G_APPLICATION_DEFAULT_FLAGS);
    g_signal_connect(application, "activate", G_CALLBACK(activate), GINT_TO_POINTER(smoke));
    char *arguments[] = {"uuway-console", NULL};
    int result = g_application_run(G_APPLICATION(application), 1, arguments);
    g_object_unref(application);
    return result;
}
