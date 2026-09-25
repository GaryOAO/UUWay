// Native Unicode commits through the user's existing Fcitx input context.
// No clipboard, synthetic shortcut, input-method switching, or text logging.
#include <fcitx/addonfactory.h>
#include <fcitx/addonmanager.h>
#include <fcitx/inputcontext.h>
#include <fcitx/inputpanel.h>
#include <fcitx/instance.h>
#include <fcitx/event.h>
#include <fcitx-utils/event.h>
#include <fcitx-utils/utf8.h>
#include <array>
#include <cstring>
#include <memory>
#include <stdexcept>
#include <string>
#include <cstdlib>
#include <fcntl.h>
#include <pwd.h>
#include <sys/file.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <sys/un.h>
#include <unistd.h>

namespace {
constexpr uint32_t Magic = 0x54525555;
struct Header { uint32_t magic, version, sequence, size; };
static_assert(sizeof(Header) == 16);
#if __BYTE_ORDER__ != __ORDER_LITTLE_ENDIAN__
#error Native input wire protocol requires little endian
#endif

class NativeIme final : public fcitx::AddonInstance {
    struct Client {
        int fd = -1;
        uint64_t epoch = 0, expires = 0;
        std::unique_ptr<fcitx::EventSourceIO> watch;
        void close() {
            if (watch) watch->setEnabled(false);
            if (fd >= 0) ::close(fd);
            fd = -1;
        }
        ~Client() { close(); }
    };
    fcitx::Instance *instance_;
    int listener_ = -1;
    int lock_ = -1;
    std::string endpoint_;
    struct stat bound_ {};
    bool boundKnown_ = false;
    uint64_t focusEpoch_ = 0;
    std::array<std::unique_ptr<Client>, 4> clients_;
    std::unique_ptr<fcitx::EventSourceIO> listenerWatch_;
    std::unique_ptr<fcitx::EventSourceTime> timer_;
    std::vector<std::unique_ptr<fcitx::HandlerTableEntry<fcitx::EventHandler>>> focusWatches_;

    uint32_t commit(const char *bytes, size_t size, uint64_t epoch) {
        if (!size || size > 8192 || memchr(bytes, 0, size)) return 0x2001;
        const auto length = fcitx::utf8::lengthValidated(bytes, bytes + size);
        if (!length || length > 2048) return 0x2001;
        auto *context = instance_->mostRecentInputContext();
        if (epoch != focusEpoch_ || !context || !context->hasFocus()) return 0x2101;
        // Do not discard a local input method's unfinished composition.
        if (!context->inputPanel().preedit().empty() || !context->inputPanel().clientPreedit().empty())
            return 0x2102;
        // Focus validation and dispatch run in one Fcitx event-loop callback.
        // ACK means dispatch only; only an actual client can witness insertion.
        context->commitString(std::string(bytes, size));
        return 0;
    }

    void receive(Client &client) noexcept {
        std::array<char, sizeof(Header) + 8192 + 4> packet {};
        const auto count = recv(client.fd, packet.data(), packet.size(), MSG_TRUNC | MSG_DONTWAIT);
        Header input {}, reply {Magic, 1, 0, 0x2001};
        if (count >= static_cast<ssize_t>(sizeof(input))) {
            memcpy(&input, packet.data(), sizeof(input));
            reply.sequence = input.sequence;
            if (input.magic == Magic && input.sequence && input.size == count - sizeof(input)) {
                try {
                    if (input.version == 1 && input.size <= 8192)
                        reply.size = commit(packet.data() + sizeof(input), input.size, client.epoch);
                    else if (input.version == 2)
                        reply.size = 0x2002; // No unverified surrounding-text deletion.
                } catch (...) {
                    reply.size = 0x2003; // Never replay an ambiguous commit.
                }
            }
        }
        // Nonblocking, one response only. Never retain an input payload.
        send(client.fd, &reply, sizeof(reply), MSG_DONTWAIT | MSG_NOSIGNAL);
        std::fill(packet.begin(), packet.end(), 0);
        client.close();
    }

    void accept() noexcept {
        int fd = accept4(listener_, nullptr, nullptr, SOCK_CLOEXEC | SOCK_NONBLOCK);
        if (fd < 0) return;
        try {
            struct ucred peer {};
            socklen_t size = sizeof(peer);
            if (getsockopt(fd, SOL_SOCKET, SO_PEERCRED, &peer, &size) || size != sizeof(peer) || peer.uid != getuid()) {
                ::close(fd); return;
            }
            for (auto &slot : clients_) {
                if (slot && slot->fd >= 0) continue;
                slot = std::make_unique<Client>();
                slot->fd = fd; fd = -1;
                slot->epoch = focusEpoch_;
                slot->expires = fcitx::now(CLOCK_MONOTONIC) + 1000000;
                Client *client = slot.get();
                slot->watch = instance_->eventLoop().addIOEvent(client->fd, fcitx::IOEventFlag::In,
                    [this, client](fcitx::EventSourceIO *, int, fcitx::IOEventFlags) {
                        receive(*client); return true;
                    });
                return;
            }
        } catch (...) {}
        if (fd >= 0) ::close(fd);
    }

