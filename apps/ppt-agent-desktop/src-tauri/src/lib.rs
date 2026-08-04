use std::{
    env, fs,
    fs::{File, OpenOptions},
    io::{self, Write},
    net::TcpListener,
    path::{Path, PathBuf},
    process::{Child, Command, Stdio},
    sync::Mutex,
    time::{SystemTime, UNIX_EPOCH},
};

use keyring::Entry;
use tauri::{Manager, Runtime};

const SERVICE: &str = "OfficeAgent.PPTAgent";
const CREDENTIAL_KEYS: [&str; 3] = ["OPENAI_API_KEY", "OPENAI_BASE_URL", "OPENAI_MODEL"];

struct SidecarState(Mutex<Option<Child>>);

#[derive(Clone, serde::Serialize)]
#[serde(rename_all = "camelCase")]
struct GatewayConfig {
    base_url: String,
    token: String,
}

#[tauri::command]
fn gateway_config(config: tauri::State<'_, GatewayConfig>) -> GatewayConfig {
    config.inner().clone()
}

fn safe_download_name(suggested_name: &str, fallback_name: &str) -> String {
    let requested = if suggested_name.trim().is_empty() {
        fallback_name
    } else {
        suggested_name
    };
    let basename = Path::new(requested)
        .file_name()
        .and_then(|value| value.to_str())
        .filter(|value| !value.trim().is_empty())
        .unwrap_or(fallback_name);
    let mut cleaned: String = basename
        .chars()
        .map(|character| {
            if character.is_control()
                || matches!(
                    character,
                    '<' | '>' | ':' | '"' | '/' | '\\' | '|' | '?' | '*'
                )
            {
                '_'
            } else {
                character
            }
        })
        .collect();
    cleaned = cleaned.trim_matches([' ', '.']).chars().take(120).collect();
    if cleaned.is_empty() {
        cleaned = fallback_name.to_string();
    }
    if !cleaned.to_ascii_lowercase().ends_with(".pptx") {
        cleaned.push_str(".pptx");
    }
    cleaned
}

fn unique_download_path(directory: &Path, file_name: &str) -> PathBuf {
    let initial = directory.join(file_name);
    if !initial.exists() {
        return initial;
    }
    let path = Path::new(file_name);
    let stem = path
        .file_stem()
        .and_then(|value| value.to_str())
        .unwrap_or("presentation");
    let extension = path
        .extension()
        .and_then(|value| value.to_str())
        .unwrap_or("pptx");
    for index in 1..10_000 {
        let candidate = directory.join(format!("{stem} ({index}).{extension}"));
        if !candidate.exists() {
            return candidate;
        }
    }
    directory.join(format!("{stem}-{}.{}", std::process::id(), extension))
}

#[tauri::command]
fn save_artifact_to_downloads(
    source_path: String,
    suggested_name: String,
) -> Result<String, String> {
    let source =
        fs::canonicalize(&source_path).map_err(|error| format!("PPTX 产物不存在：{error}"))?;
    if source
        .extension()
        .and_then(|value| value.to_str())
        .map(str::to_ascii_lowercase)
        .as_deref()
        != Some("pptx")
    {
        return Err("只允许保存 PPTX 产物".into());
    }

    let local_app_data = env::var_os("LOCALAPPDATA")
        .map(PathBuf::from)
        .ok_or("无法确定 Windows 本地应用数据目录")?;
    let artifact_root = fs::canonicalize(
        local_app_data
            .join("OfficeAgent")
            .join("PPTAgentV2")
            .join("artifacts"),
    )
    .map_err(|error| format!("无法读取 PPT Agent 产物目录：{error}"))?;
    if !source.starts_with(&artifact_root) {
        return Err("拒绝保存 PPT Agent 产物目录之外的文件".into());
    }

    let user_profile = env::var_os("USERPROFILE")
        .map(PathBuf::from)
        .ok_or("无法确定 Windows 用户目录")?;
    let downloads = user_profile.join("Downloads");
    fs::create_dir_all(&downloads).map_err(|error| format!("无法创建下载目录：{error}"))?;
    let fallback = source
        .file_name()
        .and_then(|value| value.to_str())
        .unwrap_or("presentation.pptx");
    let file_name = safe_download_name(&suggested_name, fallback);
    let destination = unique_download_path(&downloads, &file_name);
    fs::copy(&source, &destination).map_err(|error| format!("保存 PPTX 失败：{error}"))?;
    Ok(destination.to_string_lossy().into_owned())
}

