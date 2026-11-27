; Matting-Flash 桌面版 NSIS 当前用户安装器
; 编译：makensis /DAPP_VERSION=... /DSRC_DIR=... /DOUT_DIR=... /DSRC_SHELL=... /DSRC_7ZA=... /DAPP_ICON=... /DESTIMATED_SIZE_KB=... setup.nsi
; 数据分卷 Matting-Flash-Data.7z.001..NNN 与安装器同目录分发（安装期从 $EXEDIR 读取，缺卷明确报错）。
; 必须用 UTF-8 BOM 保存（含中文 DetailPrint / MessageBox）。
Unicode true
!include "MUI2.nsh"
!include "LogicLib.nsh"
!include "StrFunc.nsh"
; CLI 全局命令：PATH 幂等追加与卸载精确移除（安装/卸载分别 Using，不能混用）
${Using:StrFunc} StrStr
${Using:StrFunc} UnStrStr
${Using:StrFunc} UnStrRep

!define APP_NAME "Matting-Flash"
!define APP_DISPLAY_NAME "Matting-Flash 抠图工作台"
!define APP_EXE "Matting-Flash.exe"
!define UNINST_KEY "Software\Microsoft\Windows\CurrentVersion\Uninstall\Matting-Flash"
!define APP_REG_KEY "Software\Matting-Flash"
!define USER_DATA_DIR "$LOCALAPPDATA\Matting-Flash"
!define DATA_PREFIX "Matting-Flash-Data.7z"

!ifndef APP_VERSION
  !define APP_VERSION "0.1.0"
!endif
!ifndef OUT_DIR
  !define OUT_DIR "."
!endif
!ifndef SRC_DIR
  !define SRC_DIR "..\..\dist\desktop-staging"
!endif
!ifndef SRC_SHELL
  !error "缺少 -DSRC_SHELL=<Matting-Flash.exe 路径>"
!endif
!ifndef SRC_7ZA
  !define SRC_7ZA "7za.exe"
!endif
!ifndef APP_ICON
  !define APP_ICON "..\..\desktop\src-tauri\icons\icon.ico"
!endif
!ifndef ESTIMATED_SIZE_KB
  !define ESTIMATED_SIZE_KB "0"
!endif

Name "${APP_DISPLAY_NAME}"
OutFile "${OUT_DIR}\${APP_NAME}-Setup-v${APP_VERSION}.exe"
InstallDir "$LOCALAPPDATA\Programs\${APP_NAME}"
InstallDirRegKey HKCU "${APP_REG_KEY}" "InstallDir"
RequestExecutionLevel user
SetCompressor lzma
CRCCheck on
BrandingText "${APP_NAME}"

Var StartMenuFolder
Var TOOLSDIR
Var DATADIR
Var KEEP_DATA

; ---------- 终止运行中的壳与 Python 子进程 ----------
!macro KillRunning
  DetailPrint "检测到 ${APP_NAME} 可能正在运行，正在终止..."
  nsExec::ExecToLog 'taskkill /f /im ${APP_EXE} /T'
  Pop $0
  Sleep 1500
!macroend

; ---------- CLI 全局命令：HKCU PATH 幂等追加 / 卸载精确移除 ----------
; 用 ";...;" 包裹整条 PATH 后匹配 ";<INSTDIR>;"，避免把 "C:\Foo\Matting-Flash-Extra"
; 误判为命中，也避免追加时产生粘连条目。Windows 路径大小写差异在 NSIS StrStr 中
; 视为不同条目（保守处理：至多留一条无害重复项，不误删他人条目）。
Function AddToUserPath
  Push $0
  Push $1
  Push $2
  Push $3
  ReadRegStr $1 HKCU "Environment" "Path"
  StrCpy $0 "$1" 1 -1
  ${While} $0 == ";"
    StrCpy $1 "$1" -1
    StrCpy $0 "$1" 1 -1
  ${EndWhile}
  StrCpy $2 ";$1;"
  ${StrStr} $3 "$2" ";$INSTDIR;"
  ${If} $3 == ""
    ${If} $1 == ""
      StrCpy $1 "$INSTDIR"
    ${Else}
      StrCpy $0 "$1" 1 -1
      ${If} $0 == ";"
        StrCpy $1 "$1$INSTDIR"
      ${Else}
        StrCpy $1 "$1;$INSTDIR"
      ${EndIf}
    ${EndIf}
    WriteRegExpandStr HKCU "Environment" "Path" "$1"
    SendMessage ${HWND_BROADCAST} ${WM_WININICHANGE} 0 "STR:Environment" /TIMEOUT=5000
    DetailPrint "已将 CLI 命令目录加入当前用户 PATH: $INSTDIR"
  ${Else}
    DetailPrint "CLI 命令目录已在当前用户 PATH 中，跳过"
  ${EndIf}
  Pop $3
  Pop $2
  Pop $1
  Pop $0
