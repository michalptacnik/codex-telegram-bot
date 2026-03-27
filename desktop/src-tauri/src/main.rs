#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use std::sync::Mutex;

use serde::Serialize;
use tauri::{
    menu::{AboutMetadata, MenuBuilder, SubmenuBuilder},
    AppHandle, Emitter, Manager, State, Theme, TitleBarStyle,
};
use tauri_plugin_updater::{Builder as UpdaterPluginBuilder, Update, UpdaterExt};

#[derive(Clone, Serialize)]
struct DesktopShellInfo {
    name: &'static str,
    mode: &'static str,
    runtime_host: &'static str,
    platform: &'static str,
    appearance: &'static str,
    #[serde(rename = "menuDriven")]
    menu_driven: bool,
    #[serde(rename = "supportsTranslucency")]
    supports_translucency: bool,
    #[serde(rename = "windowStyle")]
    window_style: Option<&'static str>,
    #[serde(rename = "updateConfigured")]
    update_configured: bool,
}

const UPDATER_ENDPOINTS: Option<&str> = option_env!("AGENT_HQ_UPDATER_ENDPOINTS");
const UPDATER_PUBKEY: Option<&str> = option_env!("AGENT_HQ_UPDATER_PUBKEY");

fn updater_endpoints() -> Vec<String> {
    UPDATER_ENDPOINTS
        .unwrap_or_default()
        .split(',')
        .map(str::trim)
        .filter(|value| !value.is_empty())
        .map(ToOwned::to_owned)
        .collect()
}

fn updater_configured() -> bool {
    !updater_endpoints().is_empty()
        && UPDATER_PUBKEY
            .map(str::trim)
            .map(|value| !value.is_empty())
            .unwrap_or(false)
}

fn configure_updater(app: &AppHandle) -> tauri::Result<()> {
    if !updater_configured() {
        return Ok(());
    }

    let builder = UpdaterPluginBuilder::new().pubkey(UPDATER_PUBKEY.unwrap().trim());
    app.plugin(builder.build())?;
    Ok(())
}

struct PendingUpdate(Mutex<Option<Update>>);

#[derive(Clone, Serialize)]
#[serde(rename_all = "camelCase")]
struct DesktopUpdateMetadata {
    version: String,
    current_version: String,
    date: Option<String>,
    body: Option<String>,
}

#[tauri::command]
async fn desktop_check_for_updates(
    app: AppHandle,
    pending_update: State<'_, PendingUpdate>,
) -> Result<Option<DesktopUpdateMetadata>, String> {
    if !updater_configured() {
        return Ok(None);
    }

    let endpoints = updater_endpoints()
        .into_iter()
        .map(|endpoint| url::Url::parse(&endpoint).map_err(|error| error.to_string()))
        .collect::<Result<Vec<_>, _>>()?;

    let update = app
        .updater_builder()
        .pubkey(UPDATER_PUBKEY.unwrap().trim())
        .endpoints(endpoints)
        .map_err(|error| error.to_string())?
        .build()
        .map_err(|error| error.to_string())?
        .check()
        .await
        .map_err(|error| error.to_string())?;

    let metadata = update.as_ref().map(|update| DesktopUpdateMetadata {
        version: update.version.clone(),
        current_version: update.current_version.clone(),
        date: update.date.map(|date| date.to_string()),
        body: update.body.clone(),
    });

    *pending_update.0.lock().unwrap() = update;

    Ok(metadata)
}

#[tauri::command]
async fn desktop_install_update(
    pending_update: State<'_, PendingUpdate>,
) -> Result<(), String> {
    let update = pending_update
        .0
        .lock()
        .unwrap()
        .take()
        .ok_or_else(|| "No pending update is available.".to_string())?;

    update
        .download_and_install(
            |_chunk_length, _content_length| {},
            || {},
        )
        .await
        .map_err(|error| error.to_string())
}

