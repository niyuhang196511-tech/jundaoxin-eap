//! EAP Harness 桌面壳（M37/M38，设计 docs/18）：
//! - 系统托盘：显示/隐藏主窗口、退出
//! - 全局快捷键 Alt+Space：唤起快捷调用窗口
//! - 本地数据仓（M38）：会话/本地记忆/审计 SQLite，默认不出端（local_db.rs）
//! - 本地技能运行时（M39-A）：签名技能包安装/四域授权/沙箱执行（skills.rs）
//! - 自动更新（M43-C）：tauri-plugin-updater 接线，端内检查/下载安装/重启（updater.rs）
//! - 版本更新推送（M43-D）：发现新版本 → 系统通知（tauri-plugin-notification，Rust 侧直发）

#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

mod local_db;
mod skills;
mod updater;

use tauri::{
    menu::{Menu, MenuItem},
    tray::TrayIconBuilder,
    Manager,
};

fn show_main(app: &tauri::AppHandle) {
    if let Some(win) = app.get_webview_window("main") {
        let _ = win.show();
        let _ = win.unminimize();
        let _ = win.set_focus();
    }
}

fn main() {
    tauri::Builder::default()
        .plugin(tauri_plugin_global_shortcut::Builder::new()
            .with_handler(|app, _shortcut, event| {
                // Alt+Space：全局唤起快捷调用窗口（docs/18 M37 验收项）
                if event.state() == tauri_plugin_global_shortcut::ShortcutState::Pressed {
                    show_main(app);
                }
            })
            .build())
        // M43-C 自动更新：updater 插件（endpoint/pubkey 运行时注入见 updater.rs）
        .plugin(tauri_plugin_updater::Builder::new().build())
        // M43-D 版本更新推送：系统通知插件（notify_update command 直发，Rust 侧无需 capability）
        .plugin(tauri_plugin_notification::init())
        // M49-F 输入改选择：文件选择对话框（pythonPath「浏览…」，capability 见 default.json dialog:default）
        .plugin(tauri_plugin_dialog::init())
        .setup(|app| {
            // 本地数据仓（M38）：会话/本地记忆/审计 SQLite，默认不出端
            local_db::init(app)?;
            // 本地技能仓（M39-A）：harness-skills/ 目录 + skills.json 索引
            skills::init(app)?;

            // 托盘菜单：显示 / 退出
            let show = MenuItem::with_id(app, "show", "显示 Harness", true, None::<&str>)?;
            let quit = MenuItem::with_id(app, "quit", "退出", true, None::<&str>)?;
            let menu = Menu::with_items(app, &[&show, &quit])?;
            TrayIconBuilder::with_id("main-tray")
                .tooltip("EAP Harness")
                .menu(&menu)
                .on_menu_event(|app, event| match event.id.as_ref() {
                    "show" => show_main(app),
                    "quit" => app.exit(0),
                    _ => {}
                })
                .build(app)?;
            // 注册全局快捷键 Alt+Space
            use tauri_plugin_global_shortcut::GlobalShortcutExt;
            app.global_shortcut().register("Alt+Space")?;
            Ok(())
        })
        .invoke_handler(tauri::generate_handler![
            local_db::save_session,
            local_db::list_sessions,
            local_db::delete_session,
            local_db::save_memory,
            local_db::list_memory,
            local_db::clear_local,
            local_db::harness_audit_unreported,
            local_db::harness_audit_mark_reported,
            skills::skill_inspect,
            skills::skill_install,
            skills::skill_list,
            skills::skill_remove,
            skills::skill_run,
            updater::check_update,
            updater::install_update,
            updater::notify_update,
        ])
        .on_window_event(|window, event| {
            // 关闭按钮 → 隐藏到托盘（常驻）
            if let tauri::WindowEvent::CloseRequested { api, .. } = event {
                let _ = window.hide();
                api.prevent_close();
            }
        })
        .run(tauri::generate_context!())
        .expect("EAP Harness 启动失败");
}
