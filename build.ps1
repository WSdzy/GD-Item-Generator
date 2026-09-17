$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$native = Join-Path $root "native"
$output = Join-Path $root "bin"
$compiler = "D:\Program Files (x86)\mingw-w64\mingw64\bin\x86_64-w64-mingw32-g++.exe"

if (-not (Test-Path -LiteralPath $compiler)) {
    throw "MinGW x64 compiler not found: $compiler"
}

New-Item -ItemType Directory -Force -Path $output | Out-Null

$common = @(
    "-std=c++17",
    "-O2",
    "-Wall",
    "-Wextra",
    "-DUNICODE",
    "-D_UNICODE",
    "-static",
    "-static-libgcc",
    "-static-libstdc++"
)

& $compiler @common `
    "-shared" `
    "-o" (Join-Path $output "gd_helper.dll") `
    (Join-Path $native "gd_helper.cpp")

& $compiler @common `
    "-municode" `
    "-o" (Join-Path $output "gd_injector.exe") `
    (Join-Path $native "gd_injector.cpp")

Get-Item `
    (Join-Path $output "gd_helper.dll"), `
    (Join-Path $output "gd_injector.exe") |
    Select-Object FullName, Length, LastWriteTime |
    Format-Table -AutoSize