FunctionEnd

Function un.RemoveFromUserPath
  Push $0
  Push $1
  Push $2
  Push $3
  ReadRegStr $1 HKCU "Environment" "Path"
  ; 先归一化尾部分隔符，再循环移除所有精确条目与重复分号
  StrCpy $0 "$1" 1 -1
  ${While} $0 == ";"
    StrCpy $1 "$1" -1
    StrCpy $0 "$1" 1 -1
  ${EndWhile}
  StrCpy $1 ";$1;"
  ${UnStrStr} $2 "$1" ";$INSTDIR;"
  ${IfNot} $2 == ""
    ${While} $2 != ""
      ${UnStrRep} $1 "$1" ";$INSTDIR;" ";"
      ${UnStrStr} $2 "$1" ";$INSTDIR;"
    ${EndWhile}
    ${While} $2 != ""
      ${UnStrRep} $1 "$1" ";;" ";"
      ${UnStrStr} $2 "$1" ";;"
    ${EndWhile}
    StrCpy $1 "$1" "" 1
    StrCpy $1 "$1" -1
    WriteRegExpandStr HKCU "Environment" "Path" "$1"
    SendMessage ${HWND_BROADCAST} ${WM_WININICHANGE} 0 "STR:Environment" /TIMEOUT=5000
    DetailPrint "已从当前用户 PATH 移除 CLI 命令目录: $INSTDIR"
  ${EndIf}
  Pop $3
  Pop $2
  Pop $1
  Pop $0
FunctionEnd

; ---------- MUI 页面 ----------
!define MUI_ABORTWARNING
!define MUI_ICON "${APP_ICON}"
!define MUI_UNICON "${APP_ICON}"
!insertmacro MUI_PAGE_WELCOME
!insertmacro MUI_PAGE_LICENSE "${SRC_DIR}\app\LICENSE"
!insertmacro MUI_PAGE_DIRECTORY
!insertmacro MUI_PAGE_STARTMENU "Application" $StartMenuFolder
!insertmacro MUI_PAGE_INSTFILES
!define MUI_FINISHPAGE_RUN "$INSTDIR\${APP_EXE}"
!define MUI_FINISHPAGE_RUN_TEXT "立即运行 ${APP_DISPLAY_NAME}"
!insertmacro MUI_PAGE_FINISH
!insertmacro MUI_UNPAGE_CONFIRM
!insertmacro MUI_UNPAGE_INSTFILES
!insertmacro MUI_LANGUAGE "SimpChinese"

