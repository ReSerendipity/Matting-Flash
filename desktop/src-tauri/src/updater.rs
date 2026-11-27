//! 桌面更新（完整安装资产集路线）。
//!
//! 本仓没有签名密钥，Tauri 官方 updater 插件的 endpoints/pubkey 配置无法落地；沿用自写更新器：
//! GitHub Release 必须提供唯一 `Matting-Flash-Setup-v<version>.exe`、连续的
//! `Matting-Flash-Data.7z.001..NNN` 和唯一 `SHA256SUMS`。所有资产流式下载到
//! `%LOCALAPPDATA%\Matting-Flash\updates\<version>`，逐文件校验通过后才以
//! `/S /RELAUNCH` 启动安装器。
//!
//! 事件：update-available / update-progress / update-installing / update-error。
use std::collections::{BTreeMap, BTreeSet};
use std::fs::{self, File};
use std::io::{Read, Write};
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Mutex;
use std::time::Duration;

use anyhow::{anyhow, Context, Result};
use futures::{Stream, StreamExt};
use reqwest::Client;
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};
use tauri::{AppHandle, Emitter, Manager, State};

const OWNER: &str = "ReSerendipity";
const REPO: &str = "Matting-Flash";
const ASSET_TEMPLATE: &str = "Matting-Flash-Setup-v{version}.exe";
const DATA_PREFIX: &str = "Matting-Flash-Data.7z.";
const SUMS_NAME: &str = "SHA256SUMS";
const HTTP_TIMEOUT: Duration = Duration::from_secs(15);

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum AssetKind {
    Setup,
    Data,
    Sums,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct UpdateAsset {
    pub name: String,
    pub url: String,
    #[serde(default)]
    pub size: u64,
    pub kind: AssetKind,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct UpdateInfo {
    pub version: String,
    pub current: String,
    pub changelog: String,
    pub assets: Vec<UpdateAsset>,
    pub total_bytes: u64,
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

fn is_https(url: &str) -> bool {
    reqwest::Url::parse(url).is_ok_and(|parsed| parsed.scheme() == "https")
}

fn volume_number(name: &str) -> Result<u32> {
    let suffix = name
        .strip_prefix(DATA_PREFIX)
        .ok_or_else(|| anyhow!("更新资产含未知文件: {name}"))?;
    let digits = suffix.strip_prefix('.').unwrap_or(suffix);
    if digits.len() != 3 || !digits.bytes().all(|b| b.is_ascii_digit()) {
        return Err(anyhow!("数据分卷名非法（应为 .001..NNN）: {name}"));
    }
    let number = digits.parse::<u32>().context("数据分卷号无法解析")?;
    if number == 0 {
        return Err(anyhow!("数据分卷必须从 .001 开始: {name}"));
    }
    Ok(number)
}

fn gh_asset_url(asset: &GhAsset) -> String {
    asset
        .browser_download_url
        .clone()
        .unwrap_or_else(|| asset.url.clone())
}

fn to_update_asset(asset: &GhAsset, kind: AssetKind) -> UpdateAsset {
    UpdateAsset {
        name: asset.name.clone(),
        url: gh_asset_url(asset),
        size: asset.size.unwrap_or(0),
        kind,
    }
}

/// Release 解析：只归集契约内资产；其它 Release 文件（增量包、旧清单等）忽略且永不回退。
fn resolve_assets(release: &GhRelease) -> Result<(String, Vec<UpdateAsset>)> {
    let version = normalize_version(&release.tag_name);
    parse_semver(&version)?;
    let setup_name = asset_name(ASSET_TEMPLATE, &version);

    let setups: Vec<_> = release
        .assets
        .iter()
        .filter(|asset| asset.name == setup_name)
        .collect();
    if setups.len() != 1 {
        return Err(anyhow!(
            "更新包不存在或重名：需要唯一完整安装包 {setup_name}，实际 {} 个",
            setups.len()
        ));
    }
    let sums: Vec<_> = release
        .assets
        .iter()
        .filter(|asset| asset.name == SUMS_NAME)
        .collect();
    if sums.len() != 1 {
        return Err(anyhow!(
            "更新清单不存在或重名：需要唯一 {SUMS_NAME}，实际 {} 个",
            sums.len()
        ));
    }

    let mut volumes: Vec<_> = release
        .assets
        .iter()
        .filter_map(|asset| {
            if asset.name.starts_with(DATA_PREFIX) {
                Some(volume_number(&asset.name).map(|number| (asset, number)))
            } else {
                None
            }
        })
        .collect::<Result<Vec<_>>>()?;
    if volumes.is_empty() {
        return Err(anyhow!("缺少数据分卷 {DATA_PREFIX}001"));
    }
    volumes.sort_by_key(|(asset, number)| (*number, asset.name.clone()));
    for (index, (_, number)) in volumes.iter().enumerate() {
        let expected = index as u32 + 1;
        if *number != expected {
            return Err(anyhow!(
                "数据分卷断号或重复：期望 .{expected:03}，实际 .{number:03}"
            ));
        }
    }

    let mut assets = vec![to_update_asset(setups[0], AssetKind::Setup)];
    assets.extend(
        volumes
            .into_iter()
            .map(|(asset, _)| to_update_asset(asset, AssetKind::Data)),
    );
    assets.push(to_update_asset(sums[0], AssetKind::Sums));
    validate_asset_set(&version, &assets)?;
    Ok((version, assets))
}

/// 安装入口与 Release 解析共用的封闭资产集校验。
fn validate_asset_set(version: &str, assets: &[UpdateAsset]) -> Result<()> {
    let setup_name = asset_name(ASSET_TEMPLATE, version);
    let mut setup_count = 0usize;
    let mut sums_count = 0usize;
    let mut volumes = BTreeMap::new();

    for asset in assets {
        if !is_https(&asset.url) {
            return Err(anyhow!("更新资产 URL 必须使用 HTTPS: {}", asset.name));
        }
        if asset.name == setup_name {
            setup_count += 1;
        } else if asset.name == SUMS_NAME {
            sums_count += 1;
        } else if asset.name.starts_with(DATA_PREFIX) {
            let number = volume_number(&asset.name)?;
            if volumes.insert(number, asset.name.as_str()).is_some() {
                return Err(anyhow!("数据分卷重复: {}", asset.name));
            }
        } else {
            return Err(anyhow!("更新资产含未知文件: {}", asset.name));
        }
    }

    if setup_count != 1 {
        return Err(anyhow!(
            "需要唯一完整安装包 {setup_name}，实际 {setup_count} 个"
        ));
    }
    if sums_count != 1 {
        return Err(anyhow!("需要唯一 {SUMS_NAME}，实际 {sums_count} 个"));
    }
    if volumes.is_empty() {
        return Err(anyhow!("缺少数据分卷 {DATA_PREFIX}001"));
    }
    for expected in 1..=volumes.len() as u32 {
        if !volumes.contains_key(&expected) {
            return Err(anyhow!("数据分卷断号：缺少 .{expected:03}"));
        }
    }
    Ok(())
}

fn validate_install_target(info: &UpdateInfo) -> Result<()> {
    parse_semver(&info.version)?;
    validate_asset_set(&info.version, &info.assets)?;
    let actual_total = info.assets.iter().map(|asset| asset.size).sum::<u64>();
    if actual_total != info.total_bytes {
        return Err(anyhow!(
            "更新资产总字节数不符：期望 {}，实际 {actual_total}",
            info.total_bytes
        ));
    }
    Ok(())
}

fn plan_release(release: &GhRelease, current: &str) -> Result<Option<UpdateInfo>> {
    let (version, assets) = resolve_assets(release)?;
    if parse_semver(&version)? <= parse_semver(current)? {
        return Ok(None);
    }
    let total_bytes = assets.iter().map(|asset| asset.size).sum();
    Ok(Some(UpdateInfo {
        version,
        current: current.to_string(),
        changelog: release.body.clone().unwrap_or_default(),
        assets,
        total_bytes,
    }))
}

fn build_client(timeout: Duration) -> Result<Client> {
    Client::builder()
        .timeout(timeout)
        .user_agent("Matting-Flash-Desktop")
        .redirect(reqwest::redirect::Policy::custom(|attempt| {
            if attempt.url().scheme() == "https" {
                attempt.follow()
            } else {
                attempt.stop()
            }
        }))
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
    let releases: Vec<GhRelease> = resp
        .error_for_status()
        .context("Releases 返回错误状态")?
        .json()
        .await
        .context("解析 Releases JSON 失败")?;
    releases
        .into_iter()
        .filter(|release| !release.draft && parse_semver(&release.tag_name).is_ok())
        .max_by(|a, b| {
            parse_semver(&a.tag_name)
                .unwrap()
                .cmp(&parse_semver(&b.tag_name).unwrap())
        })
        .ok_or_else(|| anyhow!("未找到已发布版本"))
}

async fn do_check() -> Result<Option<UpdateInfo>> {
    let client = build_client(HTTP_TIMEOUT)?;
    let release = fetch_latest(&client).await?;
    plan_release(&release, env!("CARGO_PKG_VERSION"))
}

#[tauri::command]
pub async fn check_update(app: AppHandle, auto: bool) -> Result<Option<UpdateInfo>, String> {
    let (busy, updating) = {
        let state = app.state::<Mutex<UpdateState>>();
        let guard = state.lock().map_err(|e| e.to_string())?;
        let busy = guard.busy.load(Ordering::SeqCst);
        let updating = guard.updating.load(Ordering::SeqCst);
        if !busy && !updating {
            guard.busy.store(true, Ordering::SeqCst);
        }
        (busy, updating)
    };
    if busy || updating {
        return Err("已有更新检查/安装在进行中".into());
    }
    let result = do_check().await;
    {
        let state = app.state::<Mutex<UpdateState>>();
        let mut guard = state.lock().map_err(|e| e.to_string())?;
        guard.busy.store(false, Ordering::SeqCst);
        if let Ok(Some(info)) = &result {
            guard.cached.checked = true;
            guard.cached.info = Some(info.clone());
        }
    }
    match result {
        Ok(info) => {
            if let Some(info) = &info {
                let _ = app.emit("update-available", info);
            }
            Ok(info)
        }
        Err(error) => {
            if auto {
                log::warn!("自动检查更新失败（静默）: {error:#}");
                Ok(None)
            } else {
                Err(format!("{error:#}"))
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
        .and_then(|guard| guard.cached.info.clone())
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
        UpdateProgress {
            phase,
            percent,
            message: message.into(),
        },
    );
}

fn sha256_of(path: &Path) -> Result<String> {
    let mut file = File::open(path).context("打开更新资产失败")?;
    let mut hasher = Sha256::new();
    let mut buf = vec![0u8; 64 * 1024];
    loop {
        let count = file.read(&mut buf).context("读取更新资产失败")?;
        if count == 0 {
            break;
        }
        hasher.update(&buf[..count]);
    }
    Ok(format!("{:x}", hasher.finalize()))
}

/// 流式写入 `<name>.part`，大小校验成功后原子改名为正式文件。
async fn persist_stream<S, T, F>(
    stream: S,
    dest: &Path,
    expected_size: u64,
    mut progress: F,
) -> Result<u64>
where
    S: Stream<Item = Result<T>>,
    T: AsRef<[u8]>,
    F: FnMut(u64),
{
    let mut part_name = dest.as_os_str().to_os_string();
    part_name.push(".part");
    let part = PathBuf::from(part_name);
    if part.exists() {
        fs::remove_file(&part).context("清理残留 .part 文件失败")?;
    }

    let result = async {
        let mut file = File::create(&part).context("创建 .part 文件失败")?;
        let mut stream = Box::pin(stream);
        let mut written = 0u64;
        while let Some(chunk) = stream.next().await {
            let chunk = chunk?;
            file.write_all(chunk.as_ref())
                .context("写入 .part 文件失败")?;
            written += chunk.as_ref().len() as u64;
            progress(written);
        }
        file.flush().context("刷新 .part 文件失败")?;
        file.sync_all().context("同步 .part 文件失败")?;
        drop(file);
        if expected_size > 0 && written != expected_size {
            return Err(anyhow!(
                "下载不完整：期望 {expected_size} 字节，实际 {written} 字节"
            ));
        }
        // Windows 的 rename 不覆盖既有文件；正式文件只在成功下载后替换。
        if dest.exists() {
            fs::remove_file(dest).context("替换旧更新资产失败")?;
        }
        fs::rename(&part, dest).context("原子改名更新资产失败")?;
        Ok(written)
    }
    .await;

    if result.is_err() {
        let _ = fs::remove_file(&part);
    }
    result
}

async fn download_asset(
    client: &Client,
    asset: &UpdateAsset,
    dest: &Path,
    app: &AppHandle,
) -> Result<u64> {
    emit_progress(app, "download", 0, format!("开始下载 {}…", asset.name));
    let response = client
        .get(&asset.url)
        .send()
        .await
        .with_context(|| format!("连接下载服务器失败: {}", asset.name))?
        .error_for_status()
        .with_context(|| format!("下载请求被拒绝: {}", asset.name))?;
    let stream = response
        .bytes_stream()
        .map(|chunk| chunk.context("下载流中断"));
    let name = asset.name.clone();
    let expected_size = asset.size;
    let progress_app = app.clone();
    persist_stream(stream, dest, expected_size, move |written| {
        let percent = if expected_size == 0 {
            0
        } else {
            written
                .checked_mul(90)
                .and_then(|value| value.checked_div(expected_size))
                .map(|value| value.min(90) as u8)
                .unwrap_or(90)
        };
        emit_progress(
            &progress_app,
            "download",
            percent,
            format!(
                "{} · {percent}% · {:.1} MiB",
                name,
                written as f64 / (1024.0 * 1024.0)
            ),
        );
    })
    .await
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
    let mut command = std::process::Command::new(exe);
    command
        .args(installer_args())
        .current_dir(exe.parent().unwrap_or_else(|| Path::new(".")))
        .creation_flags(DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP)
        .spawn()
        .with_context(|| format!("启动安装器失败: {}", exe.display()))?;
    Ok(())
}

#[cfg(not(windows))]
fn launch_installer(exe: &Path) -> Result<()> {
    Err(anyhow!("完整安装包更新仅支持 Windows: {}", exe.display()))
}

fn version_dir(root: &Path, version: &str) -> PathBuf {
    root.join(version)
}

fn prepare_version_dir(root: &Path, version: &str) -> Result<PathBuf> {
    let dir = version_dir(root, version);
    fs::create_dir_all(&dir).context("创建版本更新目录失败")?;
    // 此目录由更新器独占；清掉陈旧卷、旧 Setup、清单与 .part，避免 NSIS 读到旧分卷。
    for entry in fs::read_dir(&dir).context("读取版本更新目录失败")? {
        let entry = entry.context("读取版本更新目录项失败")?;
        let path = entry.path();
        if path.is_file() {
            fs::remove_file(&path).context("清理陈旧更新资产失败")?;
        }
    }
    Ok(dir)
}

fn parse_sums(text: &str) -> Result<BTreeMap<String, String>> {
    let mut sums = BTreeMap::new();
    for (index, raw_line) in text.lines().enumerate() {
        let line = raw_line.trim();
        if line.is_empty() || line.starts_with('#') {
            continue;
        }
        let mut parts = line.splitn(2, char::is_whitespace);
        let hash = parts.next().unwrap_or_default();
        let name = parts
            .next()
            .map(str::trim_start)
            .map(|rest| rest.strip_prefix('*').unwrap_or(rest))
            .unwrap_or_default();
        if hash.len() != 64 || !hash.bytes().all(|byte| byte.is_ascii_hexdigit()) {
            return Err(anyhow!("SHA256SUMS 第 {} 行哈希格式错误", index + 1));
        }
        if name.is_empty() || name.contains('/') || name.contains('\\') || name == SUMS_NAME {
            return Err(anyhow!("SHA256SUMS 第 {} 行文件名无效", index + 1));
        }
        if sums
            .insert(name.to_string(), hash.to_ascii_lowercase())
            .is_some()
        {
            return Err(anyhow!("SHA256SUMS 文件名重复: {name}"));
        }
    }
    if sums.is_empty() {
        return Err(anyhow!("SHA256SUMS 为空"));
    }
    Ok(sums)
}

fn payload_names(assets: &[UpdateAsset]) -> BTreeSet<&str> {
    assets
        .iter()
        .filter(|asset| asset.name != SUMS_NAME)
        .map(|asset| asset.name.as_str())
        .collect()
}

fn verify_manifest_coverage(sums: &BTreeMap<String, String>, assets: &[UpdateAsset]) -> Result<()> {
    let expected = payload_names(assets);
    for name in &expected {
        if !sums.contains_key(*name) {
            return Err(anyhow!("SHA256SUMS 缺少资产条目: {name}"));
        }
    }
    for name in sums.keys() {
        if !expected.contains(name.as_str()) {
            return Err(anyhow!("SHA256SUMS 含资产集外条目: {name}"));
        }
    }
    Ok(())
}

fn verify_asset_set(dir: &Path, assets: &[UpdateAsset]) -> Result<()> {
    let manifest = fs::read_to_string(dir.join(SUMS_NAME)).context("读取 SHA256SUMS 失败")?;
    let sums = parse_sums(&manifest)?;
    verify_manifest_coverage(&sums, assets)?;
    for asset in assets.iter().filter(|asset| asset.name != SUMS_NAME) {
        let path = dir.join(&asset.name);
        let metadata =
            fs::metadata(&path).with_context(|| format!("更新资产缺失: {}", asset.name))?;
        if asset.size > 0 && metadata.len() != asset.size {
            return Err(anyhow!(
                "更新资产大小不符: {}（期望 {}，实际 {}）",
                asset.name,
                asset.size,
                metadata.len()
            ));
        }
        let actual = sha256_of(&path).with_context(|| format!("校验失败: {}", asset.name))?;
        let expected = sums
            .get(&asset.name)
            .ok_or_else(|| anyhow!("SHA256SUMS 缺少资产条目: {}", asset.name))?;
        if &actual != expected {
            return Err(anyhow!("SHA256 校验失败: {}", asset.name));
        }
    }
    Ok(())
}

async fn do_update_steps(app: &AppHandle, info: &UpdateInfo) -> Result<()> {
    // 安装入口重校验：拒绝未知文件、缺卷/断号、缺清单、非 HTTPS 与路径穿越版本。
    validate_install_target(info)?;
    let client = build_client(Duration::from_secs(3600))?;
    let dir = prepare_version_dir(
        &crate::paths::local_app_dir().join("updates"),
        &info.version,
    )?;

    let sums_asset = info
        .assets
        .iter()
        .find(|asset| asset.name == SUMS_NAME)
        .context("缺少 SHA256SUMS 资产")?;
    download_asset(&client, sums_asset, &dir.join(SUMS_NAME), app).await?;
    let manifest = fs::read_to_string(dir.join(SUMS_NAME)).context("读取 SHA256SUMS 失败")?;
    let sums = parse_sums(&manifest)?;
    verify_manifest_coverage(&sums, &info.assets)?;

    for asset in info.assets.iter().filter(|asset| asset.name != SUMS_NAME) {
        download_asset(&client, asset, &dir.join(&asset.name), app).await?;
    }

    emit_progress(app, "verify", 0, "逐文件校验 SHA256SUMS…");
    verify_asset_set(&dir, &info.assets)?;
    emit_progress(app, "verify", 100, "全部更新资产校验通过");

    // 所有事件必须在 spawn / exit 前发完：安装器 KillRunning 会杀掉本进程。
    emit_progress(app, "install", 0, "正在启动安装程序…");
    let _ = app.emit(
        "update-installing",
        serde_json::json!({
            "version": info.version,
            "message": "应用即将关闭，安装程序将自动完成更新并重新启动。",
        }),
    );
    let setup_name = asset_name(ASSET_TEMPLATE, &info.version);
    launch_installer(&dir.join(setup_name))?;
    log::info!(
        "安装器已启动: {} -> {}（由安装器负责重启）",
        info.current,
        info.version
    );
    std::process::exit(0);
}

#[tauri::command]
pub fn start_update(app: AppHandle, state: State<'_, Mutex<UpdateState>>) -> Result<(), String> {
    let info = {
        let guard = state.lock().map_err(|e| e.to_string())?;
        if guard.updating.swap(true, Ordering::SeqCst) {
            return Err("更新正在进行中".into());
        }
        let Some(info) = guard.cached.info.clone() else {
            guard.updating.store(false, Ordering::SeqCst);
            return Err("请先检查更新".into());
        };
        info
    };
    let handle = app.clone();
    tauri::async_runtime::spawn(async move {
        match do_update_steps(&handle, &info).await {
            Ok(()) => {
                let _ = handle.emit("update-done", serde_json::json!({"version": info.version}));
            }
            Err(error) => {
                let _ = handle.emit(
                    "update-error",
                    serde_json::json!({"message": format!("{error:#}")}),
                );
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
    use futures::executor::block_on;
    use serde_json::json;
    use std::sync::atomic::{AtomicU64, Ordering as AtomicOrdering};

    static TEMP_COUNTER: AtomicU64 = AtomicU64::new(0);

    fn temp_dir(label: &str) -> PathBuf {
        let path = std::env::temp_dir().join(format!(
            "matting_updater_{label}_{}_{}",
            std::process::id(),
            TEMP_COUNTER.fetch_add(1, AtomicOrdering::SeqCst)
        ));
        fs::create_dir_all(&path).unwrap();
        path
    }

    fn asset_json(name: &str, size: u64) -> serde_json::Value {
        json!({
            "name": name,
            "url": format!("https://api.github.test/{name}"),
            "browser_download_url": format!("https://download.test/{name}"),
            "size": size,
        })
    }

    fn fixture(tag: &str) -> GhRelease {
        serde_json::from_value(json!({
            "tag_name": tag,
            "body": "fake release notes",
            "draft": false,
            "assets": [
                asset_json(&asset_name(ASSET_TEMPLATE, tag), 1024),
                asset_json(&format!("{DATA_PREFIX}001"), 2048),
                asset_json(&format!("{DATA_PREFIX}002"), 4096),
                asset_json(SUMS_NAME, 200),
                asset_json("app-v1.5.2.zip", 9),
                asset_json("Matting-Flash-Setup-v1.5.2.exe.sha256", 65),
            ]
        }))
        .unwrap()
    }

    fn update_info() -> UpdateInfo {
        plan_release(&fixture("v1.5.2"), "1.0.0").unwrap().unwrap()
    }

    fn write_fixture_files(dir: &Path, assets: &[UpdateAsset]) {
        for asset in assets {
            if asset.name == SUMS_NAME {
                continue;
            }
            fs::write(
                dir.join(&asset.name),
                vec![asset.name.as_bytes()[0]; asset.size as usize],
            )
            .unwrap();
        }
        let mut lines = Vec::new();
        for asset in assets {
            if asset.name != SUMS_NAME {
                lines.push(format!(
                    "{}  {}",
                    sha256_of(&dir.join(&asset.name)).unwrap(),
                    asset.name
                ));
            }
        }
        fs::write(dir.join(SUMS_NAME), lines.join("\n") + "\n").unwrap();
    }

    #[test]
    fn normalize_and_semver_compare() {
        assert_eq!(normalize_version(" v1.10.0 "), "1.10.0");
        assert!(parse_semver("v1.10.0").unwrap() > parse_semver("1.9.0").unwrap());
    }

    #[test]
    fn resolve_assets_accepts_complete_set_in_download_order() {
        let (version, assets) = resolve_assets(&fixture("v1.5.2")).unwrap();
        assert_eq!(version, "1.5.2");
        let names: Vec<_> = assets.iter().map(|asset| asset.name.as_str()).collect();
        assert_eq!(
            names,
            vec![
                "Matting-Flash-Setup-v1.5.2.exe",
                "Matting-Flash-Data.7z.001",
                "Matting-Flash-Data.7z.002",
                SUMS_NAME,
            ]
        );
        assert_eq!(assets.iter().map(|asset| asset.size).sum::<u64>(), 7368);
    }

    #[test]
    fn resolve_assets_rejects_missing_setup_without_fallback() {
        let mut release = fixture("v1.5.2");
        release
            .assets
            .retain(|asset| !asset.name.starts_with("Matting-Flash-Setup"));
        assert!(resolve_assets(&release)
            .unwrap_err()
            .to_string()
            .contains("唯一完整安装包"));
    }

    #[test]
    fn resolve_assets_rejects_missing_first_volume() {
        let mut release = fixture("v1.5.2");
        release
            .assets
            .retain(|asset| asset.name != format!("{DATA_PREFIX}001"));
        assert!(resolve_assets(&release)
            .unwrap_err()
            .to_string()
            .contains("断号或重复"));
    }

    #[test]
    fn resolve_assets_rejects_volume_gap() {
        let mut release = fixture("v1.5.2");
        release
            .assets
            .retain(|asset| asset.name != format!("{DATA_PREFIX}001"));
        release.assets.push(GhAsset {
            name: format!("{DATA_PREFIX}003"),
            url: "https://download.test/003".into(),
            browser_download_url: Some("https://download.test/003".into()),
            size: Some(1),
        });
        assert!(resolve_assets(&release).is_err());
    }

    #[test]
    fn resolve_assets_rejects_missing_sums() {
        let mut release = fixture("v1.5.2");
        release.assets.retain(|asset| asset.name != SUMS_NAME);
        assert!(resolve_assets(&release)
            .unwrap_err()
            .to_string()
            .contains("SHA256SUMS"));
    }

    #[test]
    fn resolve_assets_rejects_duplicate_setup_volume_and_sums() {
        let mut release = fixture("v1.5.2");
        release.assets.push(GhAsset {
            name: "Matting-Flash-Setup-v1.5.2.exe".into(),
            url: "https://download.test/other".into(),
            browser_download_url: None,
            size: Some(1),
        });
        assert!(resolve_assets(&release).is_err());

        let mut release = fixture("v1.5.2");
        release.assets.push(GhAsset {
            name: format!("{DATA_PREFIX}001"),
            url: "https://download.test/other".into(),
            browser_download_url: None,
            size: Some(1),
        });
        assert!(resolve_assets(&release).is_err());

        let mut release = fixture("v1.5.2");
        release.assets.push(GhAsset {
            name: SUMS_NAME.into(),
            url: "https://download.test/other".into(),
            browser_download_url: None,
            size: Some(1),
        });
        assert!(resolve_assets(&release).is_err());
    }

    #[test]
    fn resolve_assets_rejects_non_https_release_urls() {
        let mut release = fixture("v1.5.2");
        release.assets[0].browser_download_url = Some("http://download.test/setup.exe".into());
        assert!(resolve_assets(&release)
            .unwrap_err()
            .to_string()
            .contains("HTTPS"));
    }

    #[test]
    fn install_target_rejects_unknown_files_and_non_https_urls() {
        let mut info = update_info();
        info.assets.push(UpdateAsset {
            name: "notes.txt".into(),
            url: "https://download.test/notes.txt".into(),
            size: 1,
            kind: AssetKind::Data,
        });
        assert!(validate_install_target(&info)
            .unwrap_err()
            .to_string()
            .contains("未知文件"));

        for url in ["http://download.test/setup.exe", "file:///setup.exe"] {
            let mut info = update_info();
            info.assets[0].url = url.into();
            assert!(validate_install_target(&info).is_err());
        }
    }

    #[test]
    fn install_target_rejects_missing_volume_and_traversal() {
        let mut info = update_info();
        info.assets.remove(2);
        assert!(validate_install_target(&info).is_err());

        let mut info = update_info();
        info.version = "../escape".into();
        assert!(validate_install_target(&info).is_err());
    }

    #[test]
    fn version_dir_is_version_scoped() {
        let root = temp_dir("version_dir");
        assert_eq!(version_dir(&root, "1.5.2"), root.join("1.5.2"));
        fs::remove_dir_all(root).unwrap();
    }

    #[test]
    fn persist_stream_writes_part_then_atomic_rename() {
        let dir = temp_dir("persist");
        let dest = dir.join("Matting-Flash-Data.7z.001");
        let chunks = vec![Ok::<_, anyhow::Error>(b"abc".to_vec()), Ok(b"def".to_vec())];
        let written = block_on(persist_stream(
            futures::stream::iter(chunks),
            &dest,
            6,
            |_| {},
        ))
        .unwrap();
        assert_eq!(written, 6);
        assert_eq!(fs::read(&dest).unwrap(), b"abcdef");
        assert!(!dest
            .with_file_name("Matting-Flash-Data.7z.001.part")
            .exists());
        fs::remove_dir_all(dir).unwrap();
    }

    #[test]
    fn persist_stream_size_mismatch_cleans_part_and_preserves_old_file() {
        let dir = temp_dir("persist_bad_size");
        let dest = dir.join("Matting-Flash-Data.7z.001");
        fs::write(&dest, b"old").unwrap();
        let result = block_on(persist_stream(
            futures::stream::iter(vec![Ok::<_, anyhow::Error>(b"new".to_vec())]),
            &dest,
            4,
            |_| {},
        ));
        assert!(result.is_err());
        assert_eq!(fs::read(&dest).unwrap(), b"old");
        assert!(!dest
            .with_file_name("Matting-Flash-Data.7z.001.part")
            .exists());
        fs::remove_dir_all(dir).unwrap();
    }

    #[test]
    fn parse_sums_accepts_gnu_format_and_rejects_bad_entries() {
        let hash = "a".repeat(64);
        let parsed = parse_sums(&format!(
            "# comment\r\n{hash} *Matting-Flash-Data.7z.001\r\n{}  Matting-Flash-Setup-v1.5.2.exe\r\n",
            "B".repeat(64)
        ))
        .unwrap();
        assert_eq!(parsed.len(), 2);
        assert!(parse_sums("not-a-sum").is_err());
        assert!(parse_sums(&format!("{hash}  same\n{hash}  same")).is_err());
        assert!(parse_sums(&format!("{hash}  ../escape")).is_err());
    }

    #[test]
    fn verify_asset_set_checks_every_file_and_exact_coverage() {
        let dir = temp_dir("verify");
        let info = update_info();
        write_fixture_files(&dir, &info.assets);
        verify_asset_set(&dir, &info.assets).unwrap();

        fs::write(dir.join("Matting-Flash-Data.7z.002"), b"tampered").unwrap();
        assert!(verify_asset_set(&dir, &info.assets).is_err());
        fs::remove_dir_all(dir).unwrap();
    }

    #[test]
    fn verify_asset_set_rejects_missing_file_and_manifest_extra_entry() {
        let dir = temp_dir("verify_missing");
        let info = update_info();
        write_fixture_files(&dir, &info.assets);
        fs::remove_file(dir.join("Matting-Flash-Data.7z.001")).unwrap();
        assert!(verify_asset_set(&dir, &info.assets).is_err());
        fs::remove_dir_all(dir).unwrap();

        let dir = temp_dir("verify_extra");
        let info = update_info();
        write_fixture_files(&dir, &info.assets);
        let mut manifest = fs::read_to_string(dir.join(SUMS_NAME)).unwrap();
        manifest.push_str(&format!("{}  notes.txt\n", "c".repeat(64)));
        fs::write(dir.join(SUMS_NAME), manifest).unwrap();
        assert!(verify_asset_set(&dir, &info.assets).is_err());
        fs::remove_dir_all(dir).unwrap();
    }

    #[test]
    fn installer_args_request_silent_and_relaunch() {
        assert_eq!(installer_args(), vec!["/S", "/RELAUNCH"]);
    }

    #[test]
    fn sha256_known_vector() {
        let dir = temp_dir("sha256");
        let path = dir.join("abc.bin");
        fs::write(&path, b"abc").unwrap();
        assert_eq!(
            sha256_of(&path).unwrap(),
            "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
        );
        fs::remove_dir_all(dir).unwrap();
    }
}
