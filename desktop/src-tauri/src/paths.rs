use std::ffi::OsString;
use std::path::PathBuf;

/// 用户级应用目录：`%LOCALAPPDATA%\Matting-Flash`。
///
/// 纯函数便于测试：传入 `None` 时回退临时目录（仅在异常环境兜底）。
pub fn local_app_dir_with(base: Option<OsString>) -> PathBuf {
    base.map(PathBuf::from)
        .unwrap_or_else(std::env::temp_dir)
        .join("Matting-Flash")
}

/// 当前用户的 `%LOCALAPPDATA%\Matting-Flash`。
pub fn local_app_dir() -> PathBuf {
    local_app_dir_with(std::env::var_os("LOCALAPPDATA"))
}

/// 环境变量优先，缺省回退指定目录（空字符串视为未设置）。
pub fn env_path(name: &str, fallback: PathBuf) -> PathBuf {
    match std::env::var_os(name) {
        Some(value) if !value.is_empty() => PathBuf::from(value),
        _ => fallback,
    }
}

/// 权重目录：安装态落 `%LOCALAPPDATA%\Matting-Flash\weights`，可用 `MATTING_WEIGHTS_DIR` 覆盖。
pub fn weights_dir() -> PathBuf {
    env_path("MATTING_WEIGHTS_DIR", local_app_dir().join("weights"))
}

/// 结果目录：`%LOCALAPPDATA%\Matting-Flash\results`，可用 `MATTING_RESULTS_DIR` 覆盖。
pub fn results_dir() -> PathBuf {
    env_path("MATTING_RESULTS_DIR", local_app_dir().join("results"))
}

/// 壳日志目录：`%LOCALAPPDATA%\Matting-Flash\logs`，可用 `MATTING_LOG_DIR` 覆盖。
pub fn log_dir() -> PathBuf {
    env_path("MATTING_LOG_DIR", local_app_dir().join("logs"))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn local_app_dir_appends_product_folder() {
        let p = local_app_dir_with(Some(OsString::from(r"C:\Users\demo\AppData\Local")));
        assert_eq!(p, PathBuf::from(r"C:\Users\demo\AppData\Local\Matting-Flash"));
    }

    #[test]
    fn env_path_falls_back_when_unset_or_empty() {
        let fallback = PathBuf::from(r"C:\fallback");
        assert_eq!(
            env_path("MATTING_TEST_PATH_SHOULD_BE_UNSET_XYZ", fallback.clone()),
            fallback
        );
    }

    #[test]
    fn default_dirs_all_live_under_local_app_dir() {
        let base = local_app_dir();
        assert_eq!(weights_dir(), env_path("MATTING_WEIGHTS_DIR", base.join("weights")));
        assert_eq!(results_dir(), env_path("MATTING_RESULTS_DIR", base.join("results")));
        assert_eq!(log_dir(), env_path("MATTING_LOG_DIR", base.join("logs")));
    }
}