; ---------- 安装 ----------
Section "${APP_DISPLAY_NAME}" SEC_APP
  StrCpy $TOOLSDIR "$TEMP\matting-flash-installer"
  RMDir /r "$TOOLSDIR"
  CreateDirectory "$TOOLSDIR"

  !insertmacro KillRunning

  ; 数据分卷与安装器同目录分发：优先 $EXEDIR，其次 $EXEDIR\data。避免把 2GB+
  ; 运行时嵌进安装器（NSIS 32 位壳对大安装包不友好），也避免解压前再复制一份。
  StrCpy $DATADIR "$EXEDIR"
  ${IfNot} ${FileExists} "$DATADIR\${DATA_PREFIX}.001"
    ${If} ${FileExists} "$EXEDIR\data\${DATA_PREFIX}.001"
      StrCpy $DATADIR "$EXEDIR\data"
    ${EndIf}
  ${EndIf}
  ${IfNot} ${FileExists} "$DATADIR\${DATA_PREFIX}.001"
    ${IfNot} ${Silent}
      MessageBox MB_ICONSTOP "缺少数据分卷 ${DATA_PREFIX}.001。$\r$\n请确认 ${APP_NAME}-Data.7z.001..NNN 与安装器位于同一目录后重试（SHA256 见 SHA256SUMS）。"
    ${EndIf}
    Abort
  ${EndIf}

  ; 独立 7za 落临时工具目录（7z.exe 依赖 7z.dll，不可嵌入）
  SetOutPath "$TOOLSDIR"
  File /oname=7za.exe "${SRC_7ZA}"
  ${IfNot} ${FileExists} "$TOOLSDIR\7za.exe"
    ${IfNot} ${Silent}
      MessageBox MB_ICONSTOP "安装器内嵌的解压工具缺失，安装器可能已损坏，请重新获取。"
    ${EndIf}
    Abort
  ${EndIf}

  CreateDirectory "$INSTDIR"
  DetailPrint "正在解压应用与推理运行时（约需 3-6 分钟，视磁盘速度）..."
  ExecWait '"$TOOLSDIR\7za.exe" x "$DATADIR\${DATA_PREFIX}.001" -o"$INSTDIR" -y -bso0 -bsp0' $0
  Delete "$TOOLSDIR\7za.exe"
  ${If} $0 != 0
    ${IfNot} ${Silent}
      MessageBox MB_ICONSTOP "数据解压失败（7z 错误码 $0）。$\r$\n请检查：1) 磁盘空间是否充足（约需 6GB）；2) 数据分卷是否齐全（SHA256SUMS 校验）；3) 分卷是否与安装器同目录。"
    ${EndIf}
    Abort
  ${EndIf}

  ; 壳 exe（数据卷只含 app/，不含壳）
  SetOutPath "$INSTDIR"
  File /oname=${APP_EXE} "${SRC_SHELL}"
  ${IfNot} ${FileExists} "$INSTDIR\${APP_EXE}"
    ${IfNot} ${Silent}
      MessageBox MB_ICONSTOP "安装异常：未找到 ${APP_EXE}。"
    ${EndIf}
    Abort
  ${EndIf}
  ${IfNot} ${FileExists} "$INSTDIR\app\scripts\desktop_launch.py"
    ${IfNot} ${Silent}
      MessageBox MB_ICONSTOP "安装异常：应用代码不完整（缺少 app\scripts\desktop_launch.py）。"
    ${EndIf}
    Abort
  ${EndIf}

  ; 用户数据目录（权重/结果/日志）安装期创建，覆盖安装保留
  CreateDirectory "${USER_DATA_DIR}"
  CreateDirectory "${USER_DATA_DIR}\weights"
  CreateDirectory "${USER_DATA_DIR}\results"
  CreateDirectory "${USER_DATA_DIR}\logs"

  ; 快捷方式（桌面 + 开始菜单）
  !insertmacro MUI_STARTMENU_WRITE_BEGIN Application
    CreateDirectory "$SMPROGRAMS\$StartMenuFolder"
    CreateShortcut "$SMPROGRAMS\$StartMenuFolder\${APP_DISPLAY_NAME}.lnk" "$INSTDIR\${APP_EXE}"
  !insertmacro MUI_STARTMENU_WRITE_END
  CreateShortcut "$DESKTOP\${APP_DISPLAY_NAME}.lnk" "$INSTDIR\${APP_EXE}"

  ; HKCU 注册
  WriteRegStr HKCU "${APP_REG_KEY}" "InstallDir" "$INSTDIR"
  WriteRegStr HKCU "${UNINST_KEY}" "DisplayName" "${APP_DISPLAY_NAME}"
  WriteRegStr HKCU "${UNINST_KEY}" "DisplayVersion" "${APP_VERSION}"
  WriteRegStr HKCU "${UNINST_KEY}" "Publisher" "ReSerendipity"
  WriteRegStr HKCU "${UNINST_KEY}" "DisplayIcon" "$INSTDIR\${APP_EXE}"
  WriteRegStr HKCU "${UNINST_KEY}" "UninstallString" '"$INSTDIR\uninstall.exe"'
  WriteRegStr HKCU "${UNINST_KEY}" "InstallLocation" "$INSTDIR"
  WriteRegDWORD HKCU "${UNINST_KEY}" "NoModify" 1
  WriteRegDWORD HKCU "${UNINST_KEY}" "NoRepair" 1
  WriteRegStr HKCU "${UNINST_KEY}" "QuietUninstallString" '"$INSTDIR\uninstall.exe" /S'
  WriteRegDWORD HKCU "${UNINST_KEY}" "EstimatedSize" ${ESTIMATED_SIZE_KB}

  ; ---------- CLI 全局命令：shim 放在 PATH 根目录，注册后任意目录可敲 matting ----------
  SetOutPath "$INSTDIR"
  File "matting.cmd"
  Call AddToUserPath

  ; 开始菜单 CLI 快捷方式：cmd.exe 用 $SYSDIR（COMSPEC 编译期未知）
  !insertmacro MUI_STARTMENU_WRITE_BEGIN Application
    CreateShortcut "$SMPROGRAMS\$StartMenuFolder\Matting-Flash CLI.lnk" "$SYSDIR\cmd.exe" '/k "$INSTDIR\matting.cmd" --help'
  !insertmacro MUI_STARTMENU_WRITE_END

  WriteUninstaller "$INSTDIR\uninstall.exe"

  ; ---------- 自更新第 7 步：装完自动重启应用 ----------
  ; Rust updater 以 `/S /RELAUNCH` 启动本安装包；普通安装不带该标志，不受影响。
  ; 用 $CMDLINE + StrStr 检测，避免引入额外插件；路径含空格由引号包裹。
  ${StrStr} $0 "$CMDLINE" "/RELAUNCH"
  ${If} $0 != ""
    DetailPrint "更新安装完成，正在重新启动 ${APP_DISPLAY_NAME}…"
    Exec '"$INSTDIR\${APP_EXE}"'
  ${EndIf}

  RMDir /r "$TOOLSDIR"
