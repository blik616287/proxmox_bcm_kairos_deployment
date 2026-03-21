# Proxmox + BCM + Kairos — E2E Deployment

Automated end-to-end pipeline that stands up a Proxmox VE environment, deploys a Bright Cluster Manager (BCM) head node, builds and provisions Kairos compute nodes, and validates the result.

## Pipeline Stages

| Stage | Make Target | Description |
|-------|-------------|-------------|
| 0 | `make proxmox-up` | Stand up Proxmox VE testbed (skipped if API already reachable) |
| 1 | `make create-vmbr1` | Create internal bridge (`vmbr1`) in Proxmox for BCM↔Kairos traffic |
| 2 | `make bcm-prepare` | Download BCM ISO from JFrog, patch rootfs for unattended install, remaster ISO |
| 3 | `make bcm-vm-create` | Create BCM head node VM, run installer, switch to disk boot, discover IP |
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
| Docker | `docker.io` (Debian) / `docker` (Fedora) |
| QEMU / KVM | `qemu-system-x86`, `qemu-utils` |
| ISO remastering | `xorriso`, `cpio`, `gzip`, `p7zip-full` |
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
vi inventory/group_vars/all.yml
```

At a minimum you need to set:
- **Proxmox** — `proxmox_api_host`, `proxmox_api_user`, `proxmox_api_password`
- **JFrog** — `jfrog_token` (for BCM ISO download)
- **Palette** — `palette_endpoint`, `palette_token`, `palette_project_uid`

If `proxmox_api_host` points to an existing Proxmox server, Stage 0 is automatically skipped. Otherwise the pipeline will stand up a local Proxmox testbed VM via libvirt.

### 5. Run the full pipeline

```bash
make orchestrate
```

This executes all 8 stages in order (Proxmox → vmbr1 → BCM ISO → BCM VM → Kairos build → deploy → Kairos VM → validate). The full run can take 1-2 hours depending on download speeds and hardware.

To run a single stage instead:
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

## Local Testing with QEMU (via VXLAN Tunnel)

For development and debugging, you can launch a local QEMU VM on the build host that PXE boots from the BCM head node over a VXLAN tunnel. This gives direct serial console access without Proxmox VM management overhead.

### Setup

```bash
# Stages 0-5 must be complete (BCM head node running, deploy-dd done)
make kairos-local
```

This:
1. Creates a VXLAN tunnel between the build host and Proxmox's vmbr1
2. Launches a local QEMU VM with PXE boot over the tunnel
3. BCM provisions the node (FULL install → dd → reboot → Kairos)
4. Waits for Kairos to boot (up to 60 minutes)
5. Validates via SSH through BCM

### Serial console

```bash
make kairos-local-serial   # tail -f the serial log
```

Serial output is logged to `logs/kairos-local-serial.log`. The log captures kernel boot messages but not interactive login (the VM runs headless with `-vga none`).

### Managing the local VM

```bash
make kairos-local-kill     # Kill the local QEMU VM by PID
```

The QEMU monitor socket is at `/tmp/qemu-kairos-monitor.sock`:
```bash
echo "info status" | sudo socat - UNIX-CONNECT:/tmp/qemu-kairos-monitor.sock
echo "info blockstats" | sudo socat - UNIX-CONNECT:/tmp/qemu-kairos-monitor.sock  # disk write progress
```

### VXLAN tunnel details

The tunnel bridges the build host into Proxmox's internal network (vmbr1, 10.141.0.0/16) so the local QEMU VM can PXE boot from BCM:

```
Build Host                          Proxmox VE
├── br-bcm (bridge)                 ├── vmbr1 (10.141.0.0/16)
│   ├── vxlan100 ──── VXLAN ────────│── vxlan100
│   └── tap-kairos (QEMU)          │   ├── BCM eth0 (10.141.255.254)
│                                   │   └── Kairos VMs
└── virbr-pve (10.100.0.0/24) ─────└── enp1s0 → vmbr0
```

**MTU**: Transport interfaces (virbr-pve, enp1s0, vmbr0) are set to 9000 (jumbo frames) to accommodate VXLAN overhead. Inner interfaces (vxlan100, br-bcm, tap-kairos) use standard 1500 MTU.

> **Important**: Never use `killall qemu-system-x86_64` — it will kill the Proxmox VM. Always kill local QEMU by specific PID.

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
   ├── Pipes: curl | lz4 -d | dd of=/dev/vda bs=16M conv=fsync,sparse
   └── sysrq reboot (from RAM — disk is being overwritten)

5. Node reboots → GRUB on disk boots Kairos
   └── cloud-config runs: hostname from BCM, SSH keys, Palette registration,
       NFS mount for BCM cmd integration
```

