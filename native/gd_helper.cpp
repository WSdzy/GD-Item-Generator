#define WIN32_LEAN_AND_MEAN
#define NOMINMAX

#include <windows.h>

#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <memory>
#include <string>
#include <vector>

namespace {

constexpr wchar_t kPipeName[] = L"\\\\.\\pipe\\GDIndependentTrainer";
constexpr DWORD kPipeBufferSize = 64 * 1024;
constexpr DWORD kCommandTimeoutMs = 20000;
constexpr std::size_t kReplicaSize = 400;

HMODULE g_module = nullptr;
CRITICAL_SECTION g_queue_lock;
bool g_queue_lock_initialized = false;
std::vector<std::shared_ptr<struct Command>> g_pending_commands;
HANDLE g_drain_idle_event = nullptr;

HHOOK g_message_hook = nullptr;
DWORD g_game_thread_id = 0;

struct MsStringRaw {
    union {
        char small[16];
        char* data;
    };
    std::size_t size;
    std::size_t capacity;
};

static_assert(sizeof(MsStringRaw) == 32, "MSVC x64 std::string layout changed");

enum class CommandState {
    Queued,
    Busy,
    Finished,
    Cancelled,
};

struct Command {
    std::string request;
    std::string response;
    HANDLE completed = nullptr;
    CommandState state = CommandState::Queued;
    bool shutdown = false;

