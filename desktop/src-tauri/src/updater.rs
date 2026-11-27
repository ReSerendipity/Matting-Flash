//! 桌面更新（单一完整包路线）：官网下载与「检查更新」拿同一个 NSIS exe。
//!
//! 设计取舍（见 docs/ADR-CLI.md §5）：本仓没有签名密钥，Tauri 官方 updater 插件
//! 的 endpoints/pubkey 配置无法落地；沿用 TTS/SeedVR2 已验证的自写更新器：
//! GitHub Release 精确匹配 `Matting-Flash-Setup-v{version}.exe`（不回退任何 zip），
//! 下载 + SHA256 校验 + `/S /RELAUNCH` 静默安装，由安装器完成第 7 步自动重启。
//!
//! 事件：update-available / update-progress / update-installing / update-error。
use std::fs::{self, File};
use std::io::{Read, Write};
use std::path::Path;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Mutex;
use std::time::Duration;

use anyhow::{anyhow, Context, Result};
use futures::StreamExt;
use reqwest::Client;
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};
use tauri::{AppHandle, Emitter, Manager, State};

const OWNER: &str = "ReSerendipity";
const REPO: &str = "Matting-Flash";
const ASSET_TEMPLATE: &str = "Matting-Flash-Setup-v{version}.exe";
const SHA_TEMPLATE: &str = "Matting-Flash-Setup-v{version}.exe.sha256";
const HTTP_TIMEOUT: Duration = Duration::from_secs(15);

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct UpdateInfo {
    pub version: String,
    pub current: String,
    pub changelog: String,
    pub url: String,
    pub sha256: String,
    #[serde(default)]
    pub size: u64,
}

#[derive(Debug, Clone, Serialize)]
pub struct UpdateProgress {
    pub phase: &'static str,
    pub percent: u8,
    pub message: String,
}

#[derive(Debug, Default)]
pub struct CachedCheck {
    pub checked: bool,
    pub info: Option<UpdateInfo>,
}

pub struct UpdateState {
    pub busy: AtomicBool,
    pub updating: AtomicBool,
    // UpdateState 本身已在 Mutex 内，cached 不再嵌套锁。
    pub cached: CachedCheck,
}

impl Default for UpdateState {
    fn default() -> Self {
        Self {
            busy: AtomicBool::new(false),
            updating: AtomicBool::new(false),
            cached: CachedCheck::default(),
        }
    }
}

#[derive(Debug, Deserialize)]
struct GhRelease {
    tag_name: String,
    #[serde(default)]
    body: Option<String>,
    #[serde(default)]
    assets: Vec<GhAsset>,
    #[serde(default)]
    draft: bool,
}

#[derive(Debug, Deserialize)]
struct GhAsset {
    name: String,
    url: String,
    #[serde(default)]
    browser_download_url: Option<String>,
    #[serde(default)]
    size: Option<u64>,
}

pub fn normalize_version(raw: &str) -> String {
    raw.trim().trim_start_matches(['v', 'V']).to_string()
}

fn parse_semver(raw: &str) -> Result<semver::Version> {
    semver::Version::parse(&normalize_version(raw))
        .with_context(|| format!("版本号无法解析: {raw}"))
}

fn asset_name(template: &str, version: &str) -> String {
    template.replace("{version}", &normalize_version(version))
}

fn resolve_assets(release: &GhRelease) -> Result<(String, u64, String)> {
    let version = normalize_version(&release.tag_name);
    let exe_name = asset_name(ASSET_TEMPLATE, &version);
    let sha_name = asset_name(SHA_TEMPLATE, &version);
    // 单一路线：只认精确完整 exe 名；不再回退增量 zip / 其它命名资产。
    let exe = release
        .assets
        .iter()
        .find(|a| a.name == exe_name)
        .ok_or_else(|| anyhow!("更新包不存在: 未找到 {exe_name}"))?;
    let url = exe
        .browser_download_url
        .clone()
        .unwrap_or_else(|| exe.url.clone());
    let sha_url = release
        .assets
        .iter()
        .find(|a| a.name == sha_name)
        .map(|a| a.browser_download_url.clone().unwrap_or_else(|| a.url.clone()))
        .unwrap_or_default();
    Ok((url, exe.size.unwrap_or(0), sha_url))
}

