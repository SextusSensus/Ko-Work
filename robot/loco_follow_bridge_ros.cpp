// loco_follow_bridge_ros.cpp
// TRANSPORT-SWAPPED twin of loco_follow_bridge.cpp for firmware where the raw SDK
// RPC channel is dead (observed 2026-09-02: BoosterRos2 build 2026-05 answers loco
// RPC ONLY via the ROS2 service /booster_rpc_service; every B1LocoClient call
// times out with 100 while ros2 service call GetMode answers instantly -- and the
// robot's own driving stack, k1_control_py/cmd_vel_translator.py, uses exactly
// this service). SAME stdin protocol, SAME hard clamps, SAME staleness watchdog +
// heartbeat deadman, SAME fail-closed shutdown; ONLY the loco transport differs:
//   B1LocoClient::Move(vx,vy,vyaw)   -> RpcService api_id 2001 {"vx","vy","vyaw"}
//   B1LocoClient::ChangeMode(m)      -> RpcService api_id 2000 {"mode":0|1|2}
//   B1LocoClient::GetMode(resp)      -> RpcService api_id 2017 ""
// (ids/bodies from the SDK's own b1_loco_api.hpp; damp=0 prep=1 walk=2 from
// robot_shared.hpp; verified read-only against the live service before writing.)
//
// Build (needs ROS2 humble + the booster_interface install sourced):
//   cmake -B build -S bridge_cpp -DBRIDGE_ROS=ON   (see bridge_cpp/CMakeLists.txt)
// Run:   ./loco_follow_bridge_ros [iface-ignored]
//   (argv[1] kept for CLI compatibility with the SDK bridge; ROS2 transport
//    configuration comes from the sourced environment, not an iface argument.)

#include <atomic>
#include <cctype>
#include <cmath>
#include <csignal>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <iostream>
#include <sstream>
#include <string>
#include <thread>
#include <mutex>
#include <chrono>
#include <unistd.h>
#include <fcntl.h>
#include <sys/stat.h>   // operator-heartbeat file mtime (untethered deadman)
#include <ctime>
#include <cstdint>

#include <rclcpp/rclcpp.hpp>
#include <booster_interface/srv/rpc_service.hpp>

// LocoApiId values from the SDK's b1_loco_api.hpp; RobotMode from robot_shared.hpp.
static const int32_t API_CHANGE_MODE = 2000;
static const int32_t API_MOVE        = 2001;
static const int32_t API_GET_MODE    = 2017;
enum class RobotMode { kDamping = 0, kPrepare = 1, kWalking = 2 };

// ---------------------------------------------------------------------------
// HARD safety clamps -- identical to loco_follow_bridge.cpp (the LAST line of
// defense: even if python is buggy or a malformed line slips through, the robot
// can never be commanded past these).
// ---------------------------------------------------------------------------
static const float VX_MIN   = -0.10f, VX_MAX   = 0.30f;   // forward  m/s
static const float VY_MIN   = -0.15f, VY_MAX   = 0.15f;   // lateral  m/s
static const float VYAW_MIN = -0.40f, VYAW_MAX = 0.40f;   // angular  rad/s

static inline float clampf(float v, float lo, float hi) {
    if (std::isnan(v)) return 0.0f;     // NaN -> 0, never propagate garbage
    return v < lo ? lo : (v > hi ? hi : v);
}

// ---------------------------------------------------------------------------
// ROS2 service transport. One node + client, spun by a dedicated executor
// thread so responses complete while the command loop blocks in getline.
//   fire(): async, no response wait -- the 10Hz velocity stream + best-effort
//           shutdown path (the request datagram is written in the CALLER thread,
//           so it goes out even if we exit soon after; the callback only cleans
//           up the pending-request entry).
//   call(): async + bounded wait -- ping/prep/walk/damp, which need the code.
//           Returns the service status, or 100 (the SDK's own timeout code, which
//           the python side already understands) when nothing answers in time.
// ---------------------------------------------------------------------------
struct LocoSvc {
    rclcpp::Node::SharedPtr node;
    rclcpp::Client<booster_interface::srv::RpcService>::SharedPtr cli;
    // Constructed lazily in init(): an Executor's constructor needs the ALREADY-
    // INITIALIZED default context, so a member executor on a pre-init struct throws
    // RCLInvalidArgument from the member constructor -- outside any try -> terminate.
    std::unique_ptr<rclcpp::executors::SingleThreadedExecutor> exec;
    std::thread spinner;

