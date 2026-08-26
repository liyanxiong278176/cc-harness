#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use tauri::menu::{MenuBuilder, MenuItem, PredefinedMenuItem};
use tauri::tray::TrayIconBuilder;
use tauri::image::Image;
use tauri::{AppHandle, Emitter, Manager, WindowEvent};

const TRAY_ID: &str = "cc-harness-status";

fn status_icon(state: &str) -> Image<'static> {
    let color = match state {
        "active" => [69, 163, 255, 255],
        "approval" => [237, 184, 76, 255],
        "attention" => [240, 107, 118, 255],
        "completed" => [77, 213, 153, 255],
        _ => [120, 132, 150, 255],
    };
    let size = 16u32;
    let mut rgba = vec![0u8; (size * size * 4) as usize];
    for y in 0..size {
        for x in 0..size {
            let dx = x as i32 - 7;
            let dy = y as i32 - 7;
            if dx * dx + dy * dy <= 36 {
                let index = ((y * size + x) * 4) as usize;
                rgba[index..index + 4].copy_from_slice(&color);
            }
        }
    }
    Image::new_owned(rgba, size, size)
}

#[tauri::command]
fn exit_desktop(app: AppHandle) {
    app.exit(0);
}

#[tauri::command]
fn update_tray_status(
    app: AppHandle,
    state: String,
    active_count: usize,
    approval_count: usize,
) -> Result<(), String> {
    let tray = app
        .tray_by_id(TRAY_ID)
        .ok_or_else(|| "cc-harness tray is not ready".to_string())?;
    let tooltip = format!(
        "cc-harness · {} · 运行 {} · 待审批 {}",
        state, active_count, approval_count
    );
    tray.set_icon(Some(status_icon(&state)))
        .map_err(|error| error.to_string())?;
    tray.set_tooltip(Some(tooltip.as_str()))
        .map_err(|error| error.to_string())
}

pub fn run() {
    tauri::Builder::default()
        .plugin(tauri_plugin_process::init())
        .plugin(tauri_plugin_shell::init())
        .plugin(tauri_plugin_updater::Builder::new().build())
        .invoke_handler(tauri::generate_handler![exit_desktop, update_tray_status])
        .setup(|app| {
            let open = MenuItem::with_id(app, "open", "打开主界面", true, None::<&str>)?;
            let runs = MenuItem::with_id(app, "runs", "当前运行", true, None::<&str>)?;
            let approvals = MenuItem::with_id(app, "approvals", "待审批", true, None::<&str>)?;
            let update = MenuItem::with_id(app, "update", "检查更新", true, None::<&str>)?;
            let quit = MenuItem::with_id(app, "quit", "退出 cc-harness", true, None::<&str>)?;
            let separator = PredefinedMenuItem::separator(app)?;
            let menu = MenuBuilder::new(app)
                .items(&[&open, &runs, &approvals, &separator, &update, &quit])
                .build()?;

            TrayIconBuilder::with_id(TRAY_ID)
                .icon(status_icon("idle"))
                .menu(&menu)
                .show_menu_on_left_click(false)
                .on_menu_event(|app, event| match event.id.as_ref() {
                    "open" | "runs" | "approvals" => {
                        let _ = app.emit("tray://open", event.id.as_ref());
                        if let Some(window) = app.get_webview_window("main") {
                            let _ = window.show();
                            let _ = window.set_focus();
                        }
                    }
                    "update" => {
                        let _ = app.emit("tray://update", ());
                    }
                    "quit" => {
                        let _ = app.emit("tray://quit", ());
                    }
                    _ => {}
                })
                .build(app)?;
            Ok(())
        })
        .on_window_event(|window, event| {
            if let WindowEvent::CloseRequested { api, .. } = event {
                api.prevent_close();
                let _ = window.hide();
            }
        })
        .run(tauri::generate_context!())
        .expect("error while running cc-harness desktop");
}
