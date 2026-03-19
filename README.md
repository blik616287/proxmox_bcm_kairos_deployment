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
| 5 | `make deploy-dd` | Upload raw disk image to BCM and deploy via dd |
| 6 | `make kairos-vm-create` | Create Kairos compute VM that PXE boots from BCM |
| 7 | `make validate` | Run validation checks against the Kairos node through BCM jump host |

## Quick Start — Full E2E Deployment

### 1. Clone the repo

```bash
git clone <repo-url> pbk
cd pbk
```

### 2. Install all dependencies

A single command installs system packages, Ansible collections, enables Docker, and initialises the CanvOS git submodule:

```bash
make install-deps
```

This runs the `install-dependencies.yml` playbook which handles Debian/Ubuntu and Fedora/RHEL automatically. It installs:

| Category | Packages |
|----------|----------|
| Ansible + SSH | `ansible`, `sshpass` |
| General tools | `curl`, `jq`, `git` |
| Docker | `docker.io` (Debian) / `docker` (Fedora) |
| QEMU / KVM | `qemu-system-x86`, `qemu-utils` |
| ISO remastering | `xorriso`, `cpio`, `gzip`, `p7zip-full` |
| FAT32 image tools | `mtools`, `dosfstools` |
| Disk image tools | `e2fsprogs`, `util-linux` |
| Netcat | `netcat-openbsd` (Debian) / `nmap-ncat` (Fedora) |
| Ansible collections | `community.proxmox`, `community.general`, `ansible.posix` |
| Git submodule | `CanvOS/` (spectrocloud/CanvOS) |

> After install, log out and back in if you were added to the `docker` group for the first time.

Or install manually if you prefer:

<details>
<summary>Manual install (Debian/Ubuntu)</summary>

```bash
sudo apt update
sudo apt install -y ansible sshpass curl jq git docker.io qemu-system-x86 \
    qemu-utils xorriso cpio gzip p7zip-full mtools dosfstools e2fsprogs \
    util-linux netcat-openbsd
sudo systemctl enable --now docker
sudo usermod -aG docker $USER
make install-collections
git submodule update --init --recursive
```
</details>

<details>
<summary>Manual install (Fedora/RHEL)</summary>

```bash
sudo dnf install -y ansible sshpass curl jq git docker qemu-system-x86 \
    qemu-img xorriso cpio gzip p7zip mtools dosfstools e2fsprogs \
    util-linux nmap-ncat
sudo systemctl enable --now docker
sudo usermod -aG docker $USER
make install-collections
git submodule update --init --recursive
```
</details>

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

Runs an 18-point check against the deployed Kairos node through the BCM jump host.

### 7. Tear down

```bash
make teardown      # Destroy VMs, remove ISOs, tear down vmbr1
make clean         # Remove local build/dist/logs directories
make clean-all     # Both of the above
```

## Prerequisites

### Tools
- `ansible`, `ansible-playbook`
- `sshpass`, `curl`, `jq`
- `docker`
- `qemu-system-x86_64`
- `xorriso`, `cpio`, `gzip`
- `mcopy`, `mkfs.vfat` (mtools / dosfstools)

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
│   └── teardown.yml                  # Destroy everything
├── roles/
│   ├── dependencies/                 # Install system packages + collections
│   ├── bcm_prepare/                  # ISO download, rootfs patch, remaster
│   ├── bcm_vm/                       # BCM VM create, install, IP discovery
│   ├── kairos_build/                 # CanvOS build, QEMU disk image
│   ├── deploy_dd/                    # Upload + dd deploy to BCM
│   ├── kairos_vm/                    # Kairos compute VM (PXE)
│   ├── proxmox_internal_network/     # vmbr1 bridge setup
│   └── validate/                     # Post-deploy validation
├── files/
│   ├── canvos/overlay/               # Custom files baked into Kairos image
│   └── installer-overlay/            # Kairos installer scripts
├── templates/                        # Jinja2 templates
├── CanvOS/                           # Git submodule (spectrocloud/CanvOS)
├── build/                            # Intermediate artifacts (gitignored)
├── dist/                             # Downloaded ISOs (gitignored)
└── logs/                             # Playbook run logs (gitignored)
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
| `cloud-config.yaml` | Stage 4 | Stage 4 (QEMU install) |

## Logs

Each `make` target logs output to `logs/<playbook-name>.log`. The full pipeline log is at `logs/site.log`.