    bool init() {
        node = rclcpp::Node::make_shared("loco_follow_bridge");
        cli  = node->create_client<booster_interface::srv::RpcService>("booster_rpc_service");
        exec = std::make_unique<rclcpp::executors::SingleThreadedExecutor>();
        exec->add_node(node);
        spinner = std::thread([this] { exec->spin(); });
        return cli->wait_for_service(std::chrono::seconds(8));   // fail closed if absent
    }
    void shutdown_spinner() {
        if (exec) exec->cancel();
        if (spinner.joinable()) spinner.join();
    }
    static booster_interface::srv::RpcService::Request::SharedPtr make_req(int32_t api_id, const std::string& body) {
        auto req = std::make_shared<booster_interface::srv::RpcService::Request>();
        req->msg.api_id = api_id;
        req->msg.body   = body;
        return req;
    }
    void fire(int32_t api_id, const std::string& body) {
        if (!cli) return;
        using Fut = rclcpp::Client<booster_interface::srv::RpcService>::SharedFuture;
        cli->async_send_request(make_req(api_id, body), [](Fut) {});   // callback frees the entry
    }
    int32_t call(int32_t api_id, const std::string& body, int timeout_ms) {
        if (!cli) return -1;
        auto fut = cli->async_send_request(make_req(api_id, body));
        if (fut.wait_for(std::chrono::milliseconds(timeout_ms)) != std::future_status::ready) {
            cli->remove_pending_request(fut);
            return 100;   // kRpcStatusCodeTimeout -- same code the SDK path produced
        }
        return (int32_t)fut.get()->msg.status;
    }
    // A service that died mid-stream never answers the fire() requests -> their
    // pending entries would accumulate at 10Hz. Reap anything older than 10s
    // (sync call() timeouts are all <=8s, so this can never clip a live call()).
    void reap() {
        if (cli) cli->prune_requests_older_than(
            std::chrono::system_clock::now() - std::chrono::seconds(10));
    }
};

static std::string move_body(float vx, float vy, float vyaw) {
    char b[96];
    std::snprintf(b, sizeof(b), "{\"vx\":%.4f,\"vy\":%.4f,\"vyaw\":%.4f}", vx, vy, vyaw);
    return b;
}
static std::string mode_body(RobotMode m) {
    char b[24];
    std::snprintf(b, sizeof(b), "{\"mode\":%d}", (int)m);
    return b;
}

// ---------------------------------------------------------------------------
// Global transport + signal-driven shutdown (same shape as the SDK twin).
// ---------------------------------------------------------------------------
static LocoSvc* g_loco = nullptr;
static std::atomic<bool> g_stop_requested{false};
static std::atomic<bool> g_cleaned{false};

// ---------------------------------------------------------------------------
// COMMAND-STALENESS WATCHDOG state -- IDENTICAL tiers and semantics to
// loco_follow_bridge.cpp (P4.4 400/1000; see that file for the tuning history).
// ---------------------------------------------------------------------------
static const int64_t STALE_MS      = 400;
static const int64_t STALE_PREP_MS = 1000;

static std::mutex g_loco_mutex;              // serialises every Move/ChangeMode
static std::atomic<int64_t> g_last_v_ms{0};
static std::atomic<bool> g_v_active{false};
static std::atomic<bool> g_zeroed{false};
static std::atomic<bool> g_prepped{false};
static std::atomic<bool> g_shutdown{false};

static inline int64_t now_ms() {
    return std::chrono::duration_cast<std::chrono::milliseconds>(
        std::chrono::steady_clock::now().time_since_epoch()).count();
}

// ---------------------------------------------------------------------------
// OPERATOR-HEARTBEAT DEADMAN -- identical to loco_follow_bridge.cpp.
// ---------------------------------------------------------------------------
static std::atomic<bool> g_hb_required{false};
static std::string g_hb_file = "/tmp/k1_hb";
static const int64_t HB_STALE_MS = 400;
static const int64_t HB_PREP_MS  = 1500;

static inline int64_t hb_age_ms() {
    struct stat st;
    if (stat(g_hb_file.c_str(), &st) != 0) return INT64_MAX / 4;
    struct timespec tnow;
    clock_gettime(CLOCK_REALTIME, &tnow);
    int64_t now_rt = (int64_t)tnow.tv_sec * 1000 + tnow.tv_nsec / 1000000;
    int64_t mt     = (int64_t)st.st_mtim.tv_sec * 1000 + st.st_mtim.tv_nsec / 1000000;
    int64_t age = now_rt - mt;
    return age < 0 ? INT64_MAX / 4 : age;   // future mtime -> STALE (fail closed)
}
static inline bool hb_fresh() {
    return !g_hb_required.load() || hb_age_ms() <= HB_STALE_MS;
}

