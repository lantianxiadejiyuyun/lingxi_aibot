pub mod api;
pub mod auth;
pub mod bilibili;
pub mod chat;
pub mod companion;
pub mod config;
pub mod db;
pub mod deployment;
pub mod error;
pub mod integrations;
pub mod jobs;
pub mod media;
pub mod memory;
pub mod migration;
pub mod network;
pub mod provider;
pub mod records;
pub mod search;
pub mod settings;
pub mod skills;
pub mod state;
pub mod tools;
pub fn version() -> &'static str {
    env!("CARGO_PKG_VERSION")
}
pub mod feishu;
pub mod feishu_runtime;
pub mod frontend;
pub mod growth;
pub mod qq;