    ~Command() {
        if (completed != nullptr) {
            CloseHandle(completed);
        }
    }
};

using GetMainPlayerFn = void* (*)(const void* engine);
using CreateItemFn = void* (*)(const void* replica);
using GiveItemToCharacterFn = void (*)(void* player, void* item, bool a, bool b);

GetMainPlayerFn g_get_main_player = nullptr;
CreateItemFn g_create_item = nullptr;
GiveItemToCharacterFn g_give_item_to_character = nullptr;
void** g_game_engine_export = nullptr;

std::string g_service_state = "starting";

void LogStartup(const char* event, DWORD error = ERROR_SUCCESS) {
    wchar_t path[MAX_PATH] = {};
    if (g_module == nullptr ||
        GetModuleFileNameW(g_module, path, static_cast<DWORD>(std::size(path))) == 0) {
        return;
    }
    wchar_t* separator = wcsrchr(path, L'\\');
    if (separator == nullptr) {
        return;
    }
    wcscpy_s(separator + 1, std::size(path) - static_cast<std::size_t>(separator + 1 - path), L"gd_helper_startup.log");

    HANDLE file = CreateFileW(
        path,
        FILE_APPEND_DATA,
        FILE_SHARE_READ | FILE_SHARE_WRITE,
        nullptr,
        OPEN_ALWAYS,
        FILE_ATTRIBUTE_NORMAL,
        nullptr);
    if (file == INVALID_HANDLE_VALUE) {
        return;
    }
    char line[256] = {};
    const int length = std::snprintf(line, sizeof(line), "%s error=%lu\r\n", event, static_cast<unsigned long>(error));
    if (length > 0) {
        DWORD written = 0;
        WriteFile(file, line, static_cast<DWORD>(length), &written, nullptr);
    }
    CloseHandle(file);
}

void SetServiceState(const char* state) {
    g_service_state = state ? state : "unknown";
    LogStartup(g_service_state.c_str());
}

std::vector<std::string> SplitTabs(const std::string& text) {
    std::vector<std::string> fields;
    std::size_t start = 0;
    while (true) {
        const std::size_t end = text.find('\t', start);
        if (end == std::string::npos) {
            fields.push_back(text.substr(start));
            break;
        }
        fields.push_back(text.substr(start, end - start));
        start = end + 1;
    }
    return fields;
}

std::string TrimLine(std::string text) {
    while (!text.empty() && (text.back() == '\r' || text.back() == '\n')) {
        text.pop_back();
    }
    return text;
}

bool ParseUnsigned(const std::string& text, unsigned long long* value) {
    if (text.empty() || value == nullptr) {
        return false;
    }

    char* end = nullptr;
    const unsigned long long parsed = std::strtoull(text.c_str(), &end, 0);
    if (end == text.c_str() || *end != '\0') {
        return false;
    }
    *value = parsed;
    return true;
}

bool IsSafeRecordPath(const std::string& path) {
    if (path.empty() || path.size() > 240) {
        return false;
    }
    if (path.find('\t') != std::string::npos ||
        path.find('\r') != std::string::npos ||
        path.find('\n') != std::string::npos) {
        return false;
    }
    return path.rfind("records/", 0) == 0 && path.size() > 8;
}

std::string PointerText(const void* pointer) {
    char buffer[32] = {};
    std::snprintf(buffer, sizeof(buffer), "0x%p", pointer);
    return buffer;
}

void* GameEngine() {
    if (g_game_engine_export == nullptr) {
        return nullptr;
    }
    return *g_game_engine_export;
}

bool ResolveGameSymbols() {
    HMODULE game = GetModuleHandleW(L"Game.dll");
    if (game == nullptr) {
        game = LoadLibraryW(L"Game.dll");
    }
    if (game == nullptr) {
        SetServiceState("missing Game.dll");
        return false;
    }

    g_get_main_player = reinterpret_cast<GetMainPlayerFn>(
        GetProcAddress(game, "?GetMainPlayer@GameEngine@GAME@@QEBAPEAVPlayer@2@XZ"));
    g_create_item = reinterpret_cast<CreateItemFn>(
        GetProcAddress(game, "?CreateItem@Item@GAME@@SAPEAV12@AEBUItemReplicaInfo@2@@Z"));
    g_give_item_to_character = reinterpret_cast<GiveItemToCharacterFn>(
        GetProcAddress(game, "?GiveItemToCharacter@Player@GAME@@UEAAXPEAVItem@2@_N1@Z"));
    g_game_engine_export = reinterpret_cast<void**>(
        GetProcAddress(game, "?gGameEngine@GAME@@3PEAVGameEngine@1@EA"));

    const bool ready =
        g_get_main_player != nullptr &&
        g_create_item != nullptr &&
        g_give_item_to_character != nullptr &&
        g_game_engine_export != nullptr;

    SetServiceState(ready ? "ready" : "missing Game.dll exports");
    return ready;
}

struct WindowSearch {
    DWORD process_id = 0;
    HWND best_window = nullptr;
    long long best_score = -1;
};

BOOL CALLBACK FindGameWindowCallback(HWND window, LPARAM parameter) {
    auto* search = reinterpret_cast<WindowSearch*>(parameter);
    if (search == nullptr || !IsWindowVisible(window)) {
        return TRUE;
    }

    DWORD process_id = 0;
    GetWindowThreadProcessId(window, &process_id);
    if (process_id != search->process_id) {
        return TRUE;
    }

    RECT rect = {};
    GetWindowRect(window, &rect);
    const long long width = static_cast<long long>(rect.right - rect.left);
    const long long height = static_cast<long long>(rect.bottom - rect.top);
    long long score = width * height;

    wchar_t title[256] = {};
    GetWindowTextW(window, title, static_cast<int>(std::size(title)));
    if (wcsstr(title, L"Grim Dawn") != nullptr) {
        score += 1000000000000LL;
    }

    if (score > search->best_score) {
        search->best_score = score;
        search->best_window = window;
    }
    return TRUE;
}

DWORD FindGameThread() {
    WindowSearch search = {};
    search.process_id = GetCurrentProcessId();
    EnumWindows(FindGameWindowCallback, reinterpret_cast<LPARAM>(&search));
    if (search.best_window == nullptr) {
        return 0;
    }
    return GetWindowThreadProcessId(search.best_window, nullptr);
}

void RawWriteU8(void* replica, std::size_t offset, std::uint8_t value) {
    std::memcpy(reinterpret_cast<char*>(replica) + offset, &value, sizeof(value));
}

void RawWriteU32(void* replica, std::size_t offset, std::uint32_t value) {
    std::memcpy(reinterpret_cast<char*>(replica) + offset, &value, sizeof(value));
}

MsStringRaw* RawString(void* replica, std::size_t offset) {
    return reinterpret_cast<MsStringRaw*>(reinterpret_cast<char*>(replica) + offset);
}

void SetRawString(MsStringRaw* target, const std::string& value) {
    std::memset(target, 0, sizeof(*target));
    target->size = value.size();
    target->capacity = 15;

    if (value.size() < sizeof(target->small)) {
        if (!value.empty()) {
            std::memcpy(target->small, value.data(), value.size());
        }
        return;
    }

    // The game only copies this input. The backing string remains alive until
    // Item::SetItemReplicaInfo has finished copying it.
    target->data = const_cast<char*>(value.data());
    target->capacity = value.capacity();
}

void InitializeReplica(void* replica) {
    std::memset(replica, 0, kReplicaSize);

    constexpr std::size_t kStringOffsets[] = {
        8, 40, 72, 112, 144, 176, 216, 256, 288, 320,
    };
    for (const std::size_t offset : kStringOffsets) {
        SetRawString(RawString(replica, offset), "");
    }

    RawWriteU8(replica, 356, 1);
    RawWriteU32(replica, 376, 1);
}

std::string ExecuteCreateAffixed(const std::vector<std::string>& fields) {
    if (fields.size() < 5) {
        return "ERR\tusage=create_affixed<TAB>base<TAB>prefix<TAB>suffix<TAB>seed[<TAB>count]";
    }
    if (!IsSafeRecordPath(fields[1])) {
        return "ERR\tinvalid_base_path";
    }
    if (!fields[2].empty() && !IsSafeRecordPath(fields[2])) {
        return "ERR\tinvalid_prefix_path";
    }
    if (!fields[3].empty() && !IsSafeRecordPath(fields[3])) {
        return "ERR\tinvalid_suffix_path";
    }

    unsigned long long seed_value = 0;
    if (!ParseUnsigned(fields[4], &seed_value)) {
        return "ERR\tinvalid_seed";
    }

    unsigned long long count_value = 1;
    if (fields.size() >= 6 && !ParseUnsigned(fields[5], &count_value)) {
        return "ERR\tinvalid_count";
    }
    if (count_value == 0 || count_value > 1000) {
        return "ERR\tcount_out_of_range";
    }

    void* engine = GameEngine();
    void* player = engine == nullptr ? nullptr : g_get_main_player(engine);
    if (player == nullptr) {
        return "ERR\tgame_not_ready";
    }

    std::string base = fields[1];
    std::string prefix = fields[2];
    std::string suffix = fields[3];

    void* first_item = nullptr;
    void* last_item = nullptr;
    for (unsigned long long index = 0; index < count_value; ++index) {
        alignas(16) unsigned char replica[kReplicaSize] = {};
        InitializeReplica(replica);
        SetRawString(RawString(replica, 8), base);
        SetRawString(RawString(replica, 40), prefix);
        SetRawString(RawString(replica, 72), suffix);
        RawWriteU32(replica, 104, static_cast<std::uint32_t>(seed_value));

        void* item = g_create_item(replica);
        if (item == nullptr) {
            if (first_item == nullptr) {
                return "ERR\titem_create_failed";
            }
            return "ERR\titem_create_failed\tcreated=" + std::to_string(index);
        }

        g_give_item_to_character(player, item, true, false);
        if (first_item == nullptr) {
            first_item = item;
        }
        last_item = item;
    }

    return "OK\tcreated_affixed\titem=" + PointerText(first_item) +
           "\tlast=" + PointerText(last_item) +
           "\tseed=" + std::to_string(seed_value) +
           "\tcount=" + std::to_string(count_value);
}

std::string ExecuteCreateBase(const std::vector<std::string>& fields) {
    if (fields.size() < 2 || !IsSafeRecordPath(fields[1])) {
        return "ERR\tinvalid_base_path";
    }

    unsigned long long count_value = 1;
    if (fields.size() >= 3 && !ParseUnsigned(fields[2], &count_value)) {
        return "ERR\tinvalid_count";
    }
    if (count_value == 0 || count_value > 1000) {
        return "ERR\tcount_out_of_range";
    }

    // A base item is an affixed item with no optional affixes.  This route
    // returns a concrete item pointer and inserts it into the local player,
    // unlike the void debug helper used previously.
    const std::vector<std::string> request = {
        "create_affixed",
        fields[1],
        "",
        "",
        "0",
        std::to_string(count_value),
    };
    std::string response = ExecuteCreateAffixed(request);
    constexpr char kAffixedPrefix[] = "OK\tcreated_affixed";
    if (response.rfind(kAffixedPrefix, 0) == 0) {
        response.replace(0, sizeof(kAffixedPrefix) - 1, "OK\tcreated_base");
    }
    return response;
}

std::string ExecuteCommand(const std::string& request, bool* shutdown) {
    if (shutdown != nullptr) {
        *shutdown = false;
    }

    const std::vector<std::string> fields = SplitTabs(TrimLine(request));
    if (fields.empty() || fields[0].empty()) {
        return "ERR\tempty_command";
    }

    if (fields[0] == "ping") {
        return "OK\tpong\tstate=" + g_service_state;
    }
    if (fields[0] == "create_base") {
        return ExecuteCreateBase(fields);
    }
    if (fields[0] == "create_affixed") {
        return ExecuteCreateAffixed(fields);
    }
    if (fields[0] == "shutdown") {
        if (shutdown != nullptr) {
            *shutdown = true;
        }
        return "OK\tshutdown";
    }

    return "ERR\tunknown_command";
}

void DrainCommands() {
    static thread_local bool draining = false;
    if (draining) {
        return;
    }
    draining = true;
    if (g_drain_idle_event != nullptr) {
        ResetEvent(g_drain_idle_event);
    }

    while (true) {
        std::shared_ptr<Command> command;
        EnterCriticalSection(&g_queue_lock);
        if (!g_pending_commands.empty()) {
            command = g_pending_commands.front();
            g_pending_commands.erase(g_pending_commands.begin());
            if (command->state != CommandState::Cancelled) {
                command->state = CommandState::Busy;
            }
        }
        LeaveCriticalSection(&g_queue_lock);

        if (command == nullptr) {
            break;
        }
        if (command->state == CommandState::Cancelled) {
            continue;
        }

        command->response = ExecuteCommand(command->request, &command->shutdown);
        EnterCriticalSection(&g_queue_lock);
        command->state = CommandState::Finished;
        LeaveCriticalSection(&g_queue_lock);
        SetEvent(command->completed);
    }

    draining = false;
    if (g_drain_idle_event != nullptr) {
        SetEvent(g_drain_idle_event);
    }
}

LRESULT CALLBACK GetMessageHook(int code, WPARAM wparam, LPARAM lparam) {
    if (code == HC_ACTION) {
        DrainCommands();
    }
    return CallNextHookEx(g_message_hook, code, wparam, lparam);
}

std::string SendCommandToGameThread(const std::string& request) {
    if (g_game_thread_id == 0 || g_message_hook == nullptr) {
        return "ERR\tgame_thread_not_hooked";
    }

    std::shared_ptr<Command> command = std::make_shared<Command>();
    command->request = request;
    command->completed = CreateEventW(nullptr, TRUE, FALSE, nullptr);
    if (command->completed == nullptr) {
        return "ERR\tcreate_event_failed";
    }

    EnterCriticalSection(&g_queue_lock);
    g_pending_commands.push_back(command);
    LeaveCriticalSection(&g_queue_lock);

    if (!PostThreadMessageW(g_game_thread_id, WM_NULL, 0, 0)) {
        EnterCriticalSection(&g_queue_lock);
        for (auto iterator = g_pending_commands.begin(); iterator != g_pending_commands.end(); ++iterator) {
            if (*iterator == command) {
                command->state = CommandState::Cancelled;
                g_pending_commands.erase(iterator);
                break;
            }
        }
        LeaveCriticalSection(&g_queue_lock);
        return "ERR\tpost_thread_message_failed";
    }

    const DWORD wait_result = WaitForSingleObject(command->completed, kCommandTimeoutMs);
    if (wait_result != WAIT_OBJECT_0) {
        EnterCriticalSection(&g_queue_lock);
        if (command->state == CommandState::Queued) {
            command->state = CommandState::Cancelled;
            for (auto iterator = g_pending_commands.begin(); iterator != g_pending_commands.end(); ++iterator) {
                if (*iterator == command) {
                    g_pending_commands.erase(iterator);
                    break;
                }
            }
        }
        LeaveCriticalSection(&g_queue_lock);
        return wait_result == WAIT_TIMEOUT ? "ERR\tcommand_timeout" : "ERR\tcommand_wait_failed";
    }

    EnterCriticalSection(&g_queue_lock);
    const std::string response = command->response;
    LeaveCriticalSection(&g_queue_lock);
    return response;
}

bool WriteAll(HANDLE pipe, const std::string& data) {
    std::size_t written_total = 0;
    while (written_total < data.size()) {
        DWORD written = 0;
        const DWORD remaining = static_cast<DWORD>(data.size() - written_total);
        if (!WriteFile(pipe, data.data() + written_total, remaining, &written, nullptr) || written == 0) {
            return false;
        }
        written_total += written;
    }
    return true;
}

bool ReadLine(HANDLE pipe, std::string* line) {
    if (line == nullptr) {
        return false;
    }
    line->clear();

    char buffer[4096] = {};
    while (true) {
        DWORD read = 0;
        if (!ReadFile(pipe, buffer, 1, &read, nullptr) || read == 0) {
            return false;
        }
        if (buffer[0] == '\n') {
            return true;
        }
        if (buffer[0] != '\r') {
            line->push_back(buffer[0]);
            if (line->size() > 4096) {
                return false;
            }
        }
    }
}

bool HandleClient(HANDLE pipe) {
    while (true) {
        std::string request;
        if (!ReadLine(pipe, &request)) {
            return false;
        }

        const std::vector<std::string> fields = SplitTabs(TrimLine(request));
        const std::string command = fields.empty() ? std::string() : fields[0];

        std::string response;
        if (command == "ping") {
            response = "OK\tpong\tstate=" + g_service_state;
        } else if (command == "shutdown") {
            response = "OK\tshutdown";
        } else {
            response = SendCommandToGameThread(request);
        }

        if (!WriteAll(pipe, response + "\r\n")) {
            return false;
        }

        if (command == "shutdown") {
            return true;
        }
    }
}

bool InstallGameHook() {
    g_game_thread_id = FindGameThread();
    if (g_game_thread_id == 0) {
        SetServiceState("game window not found");
        return false;
    }

    g_message_hook = SetWindowsHookExW(
        WH_GETMESSAGE,
        GetMessageHook,
        g_module,
        g_game_thread_id);
    if (g_message_hook == nullptr) {
        SetServiceState("SetWindowsHookEx failed");
        return false;
    }

    if (!PostThreadMessageW(g_game_thread_id, WM_NULL, 0, 0)) {
        UnhookWindowsHookEx(g_message_hook);
        g_message_hook = nullptr;
        SetServiceState("PostThreadMessage warmup failed");
        return false;
    }
    return true;
}

void CleanupAndExit() {
    if (g_message_hook != nullptr) {
        UnhookWindowsHookEx(g_message_hook);
        g_message_hook = nullptr;
    }
    if (g_drain_idle_event != nullptr) {
        WaitForSingleObject(g_drain_idle_event, 5000);
        CloseHandle(g_drain_idle_event);
        g_drain_idle_event = nullptr;
    }
    if (g_queue_lock_initialized) {
        DeleteCriticalSection(&g_queue_lock);
        g_queue_lock_initialized = false;
    }
    FreeLibraryAndExitThread(g_module, 0);
}

DWORD WINAPI HelperThread(void*) {
    LogStartup("helper-thread-start");
    InitializeCriticalSection(&g_queue_lock);
    g_queue_lock_initialized = true;
    g_drain_idle_event = CreateEventW(nullptr, TRUE, TRUE, nullptr);

    if (!ResolveGameSymbols()) {
        SetServiceState("not ready");
    } else if (!InstallGameHook()) {
        SetServiceState("hook unavailable");
    } else {
        SetServiceState("ready");
    }

    HANDLE pipe = CreateNamedPipeW(
        kPipeName,
        // The previous Helper may still be releasing its pipe handle while a
        // newly injected DLL starts.  A first-instance-only pipe fails in
        // that handover window and leaves the UI with no diagnostic channel.
        PIPE_ACCESS_DUPLEX,
        PIPE_TYPE_BYTE | PIPE_READMODE_BYTE | PIPE_WAIT | PIPE_REJECT_REMOTE_CLIENTS,
        PIPE_UNLIMITED_INSTANCES,
        kPipeBufferSize,
        kPipeBufferSize,
        0,
        nullptr);

    if (pipe == INVALID_HANDLE_VALUE) {
        const DWORD error = GetLastError();
        LogStartup("CreateNamedPipe failed", error);
        SetServiceState("CreateNamedPipe failed");
        CleanupAndExit();
        return 0;
    }
    LogStartup("pipe-created");

    bool should_exit = false;
    while (!should_exit) {
        const BOOL connected = ConnectNamedPipe(pipe, nullptr);
        if (!connected && GetLastError() != ERROR_PIPE_CONNECTED) {
            break;
        }

        should_exit = HandleClient(pipe);
        FlushFileBuffers(pipe);
        DisconnectNamedPipe(pipe);
    }

    CloseHandle(pipe);
    CleanupAndExit();
    return 0;
}

}  // namespace

BOOL WINAPI DllMain(HMODULE module, DWORD reason, LPVOID) {
    if (reason == DLL_PROCESS_ATTACH) {
        g_module = module;
        DisableThreadLibraryCalls(module);
        HANDLE thread = CreateThread(nullptr, 0, HelperThread, nullptr, 0, nullptr);
        if (thread != nullptr) {
            CloseHandle(thread);
        }
    }
    return TRUE;
}
