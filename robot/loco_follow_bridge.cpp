// loco_follow_bridge.cpp
// Tiny stdin->Booster-loco bridge for the K1 "follow marker" behaviour.
//
// Compiled ON the robot against the Booster SDK. Reads one command per line on
// stdin, drives locomotion, and prints "OK <cmd> <code>" / "ERR <msg>" per line.
//
// Commands (one per line, whitespace-separated):
//   ping              -> GetMode (read-only) liveness check. Verifies the loco
//                        service answers in ANY mode WITHOUT motion. OK ping <code> (0 == reachable).
//   prep              -> ChangeMode(kPrepare)
//   walk              -> ChangeMode(kWalking)
//   damp              -> ChangeMode(kDamping)
//   v <vx> <vy> <vyaw>-> MoveCommand(vx,vy,vyaw) fire-and-forget (the 10Hz stream).
//                        Values are HARD-clamped here as a last line of defense.
//   stop              -> MoveCommand(0,0,0) fire-and-forget (immediate halt).
//   quit              -> graceful shutdown (stop + return to PREP) then exit 0.
//
// COMMAND-STALENESS WATCHDOG: a DEDICATED thread zeroes velocity if no fresh 'v'
// arrives for STALE_MS while a stream is active, and commands kPrepare after
// STALE_PREP_MS. This covers a hung-but-CONNECTED driver (no EOF) holding the last
// MoveCommand -- the one runaway the EOF latch and the python loop-top watchdogs
// cannot catch. It runs on its own thread so a getline/stdout block can't disable it.
//
// On EOF (ssh/pipe closed), SIGINT/SIGTERM, or "quit": MoveCommand(0,0,0) then
// ChangeMode(kPrepare), so the robot never keeps walking when the driver dies.
//
// Build (matches the working enable_camera recipe):
//   g++ -std=c++17 loco_follow_bridge.cpp \
//       -I /home/booster/Workspace/booster_robotics_sdk/include \
//       /home/booster/Workspace/booster_robotics_sdk/lib/aarch64/libbooster_robotics_sdk.a \
//       -lfastrtps -lfastcdr -lpthread -o loco_follow_bridge
//
// Run:   ./loco_follow_bridge [iface]      (iface default 127.0.0.1)

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

#include <booster/robot/channel/channel_factory.hpp>
#include <booster/robot/b1/b1_loco_client.hpp>

using namespace booster::robot;

// ---------------------------------------------------------------------------
// HARD safety clamps. These mirror follow_marker.py but are the LAST line of
// defense: even if python is buggy or a malformed line slips through, the robot
// can never be commanded past these. Kept deliberately conservative.
// ---------------------------------------------------------------------------
static const float VX_MIN   = -0.10f, VX_MAX   = 0.30f;   // forward  m/s
static const float VY_MIN   = -0.15f, VY_MAX   = 0.15f;   // lateral  m/s
static const float VYAW_MIN = -0.40f, VYAW_MAX = 0.40f;   // angular  rad/s

static inline float clampf(float v, float lo, float hi) {
    if (std::isnan(v)) return 0.0f;     // NaN -> 0, never propagate garbage
    return v < lo ? lo : (v > hi ? hi : v);
}

// ---------------------------------------------------------------------------
// Global client + signal-driven shutdown. We must be able to issue a stop from
// a signal handler, so the client pointer and a flag are global/atomic. Inside
// the handler we only do async-signal-unsafe-minimal work guarded by a flag,
// then let main() perform the real cleanup; but because an EOF may never come
// after a kill, we also attempt a direct best-effort stop here.
// ---------------------------------------------------------------------------
static b1::B1LocoClient* g_client = nullptr;
static std::atomic<bool> g_stop_requested{false};
static std::atomic<bool> g_cleaned{false};

