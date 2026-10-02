#!/usr/bin/env bash
# UUWay 一键安装：按 docs/build.md 的步骤检查环境、安装依赖、把 UU 装进独立的 Wine
# 前缀、构建原生运行时、授权输入与屏幕共享，最后打包并启动用户服务。
#
# 可以反复运行：已经完成的步骤会跳过，或者安全地重做。需要你亲手完成的只有两件事：
# 在 UU 窗口里登录账号，以及在屏幕共享对话框里选择显示器。
set -Eeuo pipefail

repo_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
prefix="${UUWAY_PREFIX:-$HOME/.local/share/wineprefixes/uu-remote}"
state_dir="$HOME/.local/state/uurb"
config_dir="$HOME/.config/uurb"
wine_dir=/opt/wine-stable/bin
python=/usr/bin/python3
server_exe="$prefix/drive_c/Program Files/Netease/GameViewer/bin/GameViewerServer.exe"
restore_state="$state_dir/portal-probe.json"
udev_rule=/etc/udev/rules.d/70-uurb-native-input.rules

installer=""
from_step=1
assume_yes=0
dry_run=0
login=0
with_console=1

packages=(build-essential gcc-mingw-w64-x86-64 meson ninja-build pkg-config jq curl
          libjson-c-dev libx11-dev libxfixes-dev libpipewire-0.3-dev libvulkan-dev glslang-tools
          libei-dev fcitx5-modules-dev xvfb xclip python3-gi gir1.2-gtk-3.0)

usage() {
    cat <<'EOF'
用法：./install.sh [选项]

  --installer 路径   UU 远程官方 Windows 安装包（例如 ~/Downloads/UURemote_Setup.exe）；
                     不指定时会在 ~/Downloads 里查找，找不到再询问
  --from 步骤        从第几步开始（1-7），用于中断后继续
  --login            即使 UU 已安装，也重新打开 UU 登录一次
  --no-console       不安装控制台（图形设置工具）
  --yes              不再逐项确认（登录 UU 和选择显示器仍需手动完成）
  --dry-run          只显示将要执行的命令，不做任何改动
  -h, --help         显示本帮助

步骤：1 检查环境并安装依赖  2 安装并登录 UU  3 构建原生运行时  4 安装辅助程序
      5 授权输入与屏幕共享  6 打包并启动服务  7 安装控制台

UUWay one-step installer. Run it from a terminal in the GNOME Wayland session, as your
own user (not with sudo); it asks for sudo only to install packages and one udev rule.
EOF
}

while (($#)); do
    case "$1" in
        --installer) installer="${2:?--installer 需要一个路径}"; shift 2 ;;
        --from) from_step="${2:?--from 需要一个步骤编号}"; shift 2 ;;
        --login) login=1; shift ;;
        --no-console) with_console=0; shift ;;
        --yes|-y) assume_yes=1; shift ;;
        --dry-run) dry_run=1; shift ;;
        -h|--help) usage; exit 0 ;;
        *) printf '未知选项：%s\n\n' "$1" >&2; usage >&2; exit 2 ;;
    esac
done
[[ "$from_step" =~ ^[1-7]$ ]] || { printf -- '--from 只能是 1 到 7\n' >&2; exit 2; }

if [[ -t 1 ]]; then
    bold=$'\e[1m' dim=$'\e[2m' red=$'\e[31m' green=$'\e[32m' yellow=$'\e[33m' cyan=$'\e[36m' reset=$'\e[0m'
else
    bold="" dim="" red="" green="" yellow="" cyan="" reset=""
fi

step_title() { printf '\n%s━━ [%s/7] %s%s\n' "$bold$cyan" "$1" "$2" "$reset"; }
info() { printf '   %s\n' "$*"; }
ok() { printf '   %s✓%s %s\n' "$green" "$reset" "$*"; }
warn() { printf '   %s!%s %s\n' "$yellow" "$reset" "$*"; }
die() { printf '\n%s✗ %s%s\n' "$red" "$*" "$reset" >&2; exit 1; }

# Print a command; run it unless this is a dry run.
run() {
    printf '   %s$ %s%s\n' "$dim" "${*@Q}" "$reset"
    ((dry_run)) || "$@"
}

confirm() {
    ((assume_yes || dry_run)) && return 0
    local answer
    read -r -p "   $1 [Y/n] " answer
    [[ -z "$answer" || "$answer" =~ ^[Yy] ]]
}