#[tauri::command]
fn desktop_shell_info(app: AppHandle) -> DesktopShellInfo {
    let appearance = app
        .get_webview_window("main")
        .and_then(|window| window.theme().ok())
        .map(|theme| match theme {
            Theme::Light => "light",
            Theme::Dark => "dark",
            _ => "system",
        })
        .unwrap_or("system");

    DesktopShellInfo {
        name: "Agent HQ Desktop",
        mode: "local_control_center",
        runtime_host: "http://127.0.0.1:8765",
        platform: if cfg!(target_os = "macos") {
            "macos"
        } else if cfg!(target_os = "windows") {
            "windows"
        } else if cfg!(target_os = "linux") {
            "linux"
        } else {
            "web"
        },
        appearance,
        menu_driven: true,
        supports_translucency: cfg!(target_os = "macos"),
        window_style: if cfg!(target_os = "macos") {
            Some("transparent")
        } else {
            Some("native")
        },
        update_configured: updater_configured(),
    }
}

fn emit_menu_action(app: &AppHandle, action: &str) {
    let _ = app.emit("menu-action", action.to_string());
}

fn build_menu(app: &AppHandle) -> tauri::Result<tauri::menu::Menu<tauri::Wry>> {
    let app_menu = SubmenuBuilder::new(app, "Agent HQ")
        .about(Some(AboutMetadata {
            name: Some("Agent HQ".into()),
            version: Some(env!("CARGO_PKG_VERSION").into()),
            comments: Some("Local macOS control center for the Agent HQ runtime.".into()),
            ..Default::default()
        }))
        .separator()
        .text("app.preferences", "Settings…")
        .text("app.check_updates", "Check for Updates…")
        .text("app.toggle_language", "Toggle Language")
        .separator()
        .hide()
        .hide_others()
        .show_all()
        .separator()
        .quit()
        .build()?;

    let file_menu = SubmenuBuilder::new(app, "File")
        .text("file.new_session", "New Session")
        .text("file.new_mission", "New Mission")
        .build()?;

    let edit_menu = SubmenuBuilder::new(app, "Edit")
        .undo()
        .redo()
        .separator()
        .cut()
        .copy()
        .paste()
        .separator()
        .select_all()
        .build()?;

    let view_menu = SubmenuBuilder::new(app, "View")
        .text("view.toggle_sidebar", "Toggle Sidebar")
        .text("view.command_palette", "Command Palette…")
        .text("view.search", "Search")
        .separator()
        .text("navigate.studio", "Open Studio")
        .text("navigate.dashboard", "Open Dashboard")
        .text("navigate.agent", "Open Agent Chat")
        .text("navigate.missions", "Open Missions")
        .text("navigate.sessions", "Open Sessions")
        .text("navigate.tools", "Open Tools")
        .text("navigate.memory", "Open Memory")
        .text("navigate.logs", "Open Logs")
        .text("navigate.cost", "Open Cost")
        .separator()
        .text("view.refresh", "Refresh")
        .build()?;

    let window_menu = SubmenuBuilder::new(app, "Window")
        .minimize()
        .maximize()
        .separator()
        .close_window()
        .show_all()
        .build()?;

    let help_menu = SubmenuBuilder::new(app, "Help")
        .text("help.docs", "Documentation")
        .text("help.diagnostics", "Run Diagnostics")
        .text("help.support", "Support")
        .separator()
        .text("session.logout", "Log Out")
        .build()?;

    MenuBuilder::new(app)
        .item(&app_menu)
        .item(&file_menu)
        .item(&edit_menu)
        .item(&view_menu)
        .item(&window_menu)
        .item(&help_menu)
        .build()
}

fn main() {
    tauri::Builder::default()
        .setup(|app| {
            let menu = build_menu(&app.handle())?;
            app.set_menu(menu)?;
            configure_updater(&app.handle())?;
            app.manage(PendingUpdate(Mutex::new(None)));

            if let Some(window) = app.get_webview_window("main") {
                #[cfg(target_os = "macos")]
                {
                    let _ = window.set_title_bar_style(TitleBarStyle::Transparent);
                }
            }

            Ok(())
        })
        .on_menu_event(|app, event| emit_menu_action(app, event.id().0.as_ref()))
        .invoke_handler(tauri::generate_handler![
            desktop_shell_info,
            desktop_check_for_updates,
            desktop_install_update
        ])
        .run(tauri::generate_context!())
        .expect("failed to run Agent HQ Desktop");
}
