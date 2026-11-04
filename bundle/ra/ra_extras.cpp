/* ra_extras.cpp — RetroAchievements for GBRecomp (gbrecompiled) games.
 *
 * Plugs into the game through the framework's game-extras hooks
 * (runtime/include/game_extras.h): it implements game_post_frame(),
 * one of the per-hook strong symbols a game's extra TUs may provide
 * (runtime/src/game_defaults/ supplies the no-op the archive falls
 * back to when a game provides none). The engine additionally patches
 * the generated <game>_main.c to call game_post_frame(ctx) once per
 * completed frame — the framework declares the hook but, in the
 * current checkout, never calls it (only game_on_init has a call
 * site, in platform_sdl.cpp). All RA work therefore happens lazily on
 * the first post-frame call, on the emulation thread: no races, and
 * rom.cfg already names the ROM the launcher picked.
 *
 * Ported from the NESRecomp RA module (same config format, same
 * server plumbing, same callback-polarity fix); the GB differences:
 *
 *  - Identity: the ROM file the launcher picked (rom.cfg next to the
 *    exe, written by the launcher; falling back to a .gb/.gbc beside
 *    the exe or under roms/ — the runtime's own resolution order),
 *    hashed with rcheevos' Game Boy algorithm
 *    (rc_hash_generate_from_file; RC_CONSOLE_GAMEBOY = 4, or
 *    RC_CONSOLE_GAMEBOY_COLOR = 6 when the cart header's CGB flag is
 *    set — the vendored rc_consoles.h values).
 *  - Memory: the live machine via the runtime's gb_read8() CPU-bus
 *    read, exactly how RA addresses Game Boy memory.
 *  - Pop-ups: the GB runtime has NO toast channel (no toast/notify
 *    API exists in the runtime or the launcher seam), so the module
 *    draws the toast itself into the game's native 160x144
 *    framebuffer from game_post_frame — after the frame completes,
 *    before the platform presents it. Same 4-second lifetime and the
 *    same RA_DISABLE_POPUPS behaviour as the family (hiding pop-ups
 *    hides the login toast too: one channel).
 *  - Login: retroachievements.toml next to the exe (username +
 *    Connect api_token). Absent file = RA off, game runs normally.
 *
 * Hardcore note: rc_client is told the toml's hardcore flag, but
 * RetroAchievements counts hardcore only from approved clients; a
 * recomp client is an unknown emulator, so unlocks register as
 * Casual/softcore. Never spoof another client's identity.
 */
#include <algorithm>
#include <cctype>
#include <chrono>
#include <cstddef>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <filesystem>
#include <mutex>
#include <string>
#include <thread>
#include <vector>

#include "rc_client.h"
#include "rc_consoles.h"
#include "rc_hash.h"
#include "ra_http.h"
#include "ra_font.h"

#include "game_extras.h"
#include "gbrt.h"
#include "ppu.h" /* GB_SCREEN_WIDTH / GB_SCREEN_HEIGHT */

#ifdef _WIN32
#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#else
#include <unistd.h>
#include <limits.h>
#endif