# Wait for the user to finish something only they can do.
wait_for_user() {
    if ((dry_run)); then
        info "（演练）此处会等待：$1"
        return
    fi
    read -r -p "   $1，完成后按回车继续…" _
}

should_run() { (($1 >= from_step)); }

# UU writes setting_<account>.ini once someone has signed in.
signed_in() {
    local file
    for file in "$prefix"/drive_c/users/*/AppData/Local/GameViewer/setting_*.ini; do
        [[ -f "$file" && "$file" != */setting_guest_* ]] && return 0
    done
    return 1
}

trap 'die "第 ${current_step:-?} 步出错（上面是出错的命令）。修复后可以用 ./install.sh --from ${current_step:-1} 继续。"' ERR

wine_env=(env "WINEPREFIX=$prefix" WINEDEBUG=-all)

# ---------------------------------------------------------------------------
current_step=1
step_title 1 "检查环境并安装依赖"
((EUID != 0)) || die "请不要用 sudo 或 root 运行；脚本会在需要时自己调用 sudo。"
# shellcheck source=/dev/null
. /etc/os-release
if [[ "${ID:-}" == ubuntu && "${VERSION_ID:-}" == 24.04 ]]; then
    ok "Ubuntu 24.04"
else
    warn "UUWay 只在 Ubuntu 24.04 上验证过（当前：${PRETTY_NAME:-未知}），继续但不保证可用。"
fi
[[ "${XDG_SESSION_TYPE:-}" == wayland && "${XDG_CURRENT_DESKTOP:-}" == *GNOME* ]] ||
    die "请在 GNOME Wayland 会话里打开终端运行（当前会话：${XDG_SESSION_TYPE:-未知} / ${XDG_CURRENT_DESKTOP:-未知}）。"
ok "GNOME Wayland 会话"
if ! command -v nvidia-smi >/dev/null || ! nvidia-smi -L >/dev/null 2>&1; then
    die "没有找到可用的 NVIDIA 显卡驱动。UUWay 需要支持 NVENC 的 NVIDIA 显卡和官方驱动。"
fi
ok "NVIDIA 显卡：$(nvidia-smi --query-gpu=name,driver_version --format=csv,noheader | head -1)"