// Every loco call goes through these so the watchdog thread and the command loop
// can never race the transport. (safe_shutdown stays UNLOCKED on purpose -- same
// rationale as the SDK twin: terminal best-effort path, signal-handler reachable.)
static void loco_move(float vx, float vy, float vyaw) {
    std::lock_guard<std::mutex> lk(g_loco_mutex);
    if (g_loco) g_loco->fire(API_MOVE, move_body(vx, vy, vyaw));
}
static int32_t loco_mode(RobotMode m) {
    std::lock_guard<std::mutex> lk(g_loco_mutex);
    return g_loco ? g_loco->call(API_CHANGE_MODE, mode_body(m), 5000) : -1;
}

static void safe_shutdown() {
    bool expected = false;
    if (!g_cleaned.compare_exchange_strong(expected, true)) return;
    g_shutdown.store(true);
    g_v_active.store(false);
    if (g_loco) {
        // Fire-and-forget zero velocity FIRST, then PREP -- the request datagrams are
        // written in this thread, so they go out even on the signal/_exit path. The
        // short sleep lets the transport flush before the process dies.
        g_loco->fire(API_MOVE, move_body(0.0f, 0.0f, 0.0f));
        g_loco->fire(API_CHANGE_MODE, mode_body(RobotMode::kPrepare));
        struct timespec ts{0, 60 * 1000 * 1000};   // 60 ms
        nanosleep(&ts, nullptr);
    }
}

static void on_signal(int) {
    g_stop_requested.store(true);
    safe_shutdown();
    std::signal(SIGINT, SIG_DFL);
    std::signal(SIGTERM, SIG_DFL);
    _exit(0);
}

// ---------------------------------------------------------------------------
static void emit(const std::string& line) {
    std::cout << line << "\n";
    std::cout.flush();
}

// ---------------------------------------------------------------------------
// Watchdog thread -- IDENTICAL to loco_follow_bridge.cpp (safe action first,
// one-shot tiers, hb checked before v-staleness), plus the pending-request reap.
// ---------------------------------------------------------------------------
static void watchdog_loop() {
    int64_t last_reap = now_ms();
    while (!g_shutdown.load()) {
        std::this_thread::sleep_for(std::chrono::milliseconds(20));   // ~50 Hz check
        if (g_shutdown.load() || g_cleaned.load()) break;
        if (now_ms() - last_reap > 1000) {
            last_reap = now_ms();
            std::lock_guard<std::mutex> lk(g_loco_mutex);
            if (g_loco) g_loco->reap();
        }
        if (!g_v_active.load()) continue;
        if (g_hb_required.load()) {
            int64_t hba = hb_age_ms();
            if (hba > HB_PREP_MS && !g_prepped.exchange(true)) {
                loco_move(0.0f, 0.0f, 0.0f);                          // SAFE ACTION FIRST
                loco_mode(RobotMode::kPrepare);
                g_v_active.store(false);
                emit("WATCHDOG hb-stale " + std::to_string(hba) + "ms -> stop+kPrepare");
                continue;
            } else if (hba > HB_STALE_MS && !g_zeroed.exchange(true)) {
                loco_move(0.0f, 0.0f, 0.0f);                          // SAFE ACTION FIRST
                emit("WATCHDOG hb-stale " + std::to_string(hba) + "ms -> zero velocity");
                continue;
            }
        }
        int64_t age = now_ms() - g_last_v_ms.load();
        if (age > STALE_PREP_MS && !g_prepped.exchange(true)) {
            loco_move(0.0f, 0.0f, 0.0f);                              // SAFE ACTION FIRST
            loco_mode(RobotMode::kPrepare);
            g_v_active.store(false);
            emit("WATCHDOG stale " + std::to_string(age) + "ms -> stop+kPrepare");
        } else if (age > STALE_MS && !g_zeroed.exchange(true)) {
            loco_move(0.0f, 0.0f, 0.0f);                              // SAFE ACTION FIRST
            emit("WATCHDOG stale " + std::to_string(age) + "ms -> zero velocity");
        }
    }
}