// ---------------------------------------------------------------------------
// COMMAND-STALENESS WATCHDOG state. MoveCommand is fire-and-forget and the loco
// service HOLDS the last commanded velocity, so a python driver that hangs while
// the ssh pipe stays OPEN (no EOF) leaves the command loop blocked in getline and
// the robot walking on the last 'v'. The frame-stall / max-seconds watchdogs sit
// at the TOP of the python loop and so cannot fire during a hang INSIDE it. Only a
// dedicated thread keyed on the age of the last 'v' covers this. Two tiers mirror
// the python _stand floor: zero velocity at STALE_MS (stay in gait; the service
// holds the zero), then ChangeMode(kPrepare) at STALE_PREP_MS for a sustained hang.
// Defaults are CONSERVATIVE -- tune on-robot to the measured inter-'v' jitter (too
// tight a STALE_MS stutter-stops a healthy follow; too loose lengthens the runaway).
// ---------------------------------------------------------------------------
static const int64_t STALE_MS      = 400;    // no fresh 'v' this long while moving -> zero velocity
static const int64_t STALE_PREP_MS = 1000;   // sustained stale -> stop + kPrepare (RECOVERABLE: a
                                             // fresh 'v' re-enters kWalking -- see the 'v' handler).
                                             // P4.4 (2026-07-08): LOWERED 800/3000 -> 400/1000. These
                                             // were RAISED to 800/3000 when the un-pinned, un-warmed
                                             // Jetson loop was slow; P4.2 fixed both -- GPU pinned
                                             // (jetson_clocks) + YOLO first-inference warmup moved OFF
                                             // the loop (P4.2a) -- so the driving loop now measures
                                             // p99~122ms / steady max~155ms (docs/LOOP_BASELINE.md).
                                             // 400ms keeps ~2.6x margin over the steady max (no healthy
                                             // stutter-stop) while HALVING the tier-1 runaway window:
                                             // 800->400ms = 14.4->7.2cm at vx_max 0.18 m/s. Still tune
                                             // ABOVE the max in-loop dt in k1_follow.err. VERIFY ON
                                             // ROBOT: ZERO 'WATCHDOG stale' lines on a healthy follow;
                                             // if any fire, the loop tail exceeds 400ms -> loosen.

static std::mutex g_loco_mutex;              // serialises every MoveCommand/ChangeMode
static std::atomic<int64_t> g_last_v_ms{0};  // steady-clock ms of the last accepted 'v'
static std::atomic<bool> g_v_active{false};  // armed only while a velocity stream is live
static std::atomic<bool> g_zeroed{false};    // tier-1 fired (cleared by the next 'v')
static std::atomic<bool> g_prepped{false};   // tier-2 fired (cleared by the next 'v')
static std::atomic<bool> g_shutdown{false};  // stop the watchdog thread

static inline int64_t now_ms() {
    return std::chrono::duration_cast<std::chrono::milliseconds>(
        std::chrono::steady_clock::now().time_since_epoch()).count();
}

// ---------------------------------------------------------------------------
// OPERATOR-HEARTBEAT DEADMAN (untethered). When K1_REQUIRE_HB is set, motion is
// allowed ONLY while a fresh operator heartbeat is present: the operator's relay
// touches K1_HB_FILE (default /tmp/k1_hb) at ~10 Hz and its mtime age is the liveness
// signal. This is INDEPENDENT of the velocity stream -- a perception loop that keeps
// streaming 'v' after the operator is gone (phone dead / out of range / app crash) is
// stopped HERE, which the v-watchdog (a healthy stream) cannot do. Tiers are TIGHTER
// than the 'v' tiers because a heartbeat is pure liveness with no perception jitter.
// DEFAULT OFF -> tethered runs byte-identical (no env => hb_fresh() always true).
// Fail-CLOSED: a missing/unreadable heartbeat file reads as STALE (stop), never fresh.
// ---------------------------------------------------------------------------
static std::atomic<bool> g_hb_required{false};   // from K1_REQUIRE_HB at startup
static std::string g_hb_file = "/tmp/k1_hb";     // from K1_HB_FILE at startup
static const int64_t HB_STALE_MS = 400;          // no fresh heartbeat this long -> zero velocity
static const int64_t HB_PREP_MS  = 1500;         // sustained -> stop + kPrepare