if should_run 1; then
    missing=()
    for package in "${packages[@]}"; do
        dpkg-query -W -f='${Status}' "$package" 2>/dev/null | grep -q 'install ok installed' || missing+=("$package")
    done
    if ((${#missing[@]})); then
        info "需要安装的软件包：${missing[*]}"
        confirm "现在用 sudo apt 安装？" || die "缺少依赖，已停止。"
        run sudo apt-get update
        run sudo apt-get install -y "${missing[@]}"
    else
        ok "构建依赖已齐全"
    fi

    if [[ -x "$wine_dir/wine" ]] && "$wine_dir/wine" --version 2>/dev/null | grep -q '^wine-11'; then
        ok "WineHQ stable：$("$wine_dir/wine" --version)"
    else
        info "需要 WineHQ stable 11（安装在 /opt/wine-stable）。"
        confirm "现在添加 WineHQ 软件源并安装 winehq-stable？" || die "缺少 WineHQ stable 11，已停止。"
        run sudo dpkg --add-architecture i386
        run sudo mkdir -pm755 /etc/apt/keyrings
        run sudo curl -fsSLo /etc/apt/keyrings/winehq-archive.key https://dl.winehq.org/wine-builds/winehq.key
        run sudo curl -fsSLo /etc/apt/sources.list.d/winehq-noble.sources \
            https://dl.winehq.org/wine-builds/ubuntu/dists/noble/winehq-noble.sources
        run sudo apt-get update
        run sudo apt-get install -y --install-recommends winehq-stable
        ((dry_run)) || "$wine_dir/wine" --version | grep -q '^wine-11' ||
            die "装好的 WineHQ stable 不是 11 版（$("$wine_dir/wine" --version)）。"
    fi
fi

# ---------------------------------------------------------------------------
current_step=2
if should_run 2; then
    step_title 2 "安装并登录 UU 远程"
    # The service's UU holds the prefix; it starts again in step 6.
    if systemctl --user is-active --quiet uu-native-bridge 2>/dev/null; then
        info "先停止正在运行的 UUWay 服务（第 6 步会重新启动）。"
        run systemctl --user stop uu-native-bridge
    fi
    if [[ -f "$server_exe" ]]; then
        ok "UU 已安装在 $prefix"
    else
        if [[ -z "$installer" ]]; then
            for candidate in "$HOME"/Downloads/UURemote*.exe "$HOME"/Downloads/*UU*emote*.exe "$HOME"/下载/UURemote*.exe; do
                [[ -f "$candidate" ]] && { installer="$candidate"; break; }
            done
        fi
        if [[ -z "$installer" && $dry_run -eq 0 ]]; then
            info "请先从 UU 远程官网下载 Windows 版安装包。"
            read -r -e -p "   安装包路径：" installer
            installer="${installer/#\~/$HOME}"
        fi
        [[ -n "$installer" ]] || installer="$HOME/Downloads/UURemote_Setup.exe"
        ((dry_run)) || [[ -f "$installer" ]] || die "找不到安装包：$installer"
        ok "安装包：$installer"
        run mkdir -p "$(dirname "$prefix")"
        run "${wine_env[@]}" "$wine_dir/wineboot" -u
        run "${wine_env[@]}" "$wine_dir/wine" "$installer" /S
        run "${wine_env[@]}" "$wine_dir/wineserver" -w
        ((dry_run)) || [[ -f "$server_exe" ]] || die "安装包运行结束，但没有找到 GameViewerServer.exe。"
        ok "UU 已安装"
        login=1
    fi

    if ((login)) || ! signed_in; then
        info "接下来会打开 UU 窗口：请登录你的 UU 账号，登录成功后从托盘图标${bold}完全退出${reset} UU。"
        printf '   %s$ wine GameViewer.exe%s\n' "$dim" "$reset"
        ((dry_run)) || "${wine_env[@]}" "$wine_dir/wine" 'C:\Program Files\Netease\GameViewer\GameViewer.exe' \
            >/dev/null 2>&1 &
        wait_for_user "在 UU 里登录并完全退出"
        ((dry_run)) || signed_in || warn "没有检测到登录信息；如果连接不上，请用 ./install.sh --login 重新登录。"
        run "${wine_env[@]}" "$wine_dir/wineserver" -k || true
    fi

    # UU's terminal starts PowerShell; Wine must load UUWay's stand-in, not its placeholder.
    run "${wine_env[@]}" "$wine_dir/wine" reg add 'HKCU\Software\Wine\DllOverrides' \
        /v powershell.exe /t REG_SZ /d native /f
    run "${wine_env[@]}" "$wine_dir/wineserver" -w
fi

# ---------------------------------------------------------------------------
current_step=3
if should_run 3; then
    step_title 3 "构建原生运行时（第一次需要十几分钟）"
    cd "$repo_dir"
    for script in build-dxvk-capture build-dxgi-capture-probe build-uu-native-bootstrap \
                  build-uu-native-input build-uu-native-display build-native-ime build-helpers \
                  build-pipewire-probe; do
        run bash "scripts/$script.sh"
    done
    if ((with_console)); then
        run bash scripts/build-uu-settings.sh
    fi
    ok "构建完成"
fi

# ---------------------------------------------------------------------------
current_step=4
if should_run 4; then
    step_title 4 "安装辅助程序"
    run mkdir -p "$prefix/compat"
    ((dry_run)) || compgen -G "$repo_dir/build/helpers/*" >/dev/null || die "没有找到 build/helpers，请先完成第 3 步。"
    run install -m 0755 "$repo_dir"/build/helpers/* "$prefix/compat/"
    ok "辅助程序已安装到 $prefix/compat"
fi

# ---------------------------------------------------------------------------
current_step=5
if should_run 5; then
    step_title 5 "授权输入与屏幕共享"
    if cmp -s "$repo_dir/config/70-uurb-native-input.rules" "$udev_rule"; then
        ok "uinput 权限规则已安装"
    else
        info "键鼠注入需要当前桌面用户能访问 /dev/uinput（只授予本地登录会话，不改用户组）。"
        confirm "现在用 sudo 安装 udev 规则？" || die "没有输入权限，已停止。"
        run sudo install -m 0644 "$repo_dir/config/70-uurb-native-input.rules" "$udev_rule"
        run sudo udevadm control --reload
        run sudo udevadm trigger
    fi

    run mkdir -p -m 0700 "$state_dir"
    if [[ -s "$restore_state" ]]; then
        ok "屏幕共享已授权（$restore_state）"
    else
        info "接下来会弹出屏幕共享对话框：请选择要串流给手机的显示器，并勾选「记住」。"
        run "$python" "$repo_dir/scripts/probe-wayland-portal.py" --capture-check --restore-state "$restore_state"
        ((dry_run)) || [[ -s "$restore_state" ]] || die "没有保存屏幕共享授权，请重新运行本步骤并勾选「记住」。"
        ok "屏幕共享已授权"
    fi
fi

# ---------------------------------------------------------------------------
current_step=6
if should_run 6; then
    step_title 6 "打包并启动服务"
    cd "$repo_dir"
    if ((dry_run)); then
        run "$python" scripts/package-uu-native-runtime.py package --with-input build/uu-native-releases
        bundle="$repo_dir/build/uu-native-releases/<sha256>"
    else
        printf '   %s$ %s%s\n' "$dim" "$python scripts/package-uu-native-runtime.py package --with-input build/uu-native-releases" "$reset"
        bundle="$("$python" scripts/package-uu-native-runtime.py package --with-input build/uu-native-releases | jq -r .bundle)"
        [[ -d "$bundle" ]] || die "打包没有产生运行时目录。"
    fi
    ok "运行时：$bundle"
    run "$python" scripts/install-uu-native-service.py \
        --bundle "$bundle" --prefix "$prefix" --restore-state "$restore_state" \
        --state-parent "$state_dir" --text-socket "$state_dir/text.sock" --cursor-mode metadata

    # Apply Wine's Linux path view while the bridge is stopped.  Keeping this
    # out of the long-lived service avoids racing Wine initialization on boot.
    if systemctl --user is-active --quiet uu-native-bridge 2>/dev/null; then
        run systemctl --user stop uu-native-bridge
    fi
    run "$python" scripts/configure-uu-wine-mappings.py \
        --prefix "$prefix" --config-directory "$config_dir"
    # The registry command starts a short-lived Wine server.  Reap it fully
    # before the bridge's Wine runtime is allowed to initialize.
    run env "WINEPREFIX=$prefix" WINEDEBUG=-all "$wine_dir/wineserver" -k
    run env "WINEPREFIX=$prefix" WINEDEBUG=-all "$wine_dir/wineserver" -w

    # Phone IME text goes through Fcitx5 when it runs, otherwise through the portal.
    if [[ ! -f "$config_dir/text-backend.json" ]]; then
        backend=portal
        pgrep -u "$USER" -x fcitx5 >/dev/null && backend=fcitx
        run mkdir -p "$config_dir"
        if ((dry_run)); then
            info "（演练）会写入 $config_dir/text-backend.json：$backend"
        else
            printf '{"version":1,"backend":"%s"}' "$backend" >"$config_dir/text-backend.json"
        fi
        ok "手机输入法文字通道：$backend"
    fi

    run systemctl --user daemon-reload
    run systemctl --user enable --now uu-native-display uu-native-text uu-native-bridge
    # A re-run brings in the new runtime.
    run systemctl --user restart uu-native-bridge
    if ((!dry_run)); then
        sleep 5
        systemctl --user is-active --quiet uu-native-bridge ||
            die "uu-native-bridge 没有启动，请查看：journalctl --user -u uu-native-bridge -e"
    fi
    ok "服务已启动"
fi

# ---------------------------------------------------------------------------
current_step=7
if should_run 7 && ((with_console)); then
    step_title 7 "安装控制台"
    run "$python" "$repo_dir/scripts/install-uu-settings.py"
    ok "已添加到应用菜单：UUWay 控制台"
fi

trap - ERR
printf '\n%s🎉 安装完成！%s\n' "$bold$green" "$reset"
cat <<EOF
   在手机上打开 UU 远程，设备列表里就会出现这台电脑。

   查看状态  systemctl --user status uu-native-bridge
   查看日志  journalctl --user -u uu-native-bridge -e
             $state_dir/terminal-bridge.log、clipboard-bridge.log

   进阶：想把 60 Hz 屏幕共享的丢帧降到最低，可以按 docs/build.md 里
   「Optional: 60 FPS capture pacing」一节安装帧节奏补丁。
EOF