int main(int argc, char** argv) {
    (void)argc; (void)argv;   // iface argument accepted-and-ignored (ROS2 env decides)

    std::cout.setf(std::ios::unitbuf);

    std::signal(SIGINT,  on_signal);
    std::signal(SIGTERM, on_signal);
    std::signal(SIGPIPE, SIG_IGN);

    // Swallow rclcpp bringup chatter exactly like the SDK twin swallowed the
    // Fast-DDS chatter: the python side parses this stream line-by-line and the
    // first clean line it may see must be the "OK ready 0" handshake. fd2 stays
    // on /dev/null forever so later ROS logging can never corrupt the protocol.
    std::cout.flush(); fflush(stdout); fflush(stderr);
    int saved_out = dup(1);
    int devnull   = open("/dev/null", O_WRONLY);
    if (devnull >= 0) { dup2(devnull, 1); dup2(devnull, 2); }

    LocoSvc svc;
    bool init_ok = true;
    std::string init_err;
    try {
        rclcpp::InitOptions opts;
        rclcpp::init(0, nullptr, opts, rclcpp::SignalHandlerOptions::None);  // OUR handlers keep control
        init_ok = svc.init();
        if (!init_ok) init_err = "loco service /booster_rpc_service not available (8s)";
    } catch (const std::exception& e) {
        init_ok = false; init_err = e.what();
    } catch (...) {
        init_ok = false; init_err = "unknown";
    }

    fflush(stdout); fflush(stderr);
    if (saved_out >= 0) dup2(saved_out, 1);
    if (devnull   >= 0) close(devnull);
    if (saved_out >= 0) close(saved_out);

    if (!init_ok) {
        emit(std::string("ERR init ") + init_err);   // fail CLOSED: no service -> no drive
        return 1;
    }
    g_loco = &svc;

    emit("OK ready 0");   // handshake: first clean line follow_person sees

    {
        const char* req = std::getenv("K1_REQUIRE_HB");
        g_hb_required.store(req && req[0] && req[0] != '0');
        const char* hbf = std::getenv("K1_HB_FILE");
        if (hbf && hbf[0]) g_hb_file = hbf;
        if (g_hb_required.load())
            emit("OK hb-required 1 file=" + g_hb_file
                 + " stale=" + std::to_string(HB_STALE_MS)
                 + " prep=" + std::to_string(HB_PREP_MS));
    }

    std::thread watchdog(watchdog_loop);

    // --- Command loop: IDENTICAL protocol to loco_follow_bridge.cpp ---------
    std::string line;
    try {
    while (std::getline(std::cin, line)) {
        if (g_stop_requested.load()) break;

        std::istringstream iss(line);
        std::string cmd;
        if (!(iss >> cmd)) continue;
        for (char& c : cmd) c = (char)std::tolower((unsigned char)c);

        if (cmd == "v") {
            float vx = 0.0f, vy = 0.0f, vyaw = 0.0f;
            if (!(iss >> vx >> vy >> vyaw)) {
                loco_move(0.0f, 0.0f, 0.0f);
                emit("ERR v parse -> stopped");
                continue;
            }
            vx   = clampf(vx,   VX_MIN,   VX_MAX);
            vy   = clampf(vy,   VY_MIN,   VY_MAX);
            vyaw = clampf(vyaw, VYAW_MIN, VYAW_MAX);
            if (!hb_fresh()) {
                loco_move(0.0f, 0.0f, 0.0f);
                emit("HB-STALE v held");
                continue;
            }
            if (g_prepped.exchange(false)) {
                loco_mode(RobotMode::kWalking);   // prep->'v' recovery edge (see SDK twin)
            }
            loco_move(vx, vy, vyaw);              // fire-and-forget, 10Hz stream
            g_last_v_ms.store(now_ms());
            g_zeroed.store(false);
            g_v_active.store(true);
            char buf[96];
            std::snprintf(buf, sizeof(buf), "OK v %.3f %.3f %.3f 0", vx, vy, vyaw);
            emit(buf);
        }
        else if (cmd == "stop") {
            loco_move(0.0f, 0.0f, 0.0f);
            g_v_active.store(false);
            emit("OK stop 0");
        }
        else if (cmd == "ping") {
            // Read-only liveness check: GetMode answers in ANY mode without motion.
            int32_t code;
            {   std::lock_guard<std::mutex> lk(g_loco_mutex);
                code = g_loco->call(API_GET_MODE, "", 8000);   }
            emit("OK ping " + std::to_string(code));
        }
        else if (cmd == "prep") {
            int32_t code = loco_mode(RobotMode::kPrepare);
            g_v_active.store(false);
            emit("OK prep " + std::to_string(code));
        }
        else if (cmd == "walk") {
            int32_t code = loco_mode(RobotMode::kWalking);
            emit("OK walk " + std::to_string(code));
        }
        else if (cmd == "damp") {
            int32_t code = loco_mode(RobotMode::kDamping);
            g_v_active.store(false);
            emit("OK damp " + std::to_string(code));
        }
        else if (cmd == "quit" || cmd == "exit") {
            emit("OK quit 0");
            break;
        }
        else {
            emit("ERR unknown cmd " + cmd);
        }
    }
    } catch (...) {
        // Any transport/stream throw still safes the robot via the cleanup below.
    }

    g_shutdown.store(true);
    if (watchdog.joinable()) watchdog.join();
    safe_shutdown();
    g_loco = nullptr;
    svc.shutdown_spinner();
    rclcpp::shutdown();
    emit("OK shutdown 0");
    return 0;
}
