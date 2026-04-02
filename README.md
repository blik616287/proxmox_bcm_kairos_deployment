# Proxmox + BCM + Kairos — E2E Deployment

Automated end-to-end pipeline that stands up a Proxmox VE environment, deploys a Bright Cluster Manager (BCM) head node, builds and provisions Kairos compute nodes, and validates the result.

## Pipeline Stages

| Stage | Make Target | Description |
|-------|-------------|-------------|
| 0 | `make proxmox-up` | Stand up Proxmox VE testbed (skipped if API already reachable) |
| 1 | `make create-vmbr1` | Create internal bridge (`vmbr1`) — optional, only for dual-bridge setups |
| 2 | `make bcm-prepare` | Download BCM ISO from JFrog, patch rootfs for unattended install, remaster ISO |
| 3 | `make bcm-vm-create-api` | Create BCM head node VM via Proxmox API, install, power off, boot from disk |
| 4 | `make kairos-build` | Build Kairos ISO via CanvOS + generate raw disk image in local QEMU |
| 5 | `make deploy-dd` | Upload lz4-compressed raw image to BCM, configure installer + PXE |
| 6 | `make kairos-vm-create` | Create Kairos compute VM in Proxmox (PXE boot from BCM) |
| 7 | `make validate` | Run validation checks against the Kairos node through BCM jump host |

## Quick Start — Full E2E Deployment

### 1. Clone the repo

```bash
git clone <repo-url> pbk
cd pbk
git submodule update --init --recursive
```

### 2. Install all dependencies

```bash
make install-deps
```

This runs the `install-dependencies.yml` playbook which handles Debian/Ubuntu and Fedora/RHEL automatically. It installs:

| Category | Packages |
|----------|----------|
| Ansible + SSH | `ansible`, `sshpass` |
| General tools | `curl`, `jq`, `git`, `lz4`, `socat` |
| Docker | `docker.io` (skipped if Docker CE already installed) |
| QEMU / KVM | `qemu-system-x86`, `qemu-utils` |
| ISO remastering | `xorriso`, `cpio`, `gzip`, `7zip` |
| FAT32 image tools | `mtools`, `dosfstools` |
| Disk image tools | `e2fsprogs`, `util-linux` |
| Netcat | `netcat-openbsd` (Debian) / `nmap-ncat` (Fedora) |
| Ansible collections | `community.proxmox`, `community.general`, `ansible.posix` |
| Git submodule | `CanvOS/` (spectrocloud/CanvOS) |

> After install, log out and back in if you were added to the `docker` group for the first time.

### 3. Verify all prerequisites

```bash
make setup
```

Every tool and collection should show `[OK]`. Fix any `[MISSING]` items before proceeding.

### 4. Configure your environment

```bash
vi inventory/group_vars/all
```

At a minimum you need to set:
- **Proxmox** — `proxmox_api_host`, `proxmox_api_user`, `proxmox_api_password`
- **JFrog** — `jfrog_token`, `jfrog_instance`, `jfrog_repo`, `iso_filename`
- **Palette** — `palette_endpoint`, `palette_token`, `palette_project_uid`
- **BCM** — `bcm_password`, `bcm_hostname`, `bcm_internal_ip`, `bcm_external_ip`

If `proxmox_api_host` points to an existing Proxmox server, Stage 0 is automatically skipped. Otherwise the pipeline will stand up a local Proxmox testbed VM via libvirt.

### 5. Run the pipeline

```bash
make proxmox-up           # Stage 0: Proxmox testbed + dual-NIC VM
make bcm-prepare           # Stage 2: Remaster BCM ISO
make bcm-vm-create-api     # Stage 3: Install BCM (API-only, no SSH to Proxmox)
make kairos-build          # Stage 4: Build Kairos image
make deploy-dd             # Stage 5: Deploy to BCM
make kairos-vm-create      # Stage 6: PXE boot Kairos VM
make validate              # Stage 7: Validate
```

To run a single stage:
```bash
make bcm-prepare        # just Stage 2
make kairos-build       # just Stage 4
```

Monitor progress in real-time — output is displayed on screen and logged to `logs/`.