fn extract_hex64(text: &str) -> Option<String> {
    text.split(|c: char| !c.is_ascii_hexdigit())
        .find(|t| t.len() == 64)
        .map(|t| t.to_ascii_lowercase())
}

fn build_client(timeout: Duration) -> Result<Client> {
    Client::builder()
        .timeout(timeout)
        .user_agent("Matting-Flash-Desktop")
        .build()
        .context("创建 HTTP 客户端失败")
}

async fn fetch_latest(client: &Client) -> Result<GhRelease> {
    let url = format!("https://api.github.com/repos/{OWNER}/{REPO}/releases?per_page=10");
    let resp = client
        .get(&url)
        .header("Accept", "application/vnd.github+json")
        .send()
        .await
        .context("请求 GitHub Releases 失败")?;
    if resp.status() == reqwest::StatusCode::FORBIDDEN {
        return Err(anyhow!("GitHub API 访问受限（速率限制或鉴权失败）"));
    }
    let releases: Vec<GhRelease> = resp.error_for_status().context("Releases 返回错误状态")?
        .json()
        .await
        .context("解析 Releases JSON 失败")?;
    releases
        .into_iter()
        .filter(|r| !r.draft && parse_semver(&r.tag_name).is_ok())
        .max_by(|a, b| {
            parse_semver(&a.tag_name).unwrap().cmp(&parse_semver(&b.tag_name).unwrap())
        })
        .ok_or_else(|| anyhow!("未找到已发布版本"))
}

async fn fetch_sha256(client: &Client, body: Option<&str>, sha_url: &str) -> Result<String> {
    if let Some(text) = body {
        if let Some(hash) = extract_hex64(text) {
            return Ok(hash);
        }
    }
    if !sha_url.is_empty() {
        let resp = client
            .get(sha_url)
            .send()
            .await
            .context("下载 SHA256 文件失败")?;
        if resp.status().is_success() {
            let text = resp.text().await.context("读取 SHA256 文本失败")?;
            if let Some(hash) = extract_hex64(&text) {
                return Ok(hash);
            }
        }
    }
    Err(anyhow!("未找到 SHA256 校验值"))
}

async fn do_check() -> Result<Option<UpdateInfo>> {
    let current = env!("CARGO_PKG_VERSION");
    let client = build_client(HTTP_TIMEOUT)?;
    let release = fetch_latest(&client).await?;
    let version = normalize_version(&release.tag_name);
    if parse_semver(&version)?.le(&parse_semver(current)?) {
        return Ok(None);
    }
    let (url, size, sha_url) = resolve_assets(&release)?;
    let sha256 = fetch_sha256(&client, release.body.as_deref(), &sha_url).await?;
    Ok(Some(UpdateInfo {
        version,
        current: current.to_string(),
        changelog: release.body.unwrap_or_default(),
        url,
        sha256,
        size,
    }))
}

#[tauri::command]
pub async fn check_update(app: AppHandle, auto: bool) -> Result<Option<UpdateInfo>, String> {
    let state = app.state::<Mutex<UpdateState>>();
    let (busy, updating) = {
        let guard = state.lock().map_err(|e| e.to_string())?;
        let busy = guard.busy.swap(true, Ordering::SeqCst);
        let updating = guard.updating.load(Ordering::SeqCst);
        if busy || updating {
            guard.busy.store(false, Ordering::SeqCst);
        }
        (busy, updating)
    };
    if busy || updating {
        return Err("已有更新检查/安装在进行中".into());
    }
    let result = do_check().await;
    {
        let mut guard = state.lock().map_err(|e| e.to_string())?;
        guard.busy.store(false, Ordering::SeqCst);
        if let Ok(Some(info)) = &result {
            guard.cached.checked = true;
            guard.cached.info = Some(info.clone());
        }
    }
    match result {
        Ok(info) => {
            if let Some(i) = &info {
                let _ = app.emit("update-available", i);
            }
            Ok(info)
        }
        Err(e) => {
            if auto {
                log::warn!("自动检查更新失败（静默）: {e:#}");
                Ok(None)
            } else {
                Err(format!("{e:#}"))
            }
        }
    }
}

