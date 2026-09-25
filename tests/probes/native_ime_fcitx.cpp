// Real isolated Fcitx event loop and UNIX transport; owned input context only.
#include "../../src/uu_fcitx_native_ime.cpp"
#include <fcitx/inputcontextmanager.h>
#include <cassert>
#include <iostream>

class Editor final : public fcitx::InputContext {
public:
    std::vector<std::string> commits;
    explicit Editor(fcitx::InputContextManager &manager) : InputContext(manager, "uurb-owned-fixture") { created(); }
    ~Editor() override { destroy(); }
    const char *frontend() const override { return "uurb-owned-fixture"; }
    void commitStringImpl(const std::string &text) override { commits.push_back(text); }
    void deleteSurroundingTextImpl(int, unsigned) override { assert(false); }
    void forwardKeyImpl(const fcitx::ForwardKeyEvent &) override { assert(false); }
    void updatePreeditImpl() override {}
};

int main() {
    char app[] = "uurb-ime-fixture", disabled[] = "--disable=all";
    char *argv[] = {app, disabled, nullptr};
    fcitx::Instance instance(2, argv);
    instance.initialize();
    Editor editor(instance.inputContextManager());
    const std::string expected = u8"中文🙂\n第二行\tABC";
    const char *path = getenv("UURB_IME_SOCKET");
    assert(path && path[0] == '/');
    // An exact private socket left by a dead process can be reclaimed.
    int stale = socket(AF_UNIX, SOCK_SEQPACKET | SOCK_CLOEXEC, 0);
    sockaddr_un staleAddress {}; staleAddress.sun_family = AF_UNIX;
    strcpy(staleAddress.sun_path, path);
    assert(!bind(stale, reinterpret_cast<sockaddr *>(&staleAddress), sizeof(staleAddress)));
    assert(!chmod(path, 0600)); close(stale);
    {
        NativeIme ime(&instance);
        // A concurrent addon must fail without unlinking this listener.
        struct stat before {}, after {};
        assert(!lstat(path, &before));
        bool rejected = false;
        try { NativeIme duplicate(&instance); } catch (const std::runtime_error &) { rejected = true; }
        assert(rejected && !lstat(path, &after) && before.st_ino == after.st_ino);
        int phase = 0, fd = -1;
        auto tick = instance.eventLoop().addTimeEvent(CLOCK_MONOTONIC, fcitx::now(CLOCK_MONOTONIC) + 20000, 0,
            [&](fcitx::EventSourceTime *timer, uint64_t now) {
                if (phase % 2 == 0) {
                    if (phase == 2) editor.focusIn();
                    if (phase == 6) editor.inputPanel().setClientPreedit(fcitx::Text("owned-preedit"));
                    if (phase == 8) editor.inputPanel().reset();
                    fd = socket(AF_UNIX, SOCK_SEQPACKET | SOCK_CLOEXEC | SOCK_NONBLOCK, 0);
                    sockaddr_un address {}; address.sun_family = AF_UNIX;
                    strcpy(address.sun_path, path);
                    assert(!connect(fd, reinterpret_cast<sockaddr *>(&address), sizeof(address)));
                    std::string body = phase == 4 ? std::string("\xff", 1) : expected;
                    Header header {Magic, phase == 8 ? 2u : 1u, 1, static_cast<uint32_t>(body.size())};
                    std::string packet(reinterpret_cast<char *>(&header), sizeof(header)); packet += body;
                    assert(send(fd, packet.data(), packet.size(), MSG_NOSIGNAL) == static_cast<ssize_t>(packet.size()));
                    ++phase;
                } else {
                    Header response {};
                    auto count = recv(fd, &response, sizeof(response), MSG_DONTWAIT);
                    if (count < 0 && errno == EAGAIN) {
                        timer->setTime(now + 20000); timer->setOneShot(); return true;
                    }
                    assert(count == sizeof(response) && response.magic == Magic && response.sequence == 1);
                    const std::array<uint32_t, 6> errors {0x2101, 0, 0x2001, 0x2102, 0x2002, 0};
                    assert(response.size == errors[phase / 2]);
                    close(fd); fd = -1; ++phase;
                    if (phase == 12) { instance.eventLoop().exit(); return false; }
                }
                timer->setTime(now + 20000); timer->setOneShot(); return true;
            });
        auto timeout = instance.eventLoop().addTimeEvent(CLOCK_MONOTONIC, fcitx::now(CLOCK_MONOTONIC) + 3000000, 0,
            [](fcitx::EventSourceTime *, uint64_t) { assert(false); return false; });
        instance.eventLoop().exec();
        assert(editor.commits == std::vector<std::string>({expected, expected}));
    }
    struct stat info {};
    assert(lstat(path, &info) && errno == ENOENT);
    std::cout << "{\"native_context_commit\":true,\"no_focus_rejected\":true,\"invalid_utf8_rejected\":true,"
                 "\"local_preedit_preserved\":true,\"unverified_revision_rejected\":true,"
                 "\"owned_socket_removed\":true,\"stale_socket_reclaimed\":true,"
                 "\"live_owner_protected\":true,\"uu_phone_tested\":false}\n";
}
