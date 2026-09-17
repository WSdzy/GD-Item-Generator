#define WIN32_LEAN_AND_MEAN
#define NOMINMAX

#include <windows.h>
#include <tlhelp32.h>

#include <cstdio>
#include <cstring>
#include <string>

namespace {

struct Options {
    DWORD process_id = 0;
    std::wstring process_name = L"Grim Dawn.exe";
    std::wstring dll_path;
};

void PrintUsage() {
    std::fwprintf(
        stderr,
        L"Usage: gd_injector.exe [--pid N | --process NAME] [--dll PATH]\n"
        L"Default process: Grim Dawn.exe\n"
        L"Default DLL: gd_helper.dll next to this executable\n");
}

bool ParseArgs(int argc, wchar_t** argv, Options* options) {
    for (int index = 1; index < argc; ++index) {
        const std::wstring argument = argv[index];
        if (argument == L"--help" || argument == L"-h") {
            PrintUsage();
            return false;
        }
        if (argument == L"--pid" && index + 1 < argc) {
            wchar_t* end = nullptr;
            options->process_id = static_cast<DWORD>(std::wcstoul(argv[++index], &end, 10));
            if (end == argv[index] || *end != L'\0' || options->process_id == 0) {
                std::fwprintf(stderr, L"Invalid PID.\n");
                return false;
            }
            continue;
        }
        if (argument == L"--process" && index + 1 < argc) {
            options->process_name = argv[++index];
            continue;
        }
        if (argument == L"--dll" && index + 1 < argc) {
            options->dll_path = argv[++index];
            continue;
        }

        std::fwprintf(stderr, L"Unknown argument: %ls\n", argument.c_str());
        PrintUsage();
        return false;
    }
    return true;
}

DWORD FindProcessIdByName(const std::wstring& process_name) {
    HANDLE snapshot = CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0);
    if (snapshot == INVALID_HANDLE_VALUE) {
        return 0;
    }

    PROCESSENTRY32W entry = {};
    entry.dwSize = sizeof(entry);
    DWORD process_id = 0;
    if (Process32FirstW(snapshot, &entry)) {
        do {
            if (_wcsicmp(entry.szExeFile, process_name.c_str()) == 0) {
                process_id = entry.th32ProcessID;
                break;
            }
        } while (Process32NextW(snapshot, &entry));
    }
    CloseHandle(snapshot);
    return process_id;
}

std::wstring ExecutableDirectory() {
    wchar_t path[MAX_PATH] = {};
    const DWORD length = GetModuleFileNameW(nullptr, path, static_cast<DWORD>(std::size(path)));
    if (length == 0 || length >= std::size(path)) {
        return L".";
    }

    wchar_t* last_slash = wcsrchr(path, L'\\');
    if (last_slash == nullptr) {
        return L".";
    }
    *last_slash = L'\0';
    return path;
}

bool EnableDebugPrivilege() {
    HANDLE token = nullptr;
    if (!OpenProcessToken(GetCurrentProcess(), TOKEN_ADJUST_PRIVILEGES | TOKEN_QUERY, &token)) {
        return false;
    }

    LUID luid = {};
    if (!LookupPrivilegeValueW(nullptr, SE_DEBUG_NAME, &luid)) {
        CloseHandle(token);
        return false;
    }

    TOKEN_PRIVILEGES privileges = {};
    privileges.PrivilegeCount = 1;
    privileges.Privileges[0].Luid = luid;
    privileges.Privileges[0].Attributes = SE_PRIVILEGE_ENABLED;
    const BOOL adjusted = AdjustTokenPrivileges(token, FALSE, &privileges, sizeof(privileges), nullptr, nullptr);
    const DWORD error = GetLastError();
    CloseHandle(token);
    return adjusted && error == ERROR_SUCCESS;
}