#[tauri::command]
pub fn get_update_state(app: AppHandle) -> Result<serde_json::Value, String> {
    let state = app.state::<Mutex<UpdateState>>();
    let guard = state.lock().map_err(|e| e.to_string())?;
    Ok(serde_json::json!({
        "app_version": env!("CARGO_PKG_VERSION"),
        "shell_version": env!("CARGO_PKG_VERSION"),
        "pending": {
            "checked": guard.cached.checked,
            "info": guard.cached.info.clone(),
        },
    }))
}

#[tauri::command]
pub fn get_pending_update(app: AppHandle) -> Option<UpdateInfo> {
    app.state::<Mutex<UpdateState>>()
        .lock()
        .ok()
        .and_then(|g| g.cached.info.clone())
}

#[tauri::command]
pub fn dismiss_update(app: AppHandle) {
    if let Ok(mut guard) = app.state::<Mutex<UpdateState>>().lock() {
        guard.cached.info = None;
    }
}

fn emit_progress(app: &AppHandle, phase: &'static str, percent: u8, message: impl Into<String>) {
    let _ = app.emit(
        "update-progress",
        UpdateProgress { phase, percent, message: message.into() },
    );
}

fn sha256_of(path: &Path) -> Result<String> {
    let mut file = File::open(path).context("打开安装包失败")?;
    let mut hasher = Sha256::new();
    let mut buf = vec![0u8; 64 * 1024];
    loop {
        let n = file.read(&mut buf).context("读取安装包失败")?;
        if n == 0 {
            break;
        }
        hasher.update(&buf[..n]);
    }
    Ok(format!("{:x}", hasher.finalize()))
}

async fn download(client: &Client, url: &str, dest: &Path, total: u64, app: &AppHandle) -> Result<u64> {
    let resp = client
        .get(url)
        .send()
        .await
        .context("连接下载服务器失败")?
        .error_for_status()
        .context("下载请求被拒绝")?;
    let mut file = File::create(dest).context("创建安装包失败")?;
    let mut stream = resp.bytes_stream();
    let mut written = 0u64;
    let mut last = 0u8;
    while let Some(chunk) = stream.next().await {
        let chunk = chunk.context("下载流中断")?;
        file.write_all(&chunk)?;
        written += chunk.len() as u64;
        if total > 0 {
            let pct = ((written * 90) / total.max(1)).min(90) as u8;
            if pct >= last + 5 || written >= total {
                last = pct;
                emit_progress(app, "download", pct, format!("下载中 {pct}%"));
            }
        }
    }
    file.flush()?;
    Ok(written)
}

/// 传给安装器的参数：/S 静默 + /RELAUNCH 装完自动拉起应用（自写 NSIS 的第 7 步）。
fn installer_args() -> Vec<&'static str> {
    vec!["/S", "/RELAUNCH"]
}

#[cfg(windows)]
fn launch_installer(exe: &Path) -> Result<()> {
    use std::os::windows::process::CommandExt;
    const DETACHED_PROCESS: u32 = 0x0000_0008;
    const CREATE_NEW_PROCESS_GROUP: u32 = 0x0000_0200;
    let mut cmd = std::process::Command::new(exe);
    cmd.args(installer_args())
        .creation_flags(DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP)
        .spawn()
        .with_context(|| format!("启动安装器失败: {}", exe.display()))?;
    Ok(())
}

#[cfg(not(windows))]
fn launch_installer(exe: &Path) -> Result<()> {
    Err(anyhow!("完整安装包更新仅支持 Windows: {}", exe.display()))
}

async fn do_update_steps(app: &AppHandle, info: &UpdateInfo) -> Result<()> {
    let client = build_client(Duration::from_secs(3600))?;
    let updates_dir = crate::paths::local_app_dir().join("updates");
    fs::create_dir_all(&updates_dir).context("创建 updates 目录失败")?;
    let exe_name = asset_name(ASSET_TEMPLATE, &info.version);
    let exe_path = updates_dir.join(&exe_name);
    let _ = fs::remove_file(&exe_path);

    emit_progress(app, "download", 0, "开始下载完整安装包…");
    let downloaded = download(&client, &info.url, &exe_path, info.size, app).await?;
    if info.size > 0 && downloaded != info.size {
        let _ = fs::remove_file(&exe_path);
        return Err(anyhow!("下载不完整（{downloaded} / {} 字节）", info.size));
    }

    emit_progress(app, "verify", 0, "校验 SHA256…");
    let actual = sha256_of(&exe_path)?;
    if actual != info.sha256 {
        let _ = fs::remove_file(&exe_path);
        return Err(anyhow!("SHA256 校验失败，已丢弃安装包"));
    }
    emit_progress(app, "verify", 100, "校验通过");

    // 所有事件必须在 spawn / exit 前发完：安装器 KillRunning 会杀掉本进程。
    emit_progress(app, "install", 0, "正在启动安装程序…");
    let _ = app.emit(
        "update-installing",
        serde_json::json!({
            "version": info.version,
            "message": "应用即将关闭，安装程序将自动完成更新并重新启动。",
        }),
    );
    launch_installer(&exe_path)?;
    log::info!("安装器已启动: {} -> {}（由安装器负责重启）", info.current, info.version);
    std::process::exit(0);
}