SectionEnd

; ---------- 版本信息 ----------
VIProductVersion "${APP_VERSION}.0"
VIAddVersionKey "ProductName" "${APP_DISPLAY_NAME}"
VIAddVersionKey "ProductVersion" "${APP_VERSION}"
VIAddVersionKey "FileDescription" "${APP_DISPLAY_NAME} 安装程序"
VIAddVersionKey "FileVersion" "${APP_VERSION}"
VIAddVersionKey "CompanyName" "ReSerendipity"
VIAddVersionKey "LegalCopyright" "Copyright (C) 2026 ReSerendipity"

; ---------- 卸载 ----------
Section "Uninstall"
  !insertmacro KillRunning

  Delete "$DESKTOP\${APP_DISPLAY_NAME}.lnk"
  !insertmacro MUI_STARTMENU_GETFOLDER Application $StartMenuFolder
  Delete "$SMPROGRAMS\$StartMenuFolder\${APP_DISPLAY_NAME}.lnk"
  Delete "$SMPROGRAMS\$StartMenuFolder\Matting-Flash CLI.lnk"
  RMDir "$SMPROGRAMS\$StartMenuFolder"
  Delete "$INSTDIR\matting.cmd"
  ; CLI 全局命令：先精确移除 PATH 条目，再删注册表键
  Call un.RemoveFromUserPath

  DeleteRegKey HKCU "${APP_REG_KEY}"
  DeleteRegKey HKCU "${UNINST_KEY}"

  ; 保留用户数据：静默卸载默认保留（/SD IDYES），交互卸载默认答「是」
  StrCpy $KEEP_DATA "1"
  MessageBox MB_YESNO|MB_ICONQUESTION|MB_DEFBUTTON1 \
    "是否保留用户数据（模型权重、结果与日志）？$\r$\n选择「是」保留，选择「否」一并删除 ${USER_DATA_DIR}。" \
    /SD IDYES IDYES matting_keep_data
  StrCpy $KEEP_DATA "0"
  matting_keep_data:

  ; 只删程序：app/ 代码与 runtime、壳 exe、卸载器
  RMDir /r "$INSTDIR\app"
  Delete "$INSTDIR\${APP_EXE}"
  Delete "$INSTDIR\uninstall.exe"
  RMDir /r "$INSTDIR"
  StrCpy $R0 0
  ${DoWhile} ${FileExists} "$INSTDIR\${APP_EXE}"
    IntOp $R0 $R0 + 1
    ${If} $R0 >= 15
      ${Break}
    ${EndIf}
    Sleep 1000
    Delete "$INSTDIR\${APP_EXE}"
  ${Loop}

  ${If} $KEEP_DATA == "0"
    DetailPrint "按用户选择删除用户数据：${USER_DATA_DIR}"
    RMDir /r "${USER_DATA_DIR}"
  ${Else}
    DetailPrint "已保留用户数据：${USER_DATA_DIR}"
  ${EndIf}

  ; 兜底：卸载器退出后延迟清理仍被句柄占用的安装目录
  nsExec::Exec 'cmd /c ping -n 4 127.0.0.1 >nul & rd /s /q "$INSTDIR"'
  Pop $0
SectionEnd