bool InjectLibrary(DWORD process_id, const std::wstring& dll_path, DWORD* load_result) {
    if (load_result != nullptr) {
        *load_result = 0;
    }
    if (GetFileAttributesW(dll_path.c_str()) == INVALID_FILE_ATTRIBUTES) {
        std::fwprintf(stderr, L"DLL not found: %ls\n", dll_path.c_str());
        return false;
    }

    const DWORD access = PROCESS_CREATE_THREAD |
                         PROCESS_QUERY_INFORMATION |
                         PROCESS_VM_OPERATION |
                         PROCESS_VM_READ |
                         PROCESS_VM_WRITE;
    HANDLE process = OpenProcess(access, FALSE, process_id);
    if (process == nullptr) {
        std::fwprintf(stderr, L"OpenProcess failed: %lu\n", GetLastError());
        return false;
    }

    const SIZE_T bytes = (dll_path.size() + 1) * sizeof(wchar_t);
    void* remote_path = VirtualAllocEx(process, nullptr, bytes, MEM_COMMIT | MEM_RESERVE, PAGE_READWRITE);
    if (remote_path == nullptr) {
        std::fwprintf(stderr, L"VirtualAllocEx failed: %lu\n", GetLastError());
        CloseHandle(process);
        return false;
    }

    bool ok = false;
    SIZE_T written = 0;
    if (!WriteProcessMemory(process, remote_path, dll_path.c_str(), bytes, &written) || written != bytes) {
        std::fwprintf(stderr, L"WriteProcessMemory failed: %lu\n", GetLastError());
        VirtualFreeEx(process, remote_path, 0, MEM_RELEASE);
        CloseHandle(process);
        return false;
    }

    HMODULE kernel32 = GetModuleHandleW(L"kernel32.dll");
    FARPROC load_library = kernel32 == nullptr ? nullptr : GetProcAddress(kernel32, "LoadLibraryW");
    if (load_library == nullptr) {
        std::fwprintf(stderr, L"LoadLibraryW export not found.\n");
        VirtualFreeEx(process, remote_path, 0, MEM_RELEASE);
        CloseHandle(process);
        return false;
    }

    HANDLE thread = CreateRemoteThread(
        process,
        nullptr,
        0,
        reinterpret_cast<LPTHREAD_START_ROUTINE>(load_library),
        remote_path,
        0,
        nullptr);
    if (thread == nullptr) {
        std::fwprintf(stderr, L"CreateRemoteThread failed: %lu\n", GetLastError());
        VirtualFreeEx(process, remote_path, 0, MEM_RELEASE);
        CloseHandle(process);
        return false;
    }

    const DWORD wait_result = WaitForSingleObject(thread, 20000);
    if (wait_result == WAIT_OBJECT_0) {
        DWORD exit_code = 0;
        if (GetExitCodeThread(thread, &exit_code)) {
            if (load_result != nullptr) {
                *load_result = exit_code;
            }
            ok = exit_code != 0;
        }
    } else {
        std::fwprintf(stderr, L"Remote LoadLibrary timed out.\n");
    }

    CloseHandle(thread);
    VirtualFreeEx(process, remote_path, 0, MEM_RELEASE);
    CloseHandle(process);
    return ok;
}

}  // namespace

int wmain(int argc, wchar_t** argv) {
    Options options;
    if (!ParseArgs(argc, argv, &options)) {
        return 2;
    }

    if (options.dll_path.empty()) {
        options.dll_path = ExecutableDirectory() + L"\\gd_helper.dll";
    }

    if (options.process_id == 0) {
        options.process_id = FindProcessIdByName(options.process_name);
        if (options.process_id == 0) {
            std::fwprintf(stderr, L"Process not found: %ls\n", options.process_name.c_str());
            return 1;
        }
    }

    EnableDebugPrivilege();

    DWORD load_result = 0;
    if (!InjectLibrary(options.process_id, options.dll_path, &load_result)) {
        std::fwprintf(
            stderr,
            L"Injection failed for PID %lu. Run the injector as administrator if access was denied.\n",
            options.process_id);
        return 1;
    }

    std::wprintf(
        L"Injected into PID %lu. Remote module handle: 0x%llX\n",
        options.process_id,
        static_cast<unsigned long long>(load_result));
    return 0;
}