### Image compression

Raw disk images are compressed with **lz4** (not gzip) for ~10x faster decompression. The 80 GB sparse image compresses to ~5.6 GB. With `conv=sparse`, dd skips zero blocks — only ~8.8 GB of actual data is written.

### Reboot mechanism

After dd completes, the install script triggers a reboot via `sysrq-trigger` from staged bash in `/dev/shm` (RAM). The sysrq must be enabled (`echo 1 > /proc/sys/kernel/sysrq`) before exec'ing to the staged bash, since the disk is being overwritten during dd.

## Prerequisites

### Tools
- `ansible`, `ansible-playbook`
- `sshpass`, `curl`, `jq`
- `docker`
- `qemu-system-x86_64`, `qemu-img`
- `xorriso`, `cpio`, `gzip`, `lz4`
- `mcopy`, `mkfs.vfat` (mtools / dosfstools)
- `socat` (for local QEMU testing)
- `bridge-utils` (for VXLAN tunnel)

### Ansible Collections
- `community.proxmox`
- `community.general`
- `ansible.posix`

Install all at once: `make install-collections`

## Configuration

All configuration is in `inventory/group_vars/all.yml`. Key sections:

| Section | Variables |
|---------|-----------|
| Proxmox API | `proxmox_api_host`, `proxmox_api_user`, `proxmox_api_password` |
| Internal Network | `bcm_internal_bridge` (vmbr1), `bcm_internal_cidr` (10.141.0.0/16) |
| BCM Head Node | `bcm_vmid`, `bcm_vm_cores`, `bcm_vm_memory`, `bcm_password` |
| Kairos Compute | `kairos_vmid`, `kairos_vm_cores`, `kairos_vm_memory` |
| JFrog (BCM ISO) | `jfrog_token`, `jfrog_instance`, `jfrog_repo`, `iso_filename` |
| Palette | `palette_endpoint`, `palette_token`, `palette_project_uid` |
| Paths | `build_dir`, `dist_dir` |

### External Proxmox Server

To deploy on an existing Proxmox server (skipping Stage 0), set `proxmox_api_host` to the server's IP. The server needs:
- A bridge network (e.g., `vmbr0`) for VM connectivity
- Storage with `images` content type enabled (for VM disks)
- An API token with VM and storage permissions

## Passing Extra Ansible Arguments

Forward flags to `ansible-playbook` via `ANSIBLE_ARGS`:

```bash
make orchestrate ANSIBLE_ARGS="-v"                           # verbose
make bcm-vm-create ANSIBLE_ARGS="--tags create"              # run specific tags
make proxmox-up ANSIBLE_ARGS="-e proxmox_api_host=10.0.0.5"  # override a variable
```

## Networking

```
┌─────────────────────────────────────────────┐
│                  Proxmox VE                 │
│                                             │
│  vmbr0 (external, DHCP)                     │
│    ├── BCM head node (eth1)                 │
│    └── Proxmox management                   │
│                                             │
│  vmbr1 (internal, 10.141.0.0/16)            │
│    ├── BCM head node (eth0, 10.141.255.254) │
│    └── Kairos compute (PXE boot)            │
└─────────────────────────────────────────────┘
```

