# E2E Integration: Proxmox + BCM + Kairos
# =========================================
# All configuration: inventory/group_vars/all.yml
#
# Quick start:
#   Edit inventory/group_vars/all.yml
#   make setup
#   make orchestrate
#
# Pass extra Ansible flags:
#   make orchestrate ANSIBLE_ARGS="-v"
#   make bcm-vm-create ANSIBLE_ARGS="--tags create"
#   make proxmox-up ANSIBLE_ARGS="-e proxmox_api_host=192.168.1.50"
#
# See: make help

SHELL := /bin/bash
.DEFAULT_GOAL := help

# Extra arguments forwarded to every ansible-playbook invocation
ANSIBLE_ARGS ?=

LOG_DIR := logs

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

define run-playbook
	@mkdir -p $(LOG_DIR)
	ansible-playbook playbooks/$(1) $(ANSIBLE_ARGS) 2>&1 | tee $(LOG_DIR)/$(basename $(1)).log
endef

# ---------------------------------------------------------------------------
# Prerequisites
# ---------------------------------------------------------------------------

.PHONY: setup
setup: ## Check prerequisites (tools + Ansible collections)
	@OK=true; \
	for cmd in ansible ansible-playbook sshpass curl docker qemu-system-x86_64 xorriso cpio gzip jq mcopy mkfs.vfat; do \
		if command -v $$cmd >/dev/null 2>&1; then printf "  [OK] %s\n" "$$cmd"; \
		else printf "  [MISSING] %s\n" "$$cmd"; OK=false; fi; \
	done; \
	for col in community.proxmox community.general ansible.posix; do \
		if ansible-galaxy collection list 2>/dev/null | grep -q "$$col"; then printf "  [OK] %s\n" "$$col"; \
		else printf "  [MISSING] %s\n" "$$col"; OK=false; fi; \
	done; \
	$$OK && echo "All prerequisites met." || { echo "Install missing tools."; exit 1; }

.PHONY: install-deps
install-deps: ## Install all build dependencies (packages, collections, submodules)
	$(call run-playbook,install-dependencies.yml)

.PHONY: install-collections
install-collections: ## Install required Ansible collections only
	ansible-galaxy collection install community.proxmox community.general ansible.posix

# ---------------------------------------------------------------------------
# Pipeline stages (in order)
# ---------------------------------------------------------------------------

.PHONY: orchestrate
orchestrate: ## Run full end-to-end pipeline (all stages)
	$(call run-playbook,site.yml)

.PHONY: proxmox-up
proxmox-up: ## Stage 0: Stand up Proxmox testbed (skipped if API reachable)
	$(call run-playbook,00-proxmox-up.yml)

.PHONY: create-vmbr1
create-vmbr1: ## Stage 1: Create internal bridge (vmbr1) in Proxmox
	$(call run-playbook,01-create-internal-network.yml)

.PHONY: bcm-prepare
bcm-prepare: ## Stage 2: Download + patch + remaster BCM ISO
	$(call run-playbook,02-bcm-prepare.yml)

.PHONY: bcm-vm-create
bcm-vm-create: ## Stage 3: Create BCM head node VM in Proxmox
	$(call run-playbook,03-bcm-vm.yml)

.PHONY: kairos-build
kairos-build: ## Stage 4: Build Kairos ISO + raw disk image via QEMU
	$(call run-playbook,04-kairos-build.yml)

.PHONY: deploy-dd
deploy-dd: ## Stage 5: Deploy Kairos raw image to BCM head node
	$(call run-playbook,05-deploy-dd.yml)

.PHONY: kairos-vm-create
kairos-vm-create: ## Stage 6: Create Kairos compute VM (PXE boot from BCM)
	$(call run-playbook,06-kairos-vm.yml)

.PHONY: validate
validate: ## Stage 7: Run validation checks on Kairos node
	$(call run-playbook,07-validate.yml)

# ---------------------------------------------------------------------------
# Local testing (QEMU on build host via VXLAN tunnel)
# ---------------------------------------------------------------------------

.PHONY: kairos-local
kairos-local: ## Launch local QEMU VM that PXE boots from BCM (via VXLAN tunnel)
	$(call run-playbook,kairos-local.yml)

.PHONY: kairos-local-kill
kairos-local-kill: ## Kill the local QEMU VM
	@if [ -f build/.kairos-local-qemu.pid ]; then \
		PID=$$(cat build/.kairos-local-qemu.pid); \
		kill $$PID 2>/dev/null && echo "Killed QEMU PID $$PID" || echo "QEMU not running"; \
		rm -f build/.kairos-local-qemu.pid; \
	else echo "No PID file found"; fi

.PHONY: kairos-local-serial
kairos-local-serial: ## Tail the local QEMU serial log
	@tail -f logs/kairos-local-serial.log

# ---------------------------------------------------------------------------
# Teardown & cleanup
# ---------------------------------------------------------------------------

.PHONY: teardown
teardown: ## Destroy all VMs, remove ISOs, tear down vmbr1
	$(call run-playbook,teardown.yml)

.PHONY: clean
clean: ## Remove local build artifacts (build/, dist/, logs/)
	rm -rf build/ dist/ logs/

.PHONY: clean-all
clean-all: teardown clean ## Teardown remote resources + remove local artifacts

# ---------------------------------------------------------------------------
# Help
# ---------------------------------------------------------------------------

.PHONY: help
help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*## ' $(MAKEFILE_LIST) | \
		awk 'BEGIN {FS = ":.*## "}; {printf "  \033[36m%-24s\033[0m %s\n", $$1, $$2}'