namespace {

/* ------------------------------------------------------------------ */
/* Config: retroachievements.toml next to the exe                      */
/* ------------------------------------------------------------------ */

struct RaConfig {
    std::string username;
    std::string api_token;
    bool hardcore = false;
    bool present = false;
};

std::string ra_exe_dir() {
#ifdef _WIN32
    char path[MAX_PATH];
    DWORD n = GetModuleFileNameA(NULL, path, (DWORD)sizeof(path));
    if (n == 0 || n >= sizeof(path)) return std::string();
    std::string s(path);
    size_t slash = s.find_last_of("\\/");
    return (slash == std::string::npos) ? std::string() : s.substr(0, slash);
#else
    char path[PATH_MAX];
    ssize_t n = readlink("/proc/self/exe", path, sizeof(path) - 1);
    if (n <= 0) return std::string();
    path[n] = '\0';
    std::string s(path);
    size_t slash = s.find_last_of('/');
    return (slash == std::string::npos) ? std::string() : s.substr(0, slash);
#endif
}

std::string ra_trim(const std::string& s) {
    size_t a = 0, b = s.size();
    while (a < b && (s[a] == ' ' || s[a] == '\t' || s[a] == '\r' || s[a] == '\n')) a++;
    while (b > a && (s[b - 1] == ' ' || s[b - 1] == '\t' || s[b - 1] == '\r' || s[b - 1] == '\n')) b--;
    return s.substr(a, b - a);
}

std::string ra_unquote(const std::string& s) {
    if (s.size() >= 2 && s.front() == '"' && s.back() == '"')
        return s.substr(1, s.size() - 2);
    if (s.size() >= 2 && s.front() == '\'' && s.back() == '\'')
        return s.substr(1, s.size() - 2);
    return s;
}

RaConfig ra_read_config(const std::string& exe_dir) {
    RaConfig cfg;
    std::string path = exe_dir + "/retroachievements.toml";
    FILE* f = fopen(path.c_str(), "r");
    char line[1024];
    if (!f) return cfg;
    cfg.present = true;
    while (fgets(line, sizeof(line), f)) {
        std::string s = ra_trim(line);
        size_t eq;
        std::string key, val;
        if (s.empty() || s[0] == '#') continue;
        eq = s.find('=');
        if (eq == std::string::npos) continue;
        key = ra_trim(s.substr(0, eq));
        val = ra_unquote(ra_trim(s.substr(eq + 1)));
        if (key == "username") cfg.username = val;
        else if (key == "api_token") cfg.api_token = val;
        else if (key == "hardcore") cfg.hardcore = (val == "true" || val == "1");
    }
    fclose(f);
    return cfg;
}

/* ------------------------------------------------------------------ */
/* ROM path: the runtime's own resolution order — rom.cfg next to the  */
/* exe (first line = path), else a .gb/.gbc beside the exe, else one   */
/* under roms/ (mirrors launcher.c launcher_get_rom_path).             */
/* ------------------------------------------------------------------ */

bool ra_file_exists(const std::string& p) {
    FILE* f = fopen(p.c_str(), "rb");
    if (f) { fclose(f); return true; }
    return false;
}

std::string ra_scan_rom_dir(const std::string& dir) {
    std::error_code ec;
    std::vector<std::string> hits;
    for (const auto& e : std::filesystem::directory_iterator(dir, ec)) {
        if (ec) break;
        if (!e.is_regular_file(ec)) continue;
        std::string name = e.path().filename().string();
        std::string low = name;
        for (auto& c : low) c = (char)tolower((unsigned char)c);
        if (low.size() > 3 && (low.compare(low.size() - 3, 3, ".gb") == 0 ||
                               low.compare(low.size() - 4, 4, ".gbc") == 0))
            hits.push_back(e.path().string());
    }
    std::sort(hits.begin(), hits.end());
    return hits.empty() ? std::string() : hits.front();
}

std::string ra_rom_path(const std::string& exe_dir) {
    std::string cfg = exe_dir + "/rom.cfg";
    FILE* f = fopen(cfg.c_str(), "r");
    if (f) {
        char line[4096];
        std::string p;
        if (fgets(line, sizeof(line), f)) p = ra_trim(line);
        fclose(f);
        if (!p.empty()) {
            if (ra_file_exists(p)) return p;
            std::string joined = exe_dir + "/" + p;
            if (ra_file_exists(joined)) return joined;
        }
    }
    std::string found = ra_scan_rom_dir(exe_dir);
    if (!found.empty()) return found;
    return ra_scan_rom_dir(exe_dir + "/roms");
}

/* ------------------------------------------------------------------ */
/* Memory peek: live machine via the runtime's CPU-bus read            */
/* ------------------------------------------------------------------ */

GBContext* g_ctx = nullptr;

uint32_t ra_read_memory(uint32_t address, uint8_t* buffer, uint32_t num_bytes,
                        rc_client_t* /*client*/) {
    if (!buffer || num_bytes == 0 || !g_ctx) return 0;
    uint32_t done = 0;
    while (done < num_bytes) {
        uint32_t a = address + done;
        if (a > 0xFFFFu) break;
        buffer[done++] = gb_read8(g_ctx, (uint16_t)a);
    }
    return done;
}

/* ------------------------------------------------------------------ */
/* Server calls: detached thread + game-thread delivery                */
/* ------------------------------------------------------------------ */

struct RaPending {
    rc_client_server_callback_t callback = nullptr;
    void* callback_data = nullptr;
    char* body = nullptr;
    size_t body_len = 0;
    int http_status = 0;
};

std::mutex g_pending_mutex;
std::vector<RaPending> g_pending;

void ra_server_call(const rc_api_request_t* request,
                    rc_client_server_callback_t callback, void* callback_data,
                    rc_client_t* /*client*/) {
    std::string url = request->url ? request->url : "";
    std::string post = request->post_data ? request->post_data : "";
    std::string ctype = request->content_type ? request->content_type : "";
    std::thread([url, post, ctype, callback, callback_data]() {
        char* body = nullptr;
        size_t body_len = 0;
        int status = 0;
        RaPending p;
        if (ra_http_post(url.c_str(), post.c_str(), ctype.c_str(),
                         &body, &body_len, &status) != 0) {
            body = nullptr;
            body_len = 0;
            status = 0;
        }
        p.callback = callback;
        p.callback_data = callback_data;
        p.body = body;
        p.body_len = body_len;
        p.http_status = status;
        {
            std::lock_guard<std::mutex> lock(g_pending_mutex);
            g_pending.push_back(p);
        }
    }).detach();
}

void ra_deliver_pending() {
    std::vector<RaPending> due;
    {
        std::lock_guard<std::mutex> lock(g_pending_mutex);
        due.swap(g_pending);
    }
    for (auto& p : due) {
        rc_api_server_response_t resp;
        memset(&resp, 0, sizeof(resp));
        resp.body = p.body ? p.body : "";
        resp.body_length = p.body_len;
        resp.http_status_code = p.http_status;
        if (p.callback) p.callback(&resp, p.callback_data);
        free(p.body);
    }
}

/* ------------------------------------------------------------------ */
/* Toasts: drawn into the native framebuffer from game_post_frame.     */
/* The GB runtime exposes no toast channel; the framebuffer is the     */
/* one surface a game TU can paint that reaches the screen every       */
/* frame (the hook runs after the frame completes, before present).    */
/* ------------------------------------------------------------------ */

struct RaToast {
    std::string title;
    std::string body;
    std::chrono::steady_clock::time_point until{};
    bool active = false;
};
RaToast g_toast;

void ra_toast_show(const std::string& title, const std::string& body) {
#ifdef RA_DISABLE_POPUPS
    /* Recomp Tools option: pop-ups hidden at build time. This also
       hides the login toast, which uses the same channel. */
    (void)title; (void)body;
    return;
#else
    g_toast.title = title;
    g_toast.body = body;
    g_toast.until = std::chrono::steady_clock::now() +
                    std::chrono::milliseconds(4000);
    g_toast.active = true;
#endif
}

int ra_text_width(const std::string& s) {
    int w = 0;
    for (unsigned char c : s) {
        if (c < RA_FONT_FIRST || c > RA_FONT_LAST) c = '?';
        w += RA_FONT_ADV[c - RA_FONT_FIRST];
    }
    return w;
}

std::string ra_fit_line(const std::string& s, int max_w) {
    if (ra_text_width(s) <= max_w) return s;
    const std::string dots = "...";
    int dw = ra_text_width(dots);
    std::string out;
    int w = 0;
    for (unsigned char c : s) {
        unsigned char g = (c < RA_FONT_FIRST || c > RA_FONT_LAST) ? '?' : c;
        int adv = RA_FONT_ADV[g - RA_FONT_FIRST];
        if (w + adv + dw > max_w) break;
        w += adv;
        out += (char)g;
    }
    return out + dots;
}

void ra_draw_text(uint32_t* fb, int x, int y, const std::string& s,
                  uint32_t color, bool bold) {
    int cx = x;
    for (unsigned char c : s) {
        unsigned char g = (c < RA_FONT_FIRST || c > RA_FONT_LAST) ? '?' : c;
        const uint16_t* rows = RA_FONT_ROWS[g - RA_FONT_FIRST];
        for (int r = 0; r < RA_FONT_CELL; r++) {
            uint16_t bits = rows[r];
            if (!bits) continue;
            int py = y + r;
            if (py < 0 || py >= GB_SCREEN_HEIGHT) continue;
            for (int col = 0; col < RA_FONT_CELL; col++) {
                if (!(bits & (0x8000 >> col))) continue;
                int px = cx + col;
                if (px < 0 || px >= GB_SCREEN_WIDTH) continue;
                fb[py * GB_SCREEN_WIDTH + px] = color;
                if (bold && px + 1 < GB_SCREEN_WIDTH)
                    fb[py * GB_SCREEN_WIDTH + px + 1] = color;
            }
        }
        cx += RA_FONT_ADV[g - RA_FONT_FIRST];
    }
}

void ra_toast_draw(GBContext* ctx) {
    if (!g_toast.active) return;
    if (std::chrono::steady_clock::now() >= g_toast.until) {
        g_toast.active = false;
        return;
    }
    if (!ctx) return;
    const uint32_t* fb_const = gb_get_framebuffer(ctx);
    if (!fb_const) return;
    uint32_t* fb = const_cast<uint32_t*>(fb_const);

    const int max_text_w = GB_SCREEN_WIDTH - 16;
    std::string title = ra_fit_line(g_toast.title, max_text_w);
    std::string body = ra_fit_line(g_toast.body, max_text_w);
    int lines = (title.empty() ? 0 : 1) + (body.empty() ? 0 : 1);
    if (lines == 0) return;
    const int line_h = 15;
    int text_w = 0;
    if (!title.empty()) text_w = std::max(text_w, ra_text_width(title));
    if (!body.empty()) text_w = std::max(text_w, ra_text_width(body));
    int box_w = std::min(GB_SCREEN_WIDTH - 4, text_w + 12);
    int box_h = lines * line_h + 9;
    int box_x = (GB_SCREEN_WIDTH - box_w) / 2;
    int box_y = 6;

    const uint32_t border = 0xFFAAAAAAu;
    for (int y = box_y; y < box_y + box_h && y < GB_SCREEN_HEIGHT; y++) {
        for (int x = box_x; x < box_x + box_w && x < GB_SCREEN_WIDTH; x++) {
            uint32_t& p = fb[y * GB_SCREEN_WIDTH + x];
            if (x == box_x || y == box_y || x == box_x + box_w - 1 ||
                y == box_y + box_h - 1) {
                p = border;
            } else {
                /* Dim the game behind the toast to ~12% so busy
                   screens (legal text, HUDs) don't bleed through. */
                uint32_t r = ((p >> 16) & 0xFF) * 12 / 100;
                uint32_t g = ((p >> 8) & 0xFF) * 12 / 100;
                uint32_t b = (p & 0xFF) * 12 / 100;
                p = 0xFF000000u | (r << 16) | (g << 8) | b;
            }
        }
    }
    int ty = box_y + 4;
    if (!title.empty()) {
        ra_draw_text(fb, box_x + 6, ty, title, 0xFFFFCD50u, true);
        ty += line_h;
    }
    if (!body.empty())
        ra_draw_text(fb, box_x + 6, ty, body, 0xFFF0F0F0u, false);
}

/* ------------------------------------------------------------------ */
/* Events + async callbacks                                            */
/* ------------------------------------------------------------------ */

void ra_event_handler(const rc_client_event_t* event, rc_client_t* /*client*/) {
    if (!event) return;
    switch (event->type) {
        case RC_CLIENT_EVENT_ACHIEVEMENT_TRIGGERED:
            if (event->achievement) {
                fprintf(stdout,
                        "[RA] Achievement unlocked: %s (%u points) - %s\n",
                        event->achievement->title ? event->achievement->title : "?",
                        event->achievement->points,
                        event->achievement->description
                            ? event->achievement->description
                            : "");
                fflush(stdout);
                ra_toast_show(
                    event->achievement->title ? event->achievement->title : "?",
                    event->achievement->description
                        ? event->achievement->description
                        : "");
            }
            break;
        case RC_CLIENT_EVENT_GAME_COMPLETED:
            fprintf(stdout, "[RA] All achievements for this game earned!\n");
            fflush(stdout);
            break;
        case RC_CLIENT_EVENT_SERVER_ERROR:
            if (event->server_error) {
                fprintf(stderr, "[RA] Server error (%s): %s\n",
                        event->server_error->api ? event->server_error->api : "?",
                        event->server_error->error_message
                            ? event->server_error->error_message
                            : "?");
            }
            break;
        case RC_CLIENT_EVENT_DISCONNECTED:
            fprintf(stderr, "[RA] Disconnected: unlocks will queue until reconnect.\n");
            break;
        case RC_CLIENT_EVENT_RECONNECTED:
            fprintf(stdout, "[RA] Reconnected: queued unlocks submitted.\n");
            fflush(stdout);
            break;
        default:
            break;
    }
}

void ra_login_callback(int result, const char* error_message,
                       rc_client_t* /*client*/, void* /*userdata*/) {
    /* rcheevos reports RC_OK (0) on success, nonzero on failure. */
    if (result == 0) {
        fprintf(stdout, "[RA] Logged in.\n");
        ra_toast_show("RetroAchievements", "Logged in");
    } else {
        fprintf(stdout, "[RA] Login failed: %s\n",
                error_message ? error_message : "unknown error");
        /* GB toast body is shorter than the family text: the native
           screen is 160 px wide and the full sentence truncates. */
        ra_toast_show("RetroAchievements", "Login failed: check toml");
    }
    fflush(stdout);
}

void ra_load_callback(int result, const char* error_message,
                      rc_client_t* /*client*/, void* /*userdata*/) {
    if (result == 0) {
        fprintf(stdout, "[RA] Game achievements loaded.\n");
    } else {
        fprintf(stderr, "[RA] Could not load game achievements: %s\n",
                error_message ? error_message : "unknown error");
    }
    fflush(stdout);
}

/* ------------------------------------------------------------------ */
/* Init (lazy, first post-frame) + the frame hook                      */
/* ------------------------------------------------------------------ */

rc_client_t* g_client = nullptr;
bool g_init_done = false;

void ra_init() {
    std::string exe_dir = ra_exe_dir();
    /* Dev-only test hook (NOT shipped in packages): if ra_test_toast.txt
     * sits next to the exe, pop its contents as a toast so the pop-up
     * channel can be verified without a real RA unlock. */
    {
        std::string test_path = exe_dir + "/ra_test_toast.txt";
        FILE* tf = fopen(test_path.c_str(), "r");
        if (tf) {
            char tline[256] = "";
            char bline[512] = "";
            if (fgets(tline, sizeof(tline), tf)) {
                std::string t = ra_trim(tline);
                std::string b;
                if (fgets(bline, sizeof(bline), tf)) b = ra_trim(bline);
                if (t.empty()) t = "Test Achievement";
                if (b.empty()) b = "In-game pop-ups are working!";
                ra_toast_show(t, b);
                fprintf(stdout, "[RA] test toast armed: %s\n", t.c_str());
                fflush(stdout);
            }
            fclose(tf);
        }
    }
    RaConfig cfg = ra_read_config(exe_dir);
    if (!cfg.present) {
        fprintf(stdout,
                "[RA] Disabled: no retroachievements.toml next to the exe.\n"
                "[RA] To enable: create retroachievements.toml with your\n"
                "[RA] username and RetroAchievements API token, then restart.\n");
        fflush(stdout);
        return;
    }
    if (cfg.username.empty() || cfg.api_token.empty()) {
        fprintf(stderr,
                "[RA] Disabled: retroachievements.toml is missing username "
                "or api_token.\n");
        return;
    }
    std::string rom = ra_rom_path(exe_dir);
    if (rom.empty()) {
        fprintf(stderr, "[RA] Disabled: could not find the ROM "
                        "(no rom.cfg, no .gb/.gbc beside the exe).\n");
        return;
    }
    /* Console identity from the cart header's CGB flag (0x143):
     * RC_CONSOLE_GAMEBOY = 4, RC_CONSOLE_GAMEBOY_COLOR = 6 in the
     * vendored rc_consoles.h. The hash is the whole-file algorithm
     * either way; the id documents which library the game belongs to. */
    uint32_t console_id = RC_CONSOLE_GAMEBOY;
    {
        FILE* rf = fopen(rom.c_str(), "rb");
        if (rf) {
            if (fseek(rf, 0x143, SEEK_SET) == 0) {
                int cgb = fgetc(rf);
                if (cgb == 0x80 || cgb == 0xC0)
                    console_id = RC_CONSOLE_GAMEBOY_COLOR;
            }
            fclose(rf);
        }
    }
    char hash[33];
    memset(hash, 0, sizeof(hash));
    if (!rc_hash_generate_from_file(hash, console_id, rom.c_str())) {
        fprintf(stderr, "[RA] Disabled: could not hash ROM %s\n", rom.c_str());
        return;
    }
    fprintf(stdout, "[RA] ROM hash: %s\n", hash);

    g_client = rc_client_create(ra_read_memory, ra_server_call);
    if (!g_client) {
        fprintf(stderr, "[RA] Disabled: rc_client_create failed.\n");
        return;
    }
    rc_client_set_event_handler(g_client, ra_event_handler);
    rc_client_set_hardcore_enabled(g_client, cfg.hardcore ? 1 : 0);
    fprintf(stdout, "[RA] Logging in as %s (hardcore %s)...\n",
            cfg.username.c_str(), cfg.hardcore ? "on" : "off");
    fflush(stdout);
    rc_client_begin_login_with_token(g_client, cfg.username.c_str(),
                                     cfg.api_token.c_str(), ra_login_callback,
                                     nullptr);
    rc_client_begin_load_game(g_client, hash, ra_load_callback, nullptr);
}

} /* namespace */

/* The framework hook: one strong symbol in this TU displaces the
 * gbrt archive's no-op default (game_default_post_frame.c). The
 * engine patches the generated <game>_main.c to call this once per
 * completed frame, before the platform presents the framebuffer —
 * which is also what lets the toast paint into the finished frame. */
extern "C" void game_post_frame(struct GBContext* ctx) {
    g_ctx = ctx;
    if (!g_init_done) {
        g_init_done = true;
        ra_init();
    }
    if (g_client) ra_deliver_pending();
    if (g_client) rc_client_do_frame(g_client);
    ra_toast_draw(ctx);
}
