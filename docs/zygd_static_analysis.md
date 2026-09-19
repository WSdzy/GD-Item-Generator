# zyGD v2.1.1a x64：静态分析标记

> 本文仅记录对 `zyGD_v2.1.1a_x64.exe` 的静态观察，不能视为其作者公开的 API 文档，也不能作为本项目功能、兼容性或安全性的依据。

## 样本与范围

- 文件：`zyGD_v2.1.1a_x64.exe`
- SHA-256：`496CC3444C75122EC08A12FC185FC11389055A02260B9CC215EDCB18B4DD5621`
- 文件未签名；本分析没有运行 EXE 或其内嵌 DLL。
- 仅检查了 PE 头、导入、字符串、资源和已有配置文本。

## 已观察到的结构

1. 主 EXE 是 x64 GUI 启动器，资源 `RCDATA #100` 内嵌一个 867,840 B 的 `MZ` DLL（字符串名称为 `zyGD.dll`）。
2. 内嵌 DLL 没有导出表，且使用 `PACK0/PACK1` 压缩/保护段；游戏相关逻辑、地址和内部命令不能由本次静态检查可靠还原。
3. 主程序包含或动态解析下列注入相关 API 名称：`CreateToolhelp32Snapshot`、`Process32FirstW`、`Process32NextW`、`OpenProcess`、`VirtualAllocEx`、`WriteProcessMemory`、`CreateRemoteThread`、`LoadLibraryW`、`VirtualFreeEx`。
4. 发现命名对象字符串 `Local\zyGD_DataDir` 与 `Local\GrimDawnTrainer_Ready`。前者**可能**用于向 DLL 传递数据目录，后者**可能**是 DLL 就绪事件；对象类型和完整协议未验证。

## 已证实的外部文件约定

- `zyGD\zygd.cfg`：INI 格式；已观察到 `[loader]` 下的 `auto_teleport=1`。
- `zyGD\custom_tp.txt`：自定义坐标格式为 `名称|X|Y|Z`，可使用 `[分类]` 前缀。
- `zyGD\tp_favorites.txt`：传送点收藏数据文件。

因此，“自动传送 / 自定义传送点 / 收藏传送点”有实际文件证据。物品属性修改、角色属性修改、自动拾取、技能或任务自动化等仅是可能性，尚未获得同等级别的证据。

## 不应据此推断的内容

- Windows API 出现不等于相关功能已实现。
- 不应假定其内存偏移、物品结构或角色结构适用于本项目或当前游戏版本。
- 不应直接复用其受保护 DLL、内存地址或配置作为本项目接口。

## 若需进一步验证

应在隔离环境中进行受控动态观察：仅记录模块加载、命名对象、配置 I/O 与窗口消息，再逐项验证功能。未经验证不得向本项目添加相应功能声明。