#[tauri::command]
fn save_model_setting(name: String, value: String) -> Result<(), String> {
    if !CREDENTIAL_KEYS.contains(&name.as_str()) {
        return Err("unsupported setting key".into());
    }
    let entry = Entry::new(SERVICE, &name).map_err(|error| error.to_string())?;
    if value.trim().is_empty() {
        entry.delete_credential().map_err(|error| error.to_string())
    } else {
        entry
            .set_password(value.trim())
            .map_err(|error| error.to_string())
    }
}

#[tauri::command]
fn model_settings_status() -> Result<serde_json::Value, String> {
    let mut configured = serde_json::Map::new();
    for name in CREDENTIAL_KEYS {
        let present = Entry::new(SERVICE, name)
            .map_err(|error| error.to_string())?
            .get_password()
            .map(|value| !value.trim().is_empty())
            .unwrap_or(false);
        configured.insert(name.to_string(), present.into());
    }
    Ok(configured.into())
}

#[tauri::command]
fn restart_agent_sidecar<R: Runtime>(app: tauri::AppHandle<R>) -> Result<(), String> {
    let state = app.state::<SidecarState>();
    let mut process = state.0.lock().map_err(|_| "sidecar lock poisoned")?;
    if let Some(mut child) = process.take() {
        let _ = child.kill();
        let _ = child.wait();
        startup_trace("previous sidecar process stopped");
    }
    *process = Some(spawn_sidecar(&app)?);
    Ok(())
}

fn spawn_sidecar<R: Runtime>(app: &tauri::AppHandle<R>) -> Result<Child, String> {
    startup_trace("preparing sidecar process");
    let binary = sidecar_path(app)?;
    let gateway = app.state::<GatewayConfig>();
    let port = gateway
        .base_url
        .rsplit(':')
        .next()
        .ok_or("gateway port missing")?;
    let mut command = Command::new(&binary);
    command
        .env("PPT_AGENT_PORT", port)
        .env("PPT_AGENT_AUTH_TOKEN", &gateway.token)
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::null());
    let prefer_environment = env::var_os("PPT_AGENT_PREFER_ENV_MODEL_SETTINGS").is_some();
    if !prefer_environment {
        for name in CREDENTIAL_KEYS {
            if let Ok(entry) = Entry::new(SERVICE, name) {
                if let Ok(value) = entry.get_password() {
                    if !value.trim().is_empty() {
                        command.env(name, value);
                    }
                }
            }
        }
    }
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        command.creation_flags(0x08000000);
    }
    let child = command
        .spawn()
        .map_err(|error| format!("failed to start {}: {error}", binary.display()))?;
    startup_trace("sidecar process started");
    Ok(child)
}

fn reserve_loopback_port() -> Result<u16, String> {
    let listener = TcpListener::bind("127.0.0.1:0").map_err(|error| error.to_string())?;
    listener
        .local_addr()
        .map(|address| address.port())
        .map_err(|error| error.to_string())
}

fn new_gateway_token() -> Result<String, String> {
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        let output = Command::new("powershell.exe")
            .args([
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                "[guid]::NewGuid().ToString('N')",
            ])
            .creation_flags(0x08000000)
            .output()
            .map_err(|error| format!("failed to create gateway token: {error}"))?;
        let token = String::from_utf8_lossy(&output.stdout).trim().to_string();
        if output.status.success() && token.len() == 32 {
            return Ok(token);
        }
    }
    let nanos = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map_err(|error| error.to_string())?
        .as_nanos();
    Ok(format!("{:x}{:x}", nanos, std::process::id()))
}