### 6. Validate

```bash
make validate
```

Runs validation checks against the deployed Kairos node through the BCM jump host.

### 7. Tear down

```bash
make teardown      # Destroy VMs, remove ISOs, tear down vmbr1
make clean         # Remove local build/dist/logs directories
make clean-all     # Both of the above
```

## Deployment Modes

### API-only (recommended)

Uses the Proxmox API for all VM operations — no SSH access to the Proxmox host required:

```bash
make bcm-vm-create-api     # Stage 3 via API
```

Stage 0 creates an API token (`root@pam!ansible`) and caches the credentials as persistent Ansible facts in `facts/`. All downstream stages load these facts automatically.

### SSH-based (legacy)

Uses SSH + `qm` commands on the Proxmox host:

```bash
make bcm-vm-create         # Stage 3 via SSH
```

### Single-bridge topology

When both BCM NICs are on the same bridge (e.g., `vmbr0` with VLAN tags), Stage 1 (`create-vmbr1`) is not needed. Set in `group_vars/all`:

```yaml
bcm_internal_bridge: vmbr0
bcm_external_bridge: vmbr0
```

## Persistent Facts

Stage 0 caches deployment configuration as Ansible facts in `facts/` (jsonfile cache). All downstream playbooks load these via `playbooks/load-deploy-facts.yml`. This includes:

- Proxmox API credentials and token
- BCM network configuration (IPs, bridges, gateways)
- BCM connect IP for SSH access

Facts are cached with `cacheable: true` and persist across playbook runs. To reset, delete `facts/` and re-run Stage 0.

## Kairos Deployment Flow (Stage 5-6 Detail)

The Kairos deployment uses BCM's PXE provisioning with a dd-based disk image installer:

```
1. Node PXE boots from BCM
   └── DHCP → TFTP (kernel + initrd) → NFS root from kairos-installer image

2. BCM FULL provisioning
   └── rsyncs kairos-installer image to local disk (~10 GB, ~10 min)

3. Node boots from disk → systemd starts
   └── kairos-install.service triggers install-kairos.sh

4. install-kairos.sh
   ├── Stages curl, lz4, dd, sync, sleep to /dev/shm (RAM)
   ├── Enables sysrq
   ├── Downloads lz4-compressed raw image from BCM HTTP server
   ├── Pipes: curl | lz4 -d | dd of=/dev/vda bs=4M oflag=direct
   └── sysrq poweroff (from RAM — disk is being overwritten)

5. Ansible detects VM stopped, switches boot order to disk-first, starts VM
   └── GRUB on disk boots Kairos
   └── cloud-config runs: hostname, SSH keys, Palette registration
```

### Image compression

Raw disk images are compressed with **lz4** for ~10x faster decompression. The 80 GB sparse image compresses to ~5.6 GB.

### Poweroff mechanism

After dd completes, the install script triggers a poweroff via `sysrq-trigger` (`echo o > /proc/sysrq-trigger`) from staged bash in `/dev/shm` (RAM). The sysrq must be enabled (`echo 1 > /proc/sys/kernel/sysrq`) before exec'ing to the staged bash, since the disk is being overwritten during dd. Ansible then detects the VM has stopped, switches the boot order to disk-first, and starts the VM.

### BCM install

The BCM autoinstall script runs `cm-master-install` without `--autoreboot` and calls `poweroff` when complete. Ansible detects the VM has stopped, switches boot order to `scsi0` (disk), and starts the VM from disk.

## Thin Pool Sizing

The Proxmox testbed uses LVM thin provisioning. The `proxmox_vm_disk_size` must be large enough to back:
- BCM VM disk (default 500G virtual, ~30G actual)
- Kairos VM disk (80G virtual, ~37G actual after dd with `oflag=direct`)
- Proxmox OS + overhead (~50G)

**Minimum recommended**: 700G (`proxmox_vm_disk_size: "700G"`). The qcow2 is thin on the host so only actual written data consumes host disk space.

## Local Testing with QEMU (via VXLAN Tunnel)

For development and debugging, you can launch a local QEMU VM on the build host that PXE boots from the BCM head node over a VXLAN tunnel.

