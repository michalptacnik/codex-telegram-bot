//! Integration tests for SOP audit logging via the SopAdvanceTool and SopApproveTool.
//!
//! These tests live here (not in src/tools/) because they cross the
//! `tools` + `memory` + `sop::audit` module boundaries. Running them as
//! integration tests guarantees all types resolve from the single `zeroclaw`
//! lib crate, avoiding bin/lib type-identity mismatches.

use std::sync::{Arc, Mutex};

use serde_json::json;
use tempfile::tempdir;
use zeroclaw::config::MemoryConfig;
use zeroclaw::memory::{create_memory, traits::MemoryCategory, Memory};
use zeroclaw::sop::audit::SopAuditLogger;
use zeroclaw::sop::engine::SopEngine;
use zeroclaw::sop::types::{
    Sop, SopEvent, SopExecutionMode, SopPriority, SopStep, SopTrigger, SopTriggerSource,
};
use zeroclaw::tools::{SopAdvanceTool, SopApproveTool, Tool};

// ── Helpers ─────────────────────────────────────────────────────

fn test_sop() -> Sop {
    Sop {
        name: "test-sop".into(),
        description: "Test SOP".into(),
        version: "1.0.0".into(),
        priority: SopPriority::Normal,
        execution_mode: SopExecutionMode::Auto,
        triggers: vec![SopTrigger::Manual],
        steps: vec![
            SopStep {
                number: 1,
                title: "Step one".into(),
                body: "Do step one".into(),
                suggested_tools: vec![],
                requires_confirmation: false,
            },
            SopStep {
                number: 2,
                title: "Step two".into(),
                body: "Do step two".into(),
                suggested_tools: vec![],
                requires_confirmation: false,
            },
        ],
        cooldown_secs: 0,
        max_concurrent: 1,
        location: None,
    }
}

fn engine_with_active_run() -> (Arc<Mutex<SopEngine>>, String) {
    let mut engine = SopEngine::with_sops_dir(None);
    engine.set_sops_for_test(vec![test_sop()]);
    let event = SopEvent {
        source: SopTriggerSource::Manual,
        topic: None,
        payload: None,
        timestamp: "2026-02-19T12:00:00Z".into(),
    };
    engine.start_run("test-sop", event).unwrap();
    let run_id = engine
        .active_runs()
        .keys()
        .next()
        .expect("expected active run")
        .clone();
    (Arc::new(Mutex::new(engine)), run_id)
}

fn engine_with_supervised_run() -> (Arc<Mutex<SopEngine>>, String) {
    let mut engine = SopEngine::with_sops_dir(None);
    let sop = Sop {
        execution_mode: SopExecutionMode::Supervised,
        ..test_sop()
    };
    engine.set_sops_for_test(vec![sop]);
    let event = SopEvent {
        source: SopTriggerSource::Manual,
        topic: None,
        payload: None,
        timestamp: "2026-02-19T12:00:00Z".into(),
    };
    engine.start_run("test-sop", event).unwrap();
    let run_id = engine
        .active_runs()
        .keys()
        .next()
        .expect("expected active run")
        .clone();
    (Arc::new(Mutex::new(engine)), run_id)
}

fn sqlite_memory() -> Arc<dyn Memory> {
    let tmp = tempdir().unwrap();
    // Keep the TempDir so the backing file lives for the duration of the test.
    let path = tmp.keep();
    let mem_cfg = MemoryConfig {
        backend: "sqlite".into(),
        ..MemoryConfig::default()
    };
    Arc::from(create_memory(&mem_cfg, &path, None).unwrap())
}

// ── advance tests ────────────────────────────────────────────────

#[tokio::test]
async fn advance_error_does_not_write_step_audit() {
    let engine = Arc::new(Mutex::new(SopEngine::with_sops_dir(None)));
    let memory = sqlite_memory();
    let audit = Arc::new(SopAuditLogger::new(memory.clone()));

    let tool = SopAdvanceTool::new(engine).with_audit(audit.clone());
    let result = tool
        .execute(json!({
            "run_id": "nonexistent",
            "status": "completed",
            "output": "done"
        }))
        .await;
    assert!(result.is_err());

    let runs = audit.list_runs().await.unwrap();
    assert!(
        runs.is_empty(),
        "no audit entries should exist after advance error"
    );
}

#[tokio::test]
async fn advance_success_writes_step_audit() {
    let (engine, run_id) = engine_with_active_run();
    let memory = sqlite_memory();
    let audit = Arc::new(SopAuditLogger::new(memory.clone()));

    let tool = SopAdvanceTool::new(engine).with_audit(audit.clone());
    let result = tool
        .execute(json!({
            "run_id": run_id,
            "status": "completed",
            "output": "Step 1 done"
        }))
        .await
        .unwrap();
    assert!(result.success);

    let entries = memory
        .list(Some(&MemoryCategory::Custom("sop".into())), None)
        .await
        .unwrap();
    let step_keys: Vec<_> = entries
        .iter()
        .filter(|e| e.key.starts_with("sop_step_"))
        .collect();
    assert!(
        !step_keys.is_empty(),
        "step audit should be written on success"
    );
}

// ── approve tests ────────────────────────────────────────────────

#[tokio::test]
async fn approve_writes_audit() {
    let (engine, run_id) = engine_with_supervised_run();
    let memory = sqlite_memory();
    let audit = Arc::new(SopAuditLogger::new(memory.clone()));

    let tool = SopApproveTool::new(engine).with_audit(audit.clone());
    let result = tool.execute(json!({"run_id": &run_id})).await.unwrap();
    assert!(result.success);

    let entries = memory
        .list(Some(&MemoryCategory::Custom("sop".into())), None)
        .await
        .unwrap();
    let approval_keys: Vec<_> = entries
        .iter()
        .filter(|e| e.key.starts_with("sop_approval_"))
        .collect();
    assert!(
        !approval_keys.is_empty(),
        "approval audit should be written on approve"
    );
}

#[tokio::test]
async fn approve_failure_does_not_write_audit() {
    let engine = Arc::new(Mutex::new(SopEngine::with_sops_dir(None)));
    let memory = sqlite_memory();
    let audit = Arc::new(SopAuditLogger::new(memory.clone()));

    let tool = SopApproveTool::new(engine).with_audit(audit.clone());
    let result = tool
        .execute(json!({"run_id": "nonexistent"}))
        .await
        .unwrap();
    assert!(!result.success);

    let stored = audit.get_run("nonexistent").await.unwrap();
    assert!(stored.is_none(), "failed approve should not write audit");
}