#[tauri::command]
pub fn start_update(app: AppHandle, state: State<'_, Mutex<UpdateState>>) -> Result<(), String> {
    let info = {
        let guard = state.lock().map_err(|e| e.to_string())?;
        if guard.updating.swap(true, Ordering::SeqCst) {
            return Err("更新正在进行中".into());
        }
        guard.cached.info.clone().ok_or_else(|| "请先检查更新".to_string())?
    };
    let handle = app.clone();
    tauri::async_runtime::spawn(async move {
        match do_update_steps(&handle, &info).await {
            Ok(()) => {
                let _ = handle.emit("update-done", serde_json::json!({"version": info.version}));
            }
            Err(e) => {
                let _ = handle.emit("update-error", serde_json::json!({"message": format!("{e:#}")}));
            }
        }
        if let Ok(guard) = handle.state::<Mutex<UpdateState>>().lock() {
            guard.updating.store(false, Ordering::SeqCst);
        }
    });
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    fn fixture(tag: &str) -> GhRelease {
        GhRelease {
            draft: false,
            tag_name: tag.into(),
            body: Some(format!("## 修复\n- sha256: {}", "a".repeat(64))),
            assets: vec![
                GhAsset {
                    name: asset_name(ASSET_TEMPLATE, tag),
                    url: "https://example.com/setup.exe".into(),
                    browser_download_url: Some("https://example.com/setup.exe".into()),
                    size: Some(1024),
                },
                GhAsset {
                    name: asset_name(SHA_TEMPLATE, tag),
                    url: "https://example.com/setup.exe.sha256".into(),
                    browser_download_url: Some("https://example.com/setup.exe.sha256".into()),
                    size: Some(65),
                },
                GhAsset {
                    name: "app-v1.5.2.zip".into(),
                    url: "https://example.com/old.zip".into(),
                    browser_download_url: None,
                    size: Some(9),
                },
            ],
        }
    }

    #[test]
    fn normalize_and_semver_compare() {
        assert_eq!(normalize_version(" v1.10.0 "), "1.10.0");
        assert!(parse_semver("v1.10.0").unwrap() > parse_semver("1.9.0").unwrap());
    }

    #[test]
    fn resolve_assets_only_accepts_exact_full_installer() {
        let (url, size, sha_url) = resolve_assets(&fixture("v1.5.2")).unwrap();
        assert_eq!(url, "https://example.com/setup.exe");
        assert_eq!(size, 1024);
        assert!(sha_url.ends_with(".exe.sha256"));

        let mut only_zip = fixture("v1.5.2");
        only_zip.assets.retain(|a| !a.name.starts_with("Matting-Flash-Setup"));
        assert!(resolve_assets(&only_zip).is_err(), "增量 zip 不得作为更新资产");
    }

    #[test]
    fn installer_args_request_silent_and_relaunch() {
        let args = installer_args();
        assert!(args.contains(&"/S"));
        assert!(args.contains(&"/RELAUNCH"));
    }

    #[test]
    fn sha256_known_vector_and_extract() {
        let dir = std::env::temp_dir().join(format!("matting_updater_sha_{}", std::process::id()));
        fs::create_dir_all(&dir).unwrap();
        let p = dir.join("abc.bin");
        fs::write(&p, b"abc").unwrap();
        assert_eq!(
            sha256_of(&p).unwrap(),
            "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
        );
        assert_eq!(
            extract_hex64("SHA256: BA7816BF8F01CFEA414140DE5DAE2223B00361A396177A9CB410FF61F20015AD"),
            Some("ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad".into())
        );
        let _ = fs::remove_dir_all(&dir);
    }
}