fn sidecar_path<R: Runtime>(app: &tauri::AppHandle<R>) -> Result<PathBuf, String> {
    let resource_dir = app
        .path()
        .resource_dir()
        .map_err(|error| error.to_string())?;
    startup_trace(&format!("resource directory: {}", resource_dir.display()));
    let bundled = resource_dir
        .join("ppt-agent-v2-sidecar")
        .join("ppt-agent-v2-sidecar.exe");
    if bundled.is_file() {
        return Ok(bundled);
    }
    let archive = resource_dir.join("ppt-agent-v2-sidecar.zip");
    if archive.is_file() {
        startup_trace("bundled runtime archive found");
        return extract_sidecar_runtime(&archive);
    }
    let development = PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .join("../../../deploy/windows/dist/ppt-agent-v2-sidecar/ppt-agent-v2-sidecar.exe");
    if cfg!(debug_assertions) && development.is_file() {
        return Ok(development);
    }
    Err(format!(
        "PPT Agent sidecar was not found at {} or {}",
        development.display(),
        archive.display()
    ))
}

fn extract_sidecar_runtime(archive: &Path) -> Result<PathBuf, String> {
    let runtime_parent = runtime_parent_for_archive(archive)?;
    let runtime_dir = runtime_parent.join(env!("CARGO_PKG_VERSION"));
    let executable = runtime_dir
        .join("ppt-agent-v2-sidecar")
        .join("ppt-agent-v2-sidecar.exe");
    let ready_marker = runtime_dir.join(".ready");
    let archive_metadata = fs::metadata(archive)
        .map_err(|error| format!("failed to inspect bundled runtime: {error}"))?;
    let archive_modified = archive_metadata
        .modified()
        .ok()
        .and_then(|value| value.duration_since(UNIX_EPOCH).ok())
        .map(|value| value.as_nanos())
        .unwrap_or_default();
    let archive_signature = format!("{}:{archive_modified}", archive_metadata.len());
    let installed_signature = fs::read_to_string(&ready_marker).unwrap_or_default();
    if installed_signature == archive_signature && executable.is_file() {
        startup_trace("reusing extracted runtime");
        return Ok(executable);
    }

    fs::create_dir_all(&runtime_parent)
        .map_err(|error| format!("failed to create runtime directory: {error}"))?;
    if runtime_dir.exists() {
        fs::remove_dir_all(&runtime_dir)
            .map_err(|error| format!("failed to clear incomplete runtime: {error}"))?;
    }

    startup_trace(&format!("extracting runtime to {}", runtime_dir.display()));
    if let Err(error) = extract_zip_archive(archive, &runtime_dir) {
        let _ = fs::remove_dir_all(&runtime_dir);
        return Err(error);
    }

    if !executable.is_file() {
        let _ = fs::remove_dir_all(&runtime_dir);
        return Err("bundled runtime archive did not contain ppt-agent-v2-sidecar.exe".into());
    }
    fs::write(&ready_marker, archive_signature)
        .map_err(|error| format!("failed to mark runtime ready: {error}"))?;
    startup_trace("runtime extraction completed");
    Ok(executable)
}

fn runtime_parent_for_archive(archive: &Path) -> Result<PathBuf, String> {
    let application_dir = archive.parent().ok_or_else(|| {
        format!(
            "bundled runtime archive has no application directory: {}",
            archive.display()
        )
    })?;
    Ok(application_dir.join("runtime"))
}

fn extract_zip_archive(archive_path: &Path, destination: &Path) -> Result<(), String> {
    let archive_file = File::open(archive_path)
        .map_err(|error| format!("failed to open bundled runtime: {error}"))?;
    let mut archive = zip::ZipArchive::new(archive_file)
        .map_err(|error| format!("bundled runtime is not a valid ZIP archive: {error}"))?;
    for index in 0..archive.len() {
        let mut entry = archive
            .by_index(index)
            .map_err(|error| format!("failed to read bundled runtime entry {index}: {error}"))?;
        let relative_path = entry
            .enclosed_name()
            .ok_or_else(|| format!("bundled runtime contains an unsafe path: {}", entry.name()))?;
        let output_path = destination.join(relative_path);
        if entry.is_dir() {
            fs::create_dir_all(&output_path)
                .map_err(|error| format!("failed to create runtime directory: {error}"))?;
            continue;
        }
        if let Some(parent) = output_path.parent() {
            fs::create_dir_all(parent)
                .map_err(|error| format!("failed to create runtime directory: {error}"))?;
        }
        let mut output = File::create(&output_path)
            .map_err(|error| format!("failed to create runtime file: {error}"))?;
        io::copy(&mut entry, &mut output)
            .map_err(|error| format!("failed to extract runtime file: {error}"))?;
    }
    Ok(())
}