static inline int64_t hb_age_ms() {
    // Age (ms) of the heartbeat file vs CLOCK_REALTIME (file mtime is wall-clock). A
    // missing/unreadable file -> huge age => STALE (fail closed). A FUTURE mtime (clock
    // skew / NTP step-back / leftover future-dated file) ALSO => STALE -- a future date
    // must never read as fresh, or the deadman silently disables itself.
    struct stat st;
    if (stat(g_hb_file.c_str(), &st) != 0) return INT64_MAX / 4;
    struct timespec tnow;
    clock_gettime(CLOCK_REALTIME, &tnow);
    int64_t now_rt = (int64_t)tnow.tv_sec * 1000 + tnow.tv_nsec / 1000000;
    int64_t mt     = (int64_t)st.st_mtim.tv_sec * 1000 + st.st_mtim.tv_nsec / 1000000;
    int64_t age = now_rt - mt;
    return age < 0 ? INT64_MAX / 4 : age;   // future mtime -> STALE (fail closed), never fresh
}
static inline bool hb_fresh() {
    return !g_hb_required.load() || hb_age_ms() <= HB_STALE_MS;
}

// Every loco call goes through these so the watchdog thread and the command loop
// can never race the Fast-DDS writer. (safe_shutdown stays UNLOCKED on purpose --
// it is the terminal best-effort path, also reachable from the async signal handler
// where taking a mutex could deadlock; the watchdog is stopped before the normal
// cleanup runs safe_shutdown, and the signal path _exit()s immediately.)
static void loco_move(float vx, float vy, float vyaw) {
    std::lock_guard<std::mutex> lk(g_loco_mutex);
    if (g_client) g_client->MoveCommand(vx, vy, vyaw);
}
static int32_t loco_mode(RobotMode m) {
    std::lock_guard<std::mutex> lk(g_loco_mutex);
    return g_client ? g_client->ChangeMode(m) : -1;
}

static void safe_shutdown() {
    // Idempotent: only ever runs the loco stop+prep once.
    bool expected = false;
    if (!g_cleaned.compare_exchange_strong(expected, true)) return;
    g_shutdown.store(true);      // stop the staleness watchdog
    g_v_active.store(false);
    if (g_client) {
        // Fire-and-forget zero velocity FIRST (fast, no response wait), then
        // command PREP so the robot stands safely instead of holding a gait.
        g_client->MoveCommand(0.0f, 0.0f, 0.0f);
        g_client->ChangeMode(RobotMode::kPrepare);
    }
}

