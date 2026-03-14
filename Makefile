# Agent HQ — Unified Build System
# Powered by ZeroClaw Engine

.PHONY: all build build-release test lint clean docker web vscode-ext chrome-ext

# ── Core Build ────────────────────────────────────────────────────

all: build web

build:
	cargo build

build-release:
	cargo build --release

build-fast:
	cargo build --profile release-fast

build-dist:
	cargo build --profile dist

# ── Testing ───────────────────────────────────────────────────────

test:
	cargo test

test-component:
	cargo test --test component

test-integration:
	cargo test --test integration

bench:
	cargo bench

# ── Linting ───────────────────────────────────────────────────────

lint:
	cargo clippy --all-targets -- -D warnings
	cargo fmt --check

fmt:
	cargo fmt

# ── Web Dashboard ─────────────────────────────────────────────────

web:
	cd web && npm install && npm run build

web-dev:
	cd web && npm install && npm run dev

# ── Extensions ────────────────────────────────────────────────────

vscode-ext:
	cd vscode-extension && npm install && npm run compile

chrome-ext:
	@echo "Chrome extension is ready to load from chrome-extension/"

# ── Docker ────────────────────────────────────────────────────────

docker:
	docker build -t agent-hq .

docker-compose:
	docker compose up -d

# ── Install ───────────────────────────────────────────────────────

install: build-release
	cargo install --path .

# ── Clean ─────────────────────────────────────────────────────────

clean:
	cargo clean
	rm -rf web/node_modules web/dist
	rm -rf vscode-extension/out vscode-extension/node_modules

# ── Migration (from Python codex-telegram-bot) ────────────────────

migrate-from-python:
	@echo "=== Agent HQ Migration Tool ==="
	@echo "Migrating data from ~/.config/codex-telegram-bot/ to ~/.zeroclaw/"
	@echo ""
	@echo "This will:"
	@echo "  1. Copy sessions from state.db to ZeroClaw memory"
	@echo "  2. Preserve SOUL.md and MEMORY_INDEX.md"
	@echo "  3. Convert .env settings to config.toml"
	@echo ""
	@if [ -f ~/.config/codex-telegram-bot/.env ]; then \
		echo "Found existing Python config at ~/.config/codex-telegram-bot/.env"; \
		echo "Run 'agent-hq onboard --reinit' to set up the new configuration."; \
	else \
		echo "No existing Python config found. Run 'agent-hq onboard' to get started."; \
	fi