fn startup_log_path() -> PathBuf {
    if let Some(path) = env::var_os("PPT_AGENT_STARTUP_LOG") {
        return PathBuf::from(path);
    }
    let local_app_data = env::var_os("LOCALAPPDATA")
        .map(PathBuf::from)
        .unwrap_or_else(env::temp_dir);
    local_app_data
        .join("OfficeAgent")
        .join("PPTAgentV2")
        .join("logs")
        .join("desktop-startup.log")
}

fn startup_trace(message: &str) {
    let log_path = startup_log_path();
    if let Some(parent) = log_path.parent() {
        let _ = fs::create_dir_all(parent);
    }
    if let Ok(mut log) = OpenOptions::new().create(true).append(true).open(log_path) {
        let timestamp = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .map(|value| value.as_secs())
            .unwrap_or_default();
        let _ = writeln!(log, "[{timestamp}] {message}");
    }
}

fn write_startup_error(error: &str) {
    startup_trace(&format!("ERROR: {error}"));
}

pub fn run() {
    std::panic::set_hook(Box::new(|panic_info| {
        write_startup_error(&panic_info.to_string());
    }));
    startup_trace("desktop startup began");
    let port = reserve_loopback_port().expect("failed to reserve a loopback port");
    let gateway = GatewayConfig {
        base_url: format!("http://127.0.0.1:{port}"),
        token: new_gateway_token().expect("failed to create a loopback token"),
    };
    startup_trace("loopback gateway configuration ready");
    let app = tauri::Builder::default()
        .manage(SidecarState(Mutex::new(None)))
        .manage(gateway)
        .invoke_handler(tauri::generate_handler![
            gateway_config,
            save_artifact_to_downloads,
            save_model_setting,
            model_settings_status,
            restart_agent_sidecar
        ])
        .setup(|app| {
            let child = spawn_sidecar(app.handle())?;
            *app.state::<SidecarState>()
                .0
                .lock()
                .map_err(|_| "sidecar lock poisoned")? = Some(child);
            startup_trace("desktop setup completed");
            Ok(())
        })
        .build(tauri::generate_context!())
        .unwrap_or_else(|error| {
            write_startup_error(&error.to_string());
            panic!("error while building PPT Agent desktop application: {error}");
        });

    app.run(|handle, event| {
        if matches!(
            event,
            tauri::RunEvent::Exit | tauri::RunEvent::ExitRequested { .. }
        ) {
            if let Ok(mut process) = handle.state::<SidecarState>().0.lock() {
                if let Some(child) = process.as_mut() {
                    let _ = child.kill();
                    let _ = child.wait();
                }
            }
        }
    });
}

#[cfg(test)]
mod tests {
    use super::{runtime_parent_for_archive, safe_download_name, unique_download_path};
    use std::path::Path;

    #[test]
    fn sidecar_runtime_is_stored_next_to_the_bundled_archive() {
        let archive = Path::new(r"C:\Downloads\PPTAgentV2\ppt-agent-v2-sidecar.zip");

        let runtime_parent = runtime_parent_for_archive(archive).expect("runtime path");

        assert_eq!(
            runtime_parent,
            Path::new(r"C:\Downloads\PPTAgentV2\runtime")
        );
    }

    #[test]
    fn download_name_removes_windows_invalid_characters() {
        assert_eq!(
            safe_download_name(r"Quarter: Review?.pptx", "fallback.pptx"),
            "Quarter_ Review_.pptx"
        );
        assert_eq!(safe_download_name("", "fallback.pptx"), "fallback.pptx");
    }

    #[test]
    fn download_path_avoids_overwriting_existing_files() {
        let root = std::env::temp_dir().join(format!("ppt-agent-download-{}", std::process::id()));
        std::fs::create_dir_all(&root).expect("temp downloads");
        let original = root.join("deck.pptx");
        std::fs::write(&original, b"existing").expect("existing deck");

        let destination = unique_download_path(&root, "deck.pptx");

        assert_eq!(destination, root.join("deck (1).pptx"));
        let _ = std::fs::remove_dir_all(root);
    }
}