    void cleanup() noexcept {
        listenerWatch_.reset(); timer_.reset(); focusWatches_.clear();
        for (auto &client : clients_) client.reset();
        if (listener_ >= 0) ::close(listener_);
        listener_ = -1;
        struct stat current {};
        if (boundKnown_ && !lstat(endpoint_.c_str(), &current) &&
            current.st_dev == bound_.st_dev && current.st_ino == bound_.st_ino)
            unlink(endpoint_.c_str());
        boundKnown_ = false;
        if (lock_ >= 0) ::close(lock_);
        lock_ = -1;
    }
public:
    explicit NativeIme(fcitx::Instance *instance) : instance_(instance) {
        try {
            auto *user = getpwuid(getuid());
            if (!getuid() || !user) throw std::runtime_error("IME requires user session");
            const char *override = getenv("UURB_IME_SOCKET");
            endpoint_ = override ? override : std::string(user->pw_dir) + "/.local/state/uurb/ime.sock";
            sockaddr_un address {}; address.sun_family = AF_UNIX;
            auto slash = endpoint_.rfind('/');
            struct stat parent {}, existing {};
            if (endpoint_.empty() || endpoint_[0] != '/' || endpoint_.size() >= sizeof(address.sun_path) ||
                slash == 0 || lstat(endpoint_.substr(0, slash).c_str(), &parent) || !S_ISDIR(parent.st_mode) ||
                parent.st_uid != getuid() || parent.st_mode & 077)
                throw std::runtime_error("IME requires private endpoint parent");
            lock_ = open((endpoint_ + ".lock").c_str(), O_RDWR | O_CREAT | O_CLOEXEC | O_NOFOLLOW | O_NONBLOCK, 0600);
            struct stat lockInfo {};
            if (lock_ < 0 || fstat(lock_, &lockInfo) || !S_ISREG(lockInfo.st_mode) ||
                lockInfo.st_uid != getuid() || lockInfo.st_mode & 077 || flock(lock_, LOCK_EX | LOCK_NB))
                throw std::runtime_error("IME endpoint owner already running or lock invalid");
            strcpy(address.sun_path, endpoint_.c_str());
            if (!lstat(endpoint_.c_str(), &existing)) {
                if (!S_ISSOCK(existing.st_mode) || existing.st_uid != getuid() || existing.st_mode & 077)
                    throw std::runtime_error("IME refuses unrelated endpoint");
                const int probe = socket(AF_UNIX, SOCK_SEQPACKET | SOCK_CLOEXEC | SOCK_NONBLOCK, 0);
                if (probe < 0) throw std::runtime_error("IME endpoint check unavailable");
                const int result = connect(probe, reinterpret_cast<sockaddr *>(&address), sizeof(address));
                const int failure = errno;
                ::close(probe);
                struct stat current {};
                // Only a definitive refusal at the same inode permits reclaim.
                // Success, timeout, queue pressure and identity races do not.
                if (!result || failure != ECONNREFUSED || lstat(endpoint_.c_str(), &current) ||
                    current.st_dev != existing.st_dev || current.st_ino != existing.st_ino || unlink(endpoint_.c_str()))
                    throw std::runtime_error("IME endpoint still live or changed");
            } else if (errno != ENOENT) {
                throw std::runtime_error("IME endpoint check failed");
            }
            listener_ = socket(AF_UNIX, SOCK_SEQPACKET | SOCK_CLOEXEC | SOCK_NONBLOCK, 0);
            // Parent is owner-only even before the socket's explicit chmod.
            if (listener_ < 0 || bind(listener_, reinterpret_cast<sockaddr *>(&address), sizeof(address)))
                throw std::runtime_error("IME endpoint bind failed");
            if (lstat(endpoint_.c_str(), &bound_)) throw std::runtime_error("IME endpoint stat failed");
            boundKnown_ = true;
            if (chmod(endpoint_.c_str(), 0600) || listen(listener_, 4))
                throw std::runtime_error("IME endpoint listen failed");
            for (auto type : {fcitx::EventType::InputContextFocusIn, fcitx::EventType::InputContextFocusOut})
                focusWatches_.push_back(instance_->watchEvent(type, fcitx::EventWatcherPhase::PreInputMethod,
                    [this](fcitx::Event &) { ++focusEpoch_; }));
            listenerWatch_ = instance_->eventLoop().addIOEvent(listener_, fcitx::IOEventFlag::In,
                [this](fcitx::EventSourceIO *, int, fcitx::IOEventFlags) { accept(); return true; });
            timer_ = instance_->eventLoop().addTimeEvent(CLOCK_MONOTONIC, fcitx::now(CLOCK_MONOTONIC) + 250000, 0,
                [this](fcitx::EventSourceTime *timer, uint64_t now) {
                    for (auto &client : clients_)
                        if (client && client->fd >= 0 && now >= client->expires) client->close();
                    timer->setTime(now + 250000); timer->setOneShot(); return true;
                });
        } catch (...) { cleanup(); throw; }
    }
    ~NativeIme() override { cleanup(); }
};

class Factory final : public fcitx::AddonFactory {
    fcitx::AddonInstance *create(fcitx::AddonManager *manager) override {
        return new NativeIme(manager->instance());
    }
};
} // namespace
FCITX_ADDON_FACTORY(Factory)