```bash
make kairos-local          # Launch QEMU VM via VXLAN
make kairos-local-serial   # Tail serial log
make kairos-local-kill     # Kill local QEMU by PID
```

> **Important**: Never use `killall qemu-system-x86_64` — it will kill the Proxmox VM. Always kill local QEMU by specific PID.

## Configuration

All configuration is in `inventory/group_vars/all` (no `.yml` extension). Key sections:

| Section | Variables |
|---------|-----------|
| Proxmox API | `proxmox_api_host`, `proxmox_api_user`, `proxmox_api_password`, `proxmox_api_token_id`, `proxmox_api_token_secret` |
| BCM Network | `bcm_internal_bridge`, `bcm_internal_ip`, `bcm_external_bridge`, `bcm_external_ip`, `bcm_external_gateway` |
| BCM Head Node | `bcm_vmid`, `bcm_vm_cores`, `bcm_vm_memory`, `bcm_password` |
| Kairos Compute | `kairos_vmid`, `kairos_vm_cores`, `kairos_vm_memory`, `kairos_vm_disk_size` |
| JFrog (BCM ISO) | `jfrog_token`, `jfrog_instance`, `jfrog_repo`, `iso_filename` |
| Palette | `palette_endpoint`, `palette_token`, `palette_project_uid` |
| Testbed | `proxmox_vm_disk_size`, `proxmox_vm_ip`, `libvirt_net_gateway` |

### External Proxmox Server

To deploy on an existing Proxmox server (skipping Stage 0), set `proxmox_api_host` to the server's IP. The server needs:
- A bridge network (e.g., `vmbr0`) for VM connectivity
- Storage with `images` content type enabled (for VM disks)
- An API token with VM and storage permissions

## Networking

### Dual-bridge (default for testbed)

```
Proxmox VE
  vmbr0 (both internal + external)
    ├── BCM head node eth0 (internal, provisioning)
    ├── BCM head node eth1 (external, management)
    └── Kairos compute (PXE boot from BCM)
```

### Dual-bridge with VLANs (production)

```
Proxmox VE
  vmbr0 + VLAN tags
    ├── BCM eth0 (VLAN 3101, internal)
    ├── BCM eth1 (VLAN 3103, external)
    └── Kairos compute (VLAN 3101, PXE)
```

BCM uses `net.ifnames=0 biosdevname=0` for classic NIC naming (`eth0`, `eth1`).

## Passing Extra Ansible Arguments

Forward flags to `ansible-playbook` via `ANSIBLE_ARGS`:

```bash
make orchestrate ANSIBLE_ARGS="-v"                           # verbose
make bcm-vm-create-api ANSIBLE_ARGS="--tags create"          # run specific tags
make proxmox-up ANSIBLE_ARGS="-e proxmox_api_host=10.0.0.5"  # override a variable
```

## Make Targets

```bash
# Pipeline stages
make orchestrate          # Full end-to-end pipeline
make proxmox-up           # Stage 0: Proxmox testbed
make create-vmbr1         # Stage 1: Internal bridge (optional)
make bcm-prepare          # Stage 2: BCM ISO
make bcm-vm-create-api    # Stage 3: BCM VM (API)
make bcm-vm-create        # Stage 3: BCM VM (SSH, legacy)
make kairos-build         # Stage 4: Kairos image
make deploy-dd            # Stage 5: Deploy to BCM
make kairos-vm-create     # Stage 6: Kairos VM (Proxmox)
make validate             # Stage 7: Validation

# Local testing
make kairos-local         # Launch local QEMU via VXLAN
make kairos-local-kill    # Kill local QEMU
make kairos-local-serial  # Tail serial log

# Setup & teardown
make install-deps         # Install dependencies
make setup                # Verify prerequisites
make teardown             # Destroy VMs
make clean                # Remove build artifacts
make clean-all            # Teardown + clean
```

## Logs

Each `make` target logs output to `logs/<playbook-name>.log`. The full pipeline log is at `logs/site.log`. Serial console logs for local QEMU testing are at `logs/kairos-local-serial.log`.