- BCM head node has two NICs: `eth0` (internal/vmbr1) and `eth1` (external/vmbr0)
- Kernel parameter `net.ifnames=0 biosdevname=0` forces classic naming (`eth0`, `eth1`)
- Kairos compute nodes PXE boot from BCM over the internal bridge
- Set `bcm_provisioning_nic` to a physical NIC name to bridge vmbr1 to hardware for bare-metal PXE

## Project Structure

```
├── Makefile                          # Build/run targets
├── ansible.cfg                       # Ansible configuration
├── inventory/
│   ├── hosts.yml                     # Inventory (localhost + proxmox)
│   └── group_vars/all.yml            # All configuration variables
├── playbooks/
│   ├── site.yml                      # Full pipeline (imports all stages)
│   ├── install-dependencies.yml      # Install all build prerequisites
│   ├── 00-proxmox-up.yml             # Stage 0
│   ├── 01-create-internal-network.yml# Stage 1
│   ├── 02-bcm-prepare.yml            # Stage 2
│   ├── 03-bcm-vm.yml                 # Stage 3
│   ├── 04-kairos-build.yml           # Stage 4
│   ├── 05-deploy-dd.yml              # Stage 5
│   ├── 06-kairos-vm.yml              # Stage 6
│   ├── 07-validate.yml               # Stage 7
│   ├── kairos-local.yml              # Local QEMU testing via VXLAN
│   └── teardown.yml                  # Destroy everything
├── roles/
│   ├── dependencies/                 # Install system packages + collections
│   ├── bcm_prepare/                  # ISO download, rootfs patch, remaster
│   ├── bcm_vm/                       # BCM VM create, install, IP discovery
│   ├── kairos_build/                 # CanvOS build, QEMU disk image
│   ├── deploy_dd/                    # Upload + dd deploy to BCM
│   ├── kairos_vm/                    # Kairos compute VM (PXE)
│   ├── vxlan_tunnel/                 # VXLAN tunnel for local QEMU testing
│   ├── proxmox_internal_network/     # vmbr1 bridge setup
│   └── validate/                     # Post-deploy validation
├── proxmox/                          # Git submodule (proxmox-testbed)
├── files/
│   ├── canvos/overlay/               # Custom files baked into Kairos image
│   └── installer-overlay/            # Kairos installer scripts
├── templates/                        # Jinja2 templates
├── CanvOS/                           # Git submodule (spectrocloud/CanvOS)
├── build/                            # Intermediate artifacts (gitignored)
├── dist/                             # Downloaded ISOs (gitignored)
└── logs/                             # Playbook run logs (gitignored)
```

## Make Targets

```bash
# Pipeline stages
make orchestrate          # Full end-to-end pipeline
make proxmox-up           # Stage 0: Proxmox testbed
make create-vmbr1         # Stage 1: Internal bridge
make bcm-prepare          # Stage 2: BCM ISO
make bcm-vm-create        # Stage 3: BCM VM
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

## Build Artifacts

All generated in `build/` (gitignored):

| File | Produced By | Used By |
|------|-------------|---------|
| `bcm-autoinstall.iso` | Stage 2 | Stage 3 |
| `.bcm-kernel`, `.bcm-rootfs-auto.cgz` | Stage 2 | Stage 3 |
| `.bcm-init.img` | Stage 2 | Stage 3 |
| `.bcm-ip` | Stage 3 | Stages 5, 6, 7 |
| `palette-edge-installer.iso` | Stage 4 | Stage 4 (QEMU install) |
| `kairos-disk.raw` | Stage 4 | Stage 5 |
| `kairos-disk.raw.lz4` | Stage 5 | Stage 5 (upload to BCM) |
| `cloud-config.yaml` | Stage 4 | Stage 4 (QEMU install) |
| `bcm-kairos-key` | Stage 4 | Kairos boot (BCM integration) |
| `compute-node-disk.qcow2` | `kairos-local` | Local QEMU testing |

## Logs

Each `make` target logs output to `logs/<playbook-name>.log`. The full pipeline log is at `logs/site.log`. Serial console logs for local QEMU testing are at `logs/kairos-local-serial.log`.