static void on_signal(int) {
    // Keep handler minimal & async-safe-ish: request stop and attempt the
    // best-effort loco halt. SDK calls aren't formally async-signal-safe, but a
    // walking humanoid losing its driver is the worse outcome, so we try.
    g_stop_requested.store(true);
    safe_shutdown();
    // Restore default and re-raise so exit status reflects the signal.
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
// Watchdog thread. Acts ONLY while a velocity stream is armed (g_v_active), so it
// stays silent through bringup, prep, and any deliberate stand. The SAFE loco action
// is issued BEFORE the (best-effort) stdout note, so even a blocked emit cannot stop
// the robot being safed. exchange(true) makes each tier fire exactly once until the
// next 'v' re-arms.
// ---------------------------------------------------------------------------
static void watchdog_loop() {
    while (!g_shutdown.load()) {
        std::this_thread::sleep_for(std::chrono::milliseconds(20));   // ~50 Hz check
        if (g_shutdown.load() || g_cleaned.load()) break;
        if (!g_v_active.load()) continue;                             // disarmed
        // OPERATOR-HEARTBEAT deadman: checked FIRST, TIGHTER tiers than 'v'. Shares the
        // g_zeroed/g_prepped one-shot flags so the prep->'v' recovery still works -- but
        // the 'v' handler will NOT re-walk while the heartbeat is stale.
        if (g_hb_required.load()) {
            int64_t hba = hb_age_ms();
            if (hba > HB_PREP_MS && !g_prepped.exchange(true)) {
                loco_move(0.0f, 0.0f, 0.0f);                          // SAFE ACTION FIRST
                loco_mode(RobotMode::kPrepare);
                g_v_active.store(false);                              // safed -> disarm
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
            g_v_active.store(false);                                  // safed -> disarm
            emit("WATCHDOG stale " + std::to_string(age) + "ms -> stop+kPrepare");
        } else if (age > STALE_MS && !g_zeroed.exchange(true)) {
            loco_move(0.0f, 0.0f, 0.0f);                              // SAFE ACTION FIRST
            emit("WATCHDOG stale " + std::to_string(age) + "ms -> zero velocity");
        }
    }
}

int main(int argc, char** argv) {
    std::string iface = (argc > 1 && argv[1][0] != '\0') ? argv[1] : "127.0.0.1";

    // Unbuffered-ish stdout so the app's line reader sees responses promptly.
    std::cout.setf(std::ios::unitbuf);

    // Trap signals BEFORE we can possibly start walking.
    std::signal(SIGINT,  on_signal);
    std::signal(SIGTERM, on_signal);
    // Don't die if the read end / pipe is gone mid-write; handle EOF in the loop.
    std::signal(SIGPIPE, SIG_IGN);

    // --- Bring up the SDK channel + loco client ----------------------------
    // The Booster SDK prints channel-init chatter ("ChannelSubscriber::InitChannel
    // ...") to stdout during Init(). follow_person merges this bridge's stderr into
    // stdout and parses the stream line-by-line for the OK/ERR protocol, AND it never
    // consumes a handshake -- so that startup chatter becomes the first line it reads
    // for "ping" and aborts DRIVE. Swallow ALL SDK output during bringup by pointing
    // fd1+fd2 at /dev/null, then restore a CLEAN stdout for the protocol. fd2 stays on
    // /dev/null so any later SDK chatter can never reach the protocol stream.
    std::cout.flush(); fflush(stdout); fflush(stderr);
    int saved_out = dup(1);
    int devnull   = open("/dev/null", O_WRONLY);
    if (devnull >= 0) { dup2(devnull, 1); dup2(devnull, 2); }

    b1::B1LocoClient client;
    bool init_ok = true;
    std::string init_err;
    try {
        ChannelFactory::Instance()->Init(0, iface);
        client.Init();
    } catch (const std::exception& e) {
        init_ok = false; init_err = e.what();
    } catch (...) {
        init_ok = false; init_err = "unknown";
    }

    // Restore a clean protocol stdout (fd2 stays swallowed on /dev/null).
    fflush(stdout); fflush(stderr);
    if (saved_out >= 0) dup2(saved_out, 1);
    if (devnull   >= 0) close(devnull);
    if (saved_out >= 0) close(saved_out);

    if (!init_ok) {
        emit(std::string("ERR init ") + init_err);
        return 1;
    }
    g_client = &client;

    emit("OK ready 0");   // handshake: first clean line follow_person sees

    // Operator-heartbeat deadman config (untethered). Default OFF -> tethered byte-identical.
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

    // Start the command-staleness watchdog now that the loco client is live.
    std::thread watchdog(watchdog_loop);

    // --- Command loop ------------------------------------------------------
    // Wrapped so an SDK/stream exception falls through to the watchdog-join +
    // safe_shutdown cleanup below, rather than std::terminate-ing via the joinable
    // watchdog thread's destructor (which would leave the robot un-safed).
    std::string line;
    try {
    while (std::getline(std::cin, line)) {
        if (g_stop_requested.load()) break;

        std::istringstream iss(line);
        std::string cmd;
        if (!(iss >> cmd)) {
            // Blank line: ignore quietly (keeps the stream tolerant of newlines).
            continue;
        }
        // lowercase the command token
        for (char& c : cmd) c = (char)std::tolower((unsigned char)c);

        if (cmd == "v") {
            float vx = 0.0f, vy = 0.0f, vyaw = 0.0f;
            if (!(iss >> vx >> vy >> vyaw)) {
                // Malformed velocity -> safest action is to stop, not guess.
                loco_move(0.0f, 0.0f, 0.0f);
                emit("ERR v parse -> stopped");
                continue;
            }
            vx   = clampf(vx,   VX_MIN,   VX_MAX);
            vy   = clampf(vy,   VY_MIN,   VY_MAX);
            vyaw = clampf(vyaw, VYAW_MIN, VYAW_MAX);
            // OPERATOR-HEARTBEAT GATE: while the heartbeat is stale, refuse to move AND
            // refuse to clear the prep->walk recovery edge -- so a fresh 'v' from a still-
            // streaming perception loop can neither drive nor re-walk until the operator
            // is present again. No-op when K1_REQUIRE_HB is unset (tethered).
            if (!hb_fresh()) {
                loco_move(0.0f, 0.0f, 0.0f);
                emit("HB-STALE v held");
                continue;
            }
            // RECOVERY: if the staleness watchdog had stood the robot (tier-2 kPrepare)
            // on a stale gap, a fresh 'v' proves the driver is alive again -> re-enter
            // kWalking BEFORE moving. Without this the robot stays PERMANENTLY stood: the
            // python side never learns the bridge prepped it (it doesn't read this stdout),
            // so it keeps streaming 'v' that the loco service ignores in kPrepare. The
            // exchange(false) makes this fire only on the prep->'v' edge. Velocity stays
            // HARD-clamped; if the driver is truly dead no 'v' arrives and we stay stood.
            if (g_prepped.exchange(false)) {
                loco_mode(RobotMode::kWalking);
            }
            // Fire-and-forget: no response wait, suitable for the 10Hz stream.
            loco_move(vx, vy, vyaw);
            // Arm / refresh the staleness watchdog on every accepted 'v'.
            g_last_v_ms.store(now_ms());
            g_zeroed.store(false);
            g_v_active.store(true);
            // Echo the CLAMPED values so the app log shows what actually went out.
            char buf[96];
            std::snprintf(buf, sizeof(buf), "OK v %.3f %.3f %.3f 0", vx, vy, vyaw);
            emit(buf);
        }
        else if (cmd == "stop") {
            loco_move(0.0f, 0.0f, 0.0f);   // immediate halt, no wait
            g_v_active.store(false);       // explicit halt -> disarm the watchdog
            emit("OK stop 0");
        }
        else if (cmd == "ping") {
            // Read-only liveness check: GetMode answers in ANY mode
            // (damping/prepare/walking) WITHOUT motion, so DRIVE can verify the
            // loco service from a COLD robot before sending prep/walk.
            // (Move(0,0,0) was wrong here: it needs the robot ALREADY in Walking
            // mode and returns 400 from a cold robot, aborting DRIVE prematurely.)
            b1::GetModeResponse mode_resp;
            int32_t code;
            {   std::lock_guard<std::mutex> lk(g_loco_mutex);
                code = client.GetMode(mode_resp);   }
            emit("OK ping " + std::to_string(code));
        }
        else if (cmd == "prep") {
            int32_t code = loco_mode(RobotMode::kPrepare);
            g_v_active.store(false);   // standing -> no velocity stream expected -> disarm
            emit("OK prep " + std::to_string(code));
        }
        else if (cmd == "walk") {
            int32_t code = loco_mode(RobotMode::kWalking);
            emit("OK walk " + std::to_string(code));
        }
        else if (cmd == "damp") {
            int32_t code = loco_mode(RobotMode::kDamping);
            g_v_active.store(false);   // damping -> disarm
            emit("OK damp " + std::to_string(code));
        }
        else if (cmd == "quit" || cmd == "exit") {
            emit("OK quit 0");
            break;   // fall through to cleanup
        }
        else {
            emit("ERR unknown cmd " + cmd);
        }
    }
    } catch (...) {
        // Any SDK/stream throw still safes the robot via the cleanup below.
    }

    // EOF (ssh/pipe closed), "quit", or stop-requested all land here. Stop the
    // watchdog and JOIN it before the final safe_shutdown so it can't race the
    // teardown or outlive the stack-local loco client. (The signal path _exit()s
    // and never reaches here, so a joinable thread can't trip std::terminate.)
    g_shutdown.store(true);
    if (watchdog.joinable()) watchdog.join();
    safe_shutdown();
    emit("OK shutdown 0");
    return 0;
}
