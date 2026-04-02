# Proxmox + BCM + Kairos — Full Technical Architecture

## Pipeline Overview

This pipeline deploys a complete HPC stack inside a nested virtualization environment:

```
Physical Host (Ubuntu 24.04, KVM)
  └── libvirt VM: Proxmox VE 8.4 (10.100.0.2, virbr-pve bridge)
        ├── VM 200: BCM 11.0 Head Node (dual-NIC, both on vmbr0)
        │     ├── eth0: vmbr0 (10.100.0.200) — internal provisioning
        │     └── eth1: vmbr0 (10.100.0.201) — external access
        └── VM 201: Kairos Compute Node
              └── eth0: vmbr0 (10.100.0.10)  — PXE boot from BCM
```

The pipeline has 8 sequential stages, each implemented as an Ansible playbook backed by a role.

---

## Stage 0: Proxmox Testbed (`00-proxmox-up.yml`)

**Purpose**: Stand up a Proxmox VE hypervisor as a nested VM if one isn't already running.

**Prerequisite check**: The playbook first hits `https://<proxmox_api_host>:8006/api2/json`. If it gets HTTP 200 or 401, Proxmox is already running and all remaining tasks are skipped.

**When Proxmox is NOT running**:

### Role: `host_prepare` (from `proxmox-testbed` submodule)
1. Verifies KVM support (`kvm-ok`) and nested virtualization (`/sys/module/kvm_intel/parameters/nested`)
2. Installs system packages: `qemu-kvm`, `libvirt-daemon-system`, `virtinst`, `genisoimage`, `libguestfs-tools`, `xorriso`, `isolinux`
3. Installs Python packages: `proxmoxer`, `requests` (needed for Ansible's Proxmox modules)
4. Ensures `libvirtd` is running and current user is in `libvirt` + `kvm` groups

### Role: `libvirt_network`
1. Renders a libvirt network XML definition for `proxmox-testbed` network
2. Creates a NAT bridge (`virbr-pve`) on subnet `10.100.0.0/24`
3. Configures DHCP range `10.100.0.100–10.100.0.199` with a static lease for Proxmox at `10.100.0.2`
4. Sets network to autostart

### Role: `proxmox_vm`
1. Downloads the Proxmox VE 8.4 ISO (with SHA256 verification)
2. **ISO Remastering**:
   - Mounts the original ISO read-only
   - Copies contents to a writable directory
   - Renders `answer.toml` (auto-install config) with root password, FQDN, network settings
   - Creates `auto-installer-mode.toml` to enable unattended install
   - Patches `grub.cfg`: sets timeout=0, adds `proxmox-start-auto-installer` and serial console args
   - Extracts MBR from original ISO
   - Rebuilds ISO with `xorriso` preserving boot sectors
3. Creates a QCOW2 disk (`proxmox_vm_disk_size`, default 700 GB — thin-provisioned on host)
4. Defines and boots a libvirt VM with the remastered ISO
5. Monitors serial log for "Installation finished" / "SUCCESS"
6. Re-defines VM for disk boot (removes ISO), starts from disk
7. Waits for Proxmox API to become available (retries up to 60 times)

### Role: `proxmox_configure`
1. Waits for SSH on Proxmox VM
2. Disables enterprise repositories (`pve-enterprise.list`, `ceph.list`)
3. Adds no-subscription repository
4. Creates API token (`root@pam!ansible`)
5. Downloads Ubuntu Noble cloud image
6. Creates a cloud-init VM template (VMID 9000):
   - Creates VM → imports cloud image as scsi0 → adds cloud-init drive → sets boot order → adds serial console → converts to template

**Artifacts**: Proxmox VE running at `https://10.100.0.2:8006`

---

## Stage 1: Internal Network (`01-create-internal-network.yml`)

**Purpose**: Create a private bridge inside Proxmox for BCM ↔ Kairos communication.

### Role: `proxmox_internal_network`
1. Checks if `vmbr1` already exists on the Proxmox host
2. Appends bridge config to `/etc/network/interfaces`:
   ```
   auto vmbr1
   iface vmbr1 inet static
       address 10.100.0.200/16
       bridge-ports none       # virtual-only mode
       bridge-stp off
       bridge-fd 0
   ```
3. If `bcm_provisioning_nic` is set (e.g., `enp2s0`), uses `bridge-ports <nic>` instead of `none` — this allows physical servers on the same switch to PXE boot from BCM
4. Brings up vmbr1 with `ifup`
5. Verifies the bridge is active

**Network topology after this stage** (dual-bridge mode):
```
vmbr0 (external)  ← Proxmox management + internet
vmbr1 (internal)  ← provisioning, no physical uplink (VM-to-VM only)
```

> **Note**: Stage 1 is optional. In single-bridge mode (testbed), both BCM NICs use `vmbr0` on `10.100.0.0/24` and Stage 1 is skipped.

---

## Stage 2: BCM ISO Preparation (`02-bcm-prepare.yml`)

**Purpose**: Download the BCM ISO from JFrog and remaster it for fully unattended installation.

### Role: `bcm_prepare`

#### ISO Download
1. Creates `build/` and `dist/` directories
2. Downloads `bcm-11.0-ubuntu2404.iso` (~2 GB) from JFrog Artifactory using bearer token authentication
3. Skips download if ISO already exists in `dist/`

#### Kernel + Rootfs Extraction
4. Loop-mounts the ISO
5. Extracts the kernel (`vmlinuz-*`) to `build/.bcm-kernel` (~10 MB)
6. Extracts the root filesystem (`rootfs.cgz`) — this is a gzip-compressed cpio archive (~300 MB)
7. Unmounts ISO

#### Rootfs Patching
8. Decompresses rootfs.cgz via `lz4 -d` + `cpio -id` into a working directory
9. Patches `build-config.xml` inside the rootfs:
   - Sets hostname to `bcm_hostname` (e.g., `bcm11-headnode`)
   - Sets timezone to `bcm_timezone` (e.g., `America/Los_Angeles`)
10. Renders `bcm-autoinstall.sh` (Jinja2 template) into the rootfs — this is the main automation script that:
    - Waits for the installer environment to be ready
    - Mounts the installation ISO from `/dev/sr0`
    - Mounts the config drive from `/dev/vdb` (contains password)
    - Runs `cm-master-install` (BCM's 14-step installer)
    - Fixes GRUB after install
    - Triggers reboot
11. Copies `bcm-autoinstall.service` systemd unit into the rootfs
12. Enables the autoinstall service and disables interactive installers + getty:
    ```bash
    chroot rootfs systemctl enable bcm-autoinstall.service
    chroot rootfs systemctl mask bright-installer-text bright-installer-gui
    # Remove getty from serial console
    ```

#### Rootfs Repacking
13. Repacks the modified rootfs via `find . | cpio -o -H newc | gzip > .bcm-rootfs-auto.cgz`

#### Config Drive Creation
14. Creates a 4 MB FAT32 image (`build/.bcm-init.img`) using `mtools`
15. Writes the BCM password into the config drive (read by autoinstall script)

#### ISO Remastering
16. Extracts original ISO contents via `7z x` (loop mount has issues with modifying ISO contents)
17. Replaces the original rootfs.cgz with the patched version
18. Patches GRUB config in the extracted ISO:
    - Sets `timeout=0` (no menu delay)
    - Sets `default=1` (auto-select installer entry)
    - Adds serial console args: `console=tty0 console=ttyS0,115200`
19. Rebuilds ISO with `xorriso`:
    ```bash
    xorriso -as mkisofs \
      -o build/bcm-autoinstall.iso \
      -b boot/grub/i386-pc/eltorito.img \
      -no-emul-boot -boot-load-size 4 -boot-info-table \
      --grub2-boot-info --grub2-mbr /usr/lib/grub/i386-pc/boot_hybrid.img \
      -eltorito-alt-boot -e boot/grub/efi.img -no-emul-boot \
      -isohybrid-gpt-basdat \
      <extracted_iso_dir>
    ```

**Artifacts**:
| File | Size | Purpose |
|------|------|---------|
| `dist/bcm-11.0-ubuntu2404.iso` | ~2 GB | Original ISO (cached) |
| `build/bcm-autoinstall.iso` | ~2 GB | Remastered ISO |
| `build/.bcm-kernel` | ~10 MB | Extracted kernel for direct boot |
| `build/.bcm-rootfs-auto.cgz` | ~300 MB | Patched rootfs with autoinstall |
| `build/.bcm-init.img` | 4 MB | FAT32 config drive (password) |

---

## Stage 3: BCM VM Deployment

Two deployment modes:
- **API-only** (`03-bcm-vm-api.yml` / `make bcm-vm-create-api`): Uses Proxmox API — no SSH to Proxmox host required. Recommended.
- **SSH-based** (`03-bcm-vm.yml` / `make bcm-vm-create`): Uses SSH + `qm` commands. Legacy.

**Purpose**: Create the BCM head node VM, install BCM unattended, discover its IP, and wait for first-boot configuration.

### Role: `bcm_vm_api` (API path) / `bcm_vm` (SSH path)

#### Artifact Upload
1. Uploads to Proxmox via `ansible.builtin.copy`:
   - `build/.bcm-kernel` → `/var/lib/vz/images/bcm-kernel`
   - `build/.bcm-rootfs-auto.cgz` → `/var/lib/vz/images/bcm-rootfs-auto.cgz`
   - `dist/<iso>` → `/var/lib/vz/template/iso/<iso>`
   - `build/.bcm-init.img` → `/var/lib/vz/images/bcm-init.img`

#### VM Creation
2. Creates VM 200 with:
   ```
   --memory 8192 --cores 4 --bios seabios
   --net0 virtio=<mac>,bridge=vmbr0    # internal (eth0)
   --net1 e1000=<mac>,bridge=vmbr0    # external (eth1)
   --scsi0 local-lvm:100,format=raw    # 100 GB thin-provisioned LVM
   --scsihw virtio-scsi-pci
   --ide2 local:iso/<bcm.iso>,media=cdrom
   ```

#### Direct Kernel Boot (key technique)
3. Uses QEMU's `-kernel` and `-initrd` args to bypass the ISO bootloader entirely:
   ```
   --args '-kernel /var/lib/vz/images/bcm-kernel
           -initrd /var/lib/vz/images/bcm-rootfs-auto.cgz
           -append "ro dvdinstall systemd.unit=multi-user.target
                    net.ifnames=0 biosdevname=0
                    console=tty0 console=ttyS0,115200"
           -drive file=/var/lib/vz/images/bcm-init.img,format=raw,if=virtio
           -chardev file,id=serial0,path=/var/log/e2e/bcm-serial.log
           -serial chardev:serial0'
   ```
   - `dvdinstall`: Tells BCM installer to look for packages on CDROM
   - `systemd.unit=multi-user.target`: Boots to multi-user (no GUI)
   - `net.ifnames=0 biosdevname=0`: Uses classic `eth0/eth1` naming
   - The config drive is attached as virtio (`/dev/vdb` inside VM)
   - Serial output is logged to `/var/log/e2e/bcm-serial.log` on Proxmox

#### Installation Monitoring
4. Starts the VM — the kernel boots directly into the patched rootfs ramdisk
5. The ramdisk runs `bcm-autoinstall.service` which:
   - Mounts ISO from `/dev/sr0`
   - Reads password from config drive (`/dev/vdb`)
   - Runs `cm-master-install` (14-step BCM installer)
   - The installer writes ~28 GB to disk over ~75 minutes:
     - Steps 1-3: Parse config, partition disk
     - Step 4: Install Ubuntu Server 24.04 base
     - Step 5: Install head node distro packages
     - Step 6: Install BCM packages (~5 GB)
     - Step 7: Configure kernel + GRUB bootloader
     - Steps 8-9: Install software images for compute nodes
     - Step 10: Install BCM packages to software images
     - Step 11: Install Python packages
     - Step 12: Create node installer NFS image
     - Step 13: Finalize installation
     - Step 14: Initialize management daemon
   - Fixes GRUB, calls `poweroff` (not reboot)
6. **Disk write monitoring**: Instead of using a fixed timeout, monitors `qm status 200 --verbose | grep diskwrite`:
   - First waits for writes to exceed 10 MB (installer has started)
   - Then polls every 30 seconds, comparing write totals
   - When writes are identical for 3 consecutive checks (90 seconds of no change), declares install complete
   - After reboot, the VM re-enters the ramdisk (direct kernel boot is still active), writes stop, plateau is detected
   - Timeout: 4 hours (`async: 14400`)

#### Switch to Disk Boot
7. Stops the VM
8. Removes direct kernel boot args and CDROM:
   ```bash
   qm set 200 --ide2 none,media=cdrom --boot order=scsi0 --delete args
   ```
9. Starts VM from disk — GRUB in the installed OS takes over

#### IP Discovery
10. Adds temporary IP `10.100.0.1/24` on `vmbr1` so Proxmox host can reach BCM's internal IP
11. Waits for SSH on `10.100.0.200` (BCM's internal eth0)
12. Discovers external IP by SSH'ing to BCM and reading eth1's IP:
    ```bash
    ssh root@10.100.0.200 "ip -4 addr show eth1 | grep -oP 'inet \K[0-9.]+'"
    ```
13. Saves external IP to `build/.bcm-ip` (read by stages 5, 6, 7)

#### First-Boot Wait
14. Waits for `cmfirstboot` service to complete (up to 30 minutes):
    - `cmfirstboot` configures DHCP, DNS, NFS exports, provisions default software images, etc.
    - Polls `systemctl is-active cmfirstboot` every 15 seconds
    - When status is neither `active` nor `activating`, first boot is done

**Artifacts**: `build/.bcm-ip` containing the discovered external IP

---

## Stage 4: Kairos Build (`04-kairos-build.yml`)

**Purpose**: Build the Kairos OS ISO via CanvOS, then create a bootable raw disk image using QEMU.

### Role: `kairos_build`

#### CanvOS Setup
1. Checks for CanvOS git submodule, clones if missing
2. Generates `.arg` file from template with build parameters:
   ```
   CUSTOM_TAG=bcm-test
   IMAGE_REGISTRY=ttl.sh
   OS_DISTRIBUTION=ubuntu
   OS_VERSION=22.04
   K8S_DISTRIBUTION=k3s
   ISO_NAME=palette-edge-installer
   ARCH=amd64
   ```

#### CanvOS Patching
3. Copies overlay files from `files/canvos/overlay/` into CanvOS:
   - `/etc/network/interfaces.d/ifcfg-ens3` — static network config
   - `/etc/systemd/system/bcm-compat-fixes.service` — BCM compatibility service
   - `/etc/systemd/system/stylus-agent.service.d/bcm-sync.conf` — Palette agent config
   - `/usr/bin/bcm-compat-fixes.sh` — fixes for BCM compatibility
   - `/usr/bin/bcm-sync-userdata.sh` — syncs user data from BCM
4. Patches `Earthfile`:
   - Adds packages: `wget ifupdown nfs-common`
   - Skips dracut `nfit` module (not needed in VM)
5. Patches `Dockerfile`:
   - Adds `COPY` commands for BCM compatibility scripts

#### ISO Build
6. Runs `./earthly.sh +iso --ARCH=amd64` (containerized build, ~30-60 min):
   - Earthly pulls the build container from `us-docker.pkg.dev/palette-images/third-party/edge/earthly/earthly:v0.8.15`
   - Builds a Kairos-based Ubuntu 22.04 image with K3s
   - Produces `palette-edge-installer.iso`

#### SSH Key Generation
7. Generates ED25519 SSH keypair (`build/bcm-kairos-key`) for BCM ↔ Kairos communication

#### Cloud-Config
8. Renders `cloud-config.yaml` from Jinja2 template with:
   - User accounts + SSH keys
   - Palette endpoint, token, project UID
   - BCM SSH public key (for headless management)
   - Boot scripts for first-run configuration

#### Raw Disk Image Creation
9. Creates FAT32 user-data image (4 MB) with cloud-config
10. Creates blank 80 GB sparse raw disk: `truncate -s 81920M kairos-disk.raw`
11. **QEMU Install** — runs `kairos-agent install` inside a headless VM:
    ```bash
    qemu-system-x86_64 \
      -enable-kvm -m 4096 -smp 2 -cpu host \
      -display none \
      -chardev socket,id=ser0,path=<sock>,server=on,wait=off \
      -serial chardev:ser0 \
      -drive if=virtio,format=raw,media=disk,file=kairos-disk.raw \
      -drive if=virtio,format=raw,readonly=on,file=userdata.img \
      -drive format=raw,media=cdrom,readonly=on,file=palette-edge-installer.iso \
      -boot d \
      -daemonize
    ```
    - Boots from ISO (CDROM) into Kairos live environment
    - After 90 seconds (boot time), sends commands via serial socket using `nc`:
      ```bash
      mount /dev/vdb /mnt && cp /mnt/user-data /oem/90_custom.yaml && cp /mnt/user-data /tmp/99_bcm.yaml
      kairos-agent --debug install 2>&1; mount /dev/vda2 /oem; cp /tmp/99_bcm.yaml /oem/99_bcm.yaml; poweroff
      ```
    - `kairos-agent install` writes the OS to `/dev/vda` (the raw disk):
      - Creates partition table (MBR)
      - Partition 1: BIOS boot (1 MB, for GRUB)
      - Partition 2: OEM (64 MB, ext4, cloud-config storage)
      - Partition 3: COS_STATE (~15 GB, ext4, squashfs OS images)
      - Partition 4: COS_RECOVERY (~8 GB, ext4, recovery image)
      - Partition 5: COS_PERSISTENT (remaining, ext4, persistent data)
      - Installs GRUB to MBR
      - Copies cloud-config to OEM partition
    - VM powers off when done
    - Ansible waits for QEMU process to exit

#### Post-Processing
12. **ext4 metadata_csum fix**: Loop-mounts each partition and runs:
    ```bash
    e2fsck -fy <partition>
    tune2fs -O ^metadata_csum <partition>
    ```
    This removes the `metadata_csum` feature flag so GRUB (which may not support it) can read the filesystems. Ensures compatibility across BIOS and older GRUB versions.

13. **Sparse trim**: `fallocate --dig-holes kairos-disk.raw`
    - Punches holes in the file for any zero-filled regions
    - Reduces actual disk usage from 80 GB virtual to ~8.8 GB on disk

14. **Checksum**: `sha256sum kairos-disk.raw > kairos-disk.raw.sha256`

**Artifacts**:
| File | Virtual Size | Actual Size | Purpose |
|------|-------------|-------------|---------|
| `build/palette-edge-installer.iso` | ~1 GB | ~1 GB | Kairos installer ISO |
| `build/kairos-disk.raw` | 80 GB | 8.8 GB | Bootable disk image (sparse) |
| `build/kairos-disk.raw.lz4` | — | 1.6 GB | Compressed for transfer |
| `build/cloud-config.yaml` | — | ~10 KB | Kairos first-boot config |
| `build/bcm-kairos-key` | — | ~400 B | ED25519 SSH keypair |

---

## Stage 5: Deploy to BCM (`05-deploy-dd.yml`)

**Purpose**: Upload the raw disk image to BCM and configure BCM's provisioning system to deploy Kairos to compute nodes via PXE boot + dd.

### Role: `deploy_dd`

1. Reads BCM IP from `build/.bcm-ip`
2. Renders two scripts from Jinja2 templates:
   - `deploy-dd.sh` — orchestrator that runs on localhost
   - `install-kairos.sh` — installer that runs on the compute node (via NFS)
3. Executes `deploy-dd.sh` which performs 7 steps on BCM:

#### Step 1: Upload Image
- Compresses raw image: `gzip -c kairos-disk.raw > kairos-disk.raw.lz4` (80 GB → 1.6 GB)
- Uploads via scp to BCM: `scp kairos-disk.raw.lz4 root@<bcm>:/cm/shared/kairos/disk.raw.lz4`

#### Step 2: HTTP Server
- Creates a systemd service on BCM running `python3 -m http.server 8888` in `/cm/shared/kairos/`
- The compute node will download the image from `http://10.100.0.200:8888/disk.raw.lz4`
- Verifies server is responding with `curl --head`

#### Step 3: Software Image
- Clones BCM's `default-image` to create `kairos-installer` software image:
  ```bash
  cmsh -c "softwareimage; clone default-image kairos-installer; commit"
  ```
- This image lives at `/cm/images/kairos-installer/` and is served to nodes via NFS during PXE boot

#### Step 4: Install Script + Service
- Copies `install-kairos.sh` to `/cm/images/kairos-installer/usr/local/sbin/`
- Creates `kairos-install.service` systemd unit in the image:
  ```ini
  [Service]
  Type=oneshot
  ExecStartPre=/bin/bash -c 'sleep 10'
  ExecStart=/usr/local/sbin/install-kairos.sh
  TimeoutStartSec=1800
  ```
- Enables the service so it auto-runs when a node boots this image

#### Step 5: PXE Template
- Patches PXE config: changes `IPAPPEND 3` to `IPAPPEND 2` (only pass IP, not interface info)

#### Step 6: BCM Category Configuration
- Detects kernel version in the installer image
- Creates `kairos` category (cloned from `default`):
  ```bash
  cmsh -c "category; clone default kairos; commit"
  cmsh -c "category; use kairos; set softwareimage kairos-installer; commit"
  cmsh -c "category; use kairos; set installmode FULL; commit"
  cmsh -c "category; use kairos; set kernelparameters 'console=tty0 console=ttyS0,115200n8'; commit"
  cmsh -c "category; use kairos; set installbootrecord yes; commit"
  ```
- Disables BCM health monitoring (interfaces, mounts, NTP) for Kairos nodes — they'll be managed by Palette, not BCM
- Sets default category for new nodes to `kairos`
- Registers the compute node MAC address:
  ```bash
  cmsh -c "device; use node001; set mac <mac>; set category kairos; commit"
  ```

#### Step 7: Ramdisk Regeneration
- Verifies `install-kairos.sh` and `kairos-install.service` exist in the image
- Regenerates the BCM ramdisk to include the installer files:
  ```bash
  cmsh -c "softwareimage; use kairos-installer; createramdisk -w"
  ```
  This is critical — the ramdisk is what gets served during PXE boot (initrd)

### The `install-kairos.sh` Script (runs on compute node)

This script runs inside the NFS-booted `kairos-installer` environment on the compute node:

1. **Discovers target disk**: `lsblk -ndo NAME,TYPE | awk '$2=="disk"'` → `/dev/sda`
2. **Waits for HTTP server**: Polls `http://10.100.0.200:8888/disk.raw.lz4` until reachable
3. **Stages binaries to RAM** (`/dev/shm/kinstall/`):
   - Copies `bash`, `curl`, `lz4`, `dd`, `sync`, `sleep`, `sgdisk` and all their shared libraries
   - This is critical because `dd` is about to overwrite the disk that the OS is running from (NFS root → disk write → disk now has Kairos, old NFS filesystem is irrelevant)
4. **Creates `run-dd.sh`** in RAM:
   ```bash
   curl --fail -s http://{bcm_ip}:8888/disk.raw.lz4 | lz4 -d - - | dd of=/dev/vda bs=4M oflag=direct
   sync
   echo o > /proc/sysrq-trigger  # emergency poweroff
   ```
5. **Executes from RAM**: `exec /dev/shm/kinstall/bash /dev/shm/kinstall/run-dd.sh`
   - The `exec` replaces the current process, so the script itself is no longer needed
   - All binaries run from RAM, filesystem on disk is being overwritten
   - Uses `sysrq-trigger` for poweroff because normal `poweroff` command would try to use the (now overwritten) disk

---

## Stage 6: Kairos VM Creation (`06-kairos-vm.yml`)

**Purpose**: Create the Kairos compute node VM and PXE boot it from BCM.

### Role: `kairos_vm`

1. Creates VM 201 on Proxmox:
   ```
   --memory 4096 --cores 2 --bios seabios
   --net0 virtio=<mac>,bridge=vmbr1   # internal, PXE boot from BCM
   --scsi0 local-lvm:80,format=raw    # 80 GB disk
   --scsihw virtio-scsi-pci
   --boot order=scsi0;net0            # disk first, PXE fallback
   ```
   - Boot order is disk first, PXE second
   - On first boot, disk is empty → falls through to PXE → BCM provisions it
   - On subsequent boots, disk has Kairos → boots directly

2. Starts the VM

3. **Boot sequence**:
   ```
   Empty disk → PXE boot from BCM (via vmbr1)
     → DHCP from BCM (10.100.0.x)
     → TFTP: pxelinux.0 → kernel + initrd
     → NFS root: kairos-installer image
     → systemd starts kairos-install.service
     → install-kairos.sh stages binaries to RAM
     → curl | lz4 -d | dd writes kairos-disk.raw to /dev/sda
     → sysrq poweroff (echo o > /proc/sysrq-trigger)
     → GRUB on /dev/sda boots Kairos
     → Kairos runs cloud-config (users, SSH, Palette agent)
     → K3s starts, node registers with Palette
   ```

4. Runs `wait-kairos-boot.sh` which polls BCM for node status:
   - SSH to BCM → `cmsh -c "device; use node001; get status"`
   - Waits up to 30 minutes for the node to complete provisioning
   - Also checks for Kairos boot indicators (e.g., SSH on the node's IP)

---

## Stage 7: Validation (`07-validate.yml`)

**Purpose**: Run comprehensive post-deployment checks.

### Role: `validate`

1. Reads BCM IP from `build/.bcm-ip`
2. Renders `validate.sh` from template
3. Runs 18 validation checks through BCM as a jump host:

| # | Check | Method |
|---|-------|--------|
| 1 | BCM SSH accessible | Direct SSH to BCM external IP |
| 2 | BCM cmd service running | `systemctl is-active cmd` |
| 3 | BCM cmsh functional | `cmsh -c "device; list"` |
| 4 | Node registered in BCM | `cmsh -c "device; use node001; get status"` |
| 5 | Node category is kairos | `cmsh -c "device; use node001; get category"` |
| 6 | Node has IP assigned | `cmsh -c "device; use node001; get ip"` |
| 7 | Node SSH accessible | SSH through BCM jump host |
| 8 | Kairos OS detected | Check `/etc/kairos-release` |
| 9 | K3s running | `systemctl is-active k3s` |
| 10 | kubectl functional | `kubectl get nodes` |
| 11 | Node is Ready | `kubectl get nodes -o jsonpath` |
| 12 | Palette agent running | `systemctl is-active stylus-agent` |
| 13 | Palette registration | Check `/oem/` for Palette config |
| 14 | Network connectivity | Ping BCM internal IP from node |
| 15 | DNS resolution | `nslookup` or `dig` |
| 16 | Disk space adequate | `df -h /` check |
| 17 | NTP sync | Time synchronization check |
| 18 | BCM compatibility | Verify BCM compat scripts are running |

---

## Networking Deep Dive

### Bridge Architecture
```
Physical Host
├── virbr-pve (10.100.0.0/24, NAT)
│     └── Proxmox VE eth0 (10.100.0.2)
│
Proxmox VE
├── vmbr0 (external, bridged to virbr-pve)
│     ├── Proxmox management (:8006)
│     ├── BCM eth1 (DHCP from virbr-pve)
│     └── Internet access
│
└── vmbr1 (internal, 10.100.0.0/24, no physical uplink)
      ├── BCM eth0 (10.100.0.200, static)
      │     ├── DHCP server (for compute nodes)
      │     ├── TFTP server (PXE boot)
      │     ├── NFS server (/cm/images, /cm/shared)
      │     └── HTTP server (:8888, disk image)
      │
      └── Kairos eth0 (10.100.0.x, DHCP from BCM)
            └── PXE boot client
```

### NIC Naming
- `net.ifnames=0 biosdevname=0` kernel args force classic naming (`eth0`, `eth1`)
- BCM net0 (first NIC) = eth0 = internal (vmbr1)
- BCM net1 (second NIC) = eth1 = external (vmbr0)
- NIC order matters for BCM's provisioning setup

### IP Discovery Flow
1. Proxmox adds temporary `10.100.0.1/24` on vmbr1
2. SSH from Proxmox → BCM at `10.100.0.200` (internal)
3. On BCM: `ip -4 addr show eth1 | grep -oP 'inet \K[0-9.]+'` → external IP
4. External IP saved to `build/.bcm-ip`
5. All subsequent access uses external IP (reachable from host)

---

## State Management

### Inter-Stage State
The only state passed between stages is `build/.bcm-ip` — BCM's discovered external IP. All other configuration comes from `inventory/group_vars/all.yml`.

### Build Artifact Caching
Each stage checks for existing artifacts before rebuilding:
- `stat` checks for ISOs, raw images, SSH keys
- `qm status` checks for existing VMs
- This makes the pipeline **safe for re-runs** — completed stages are skipped

### Idempotency Patterns
- File existence checks before downloads/builds
- `qm status` before VM creation
- `grep -q` before config file modifications
- `changed_when` / `failed_when` for accurate change tracking

---

## Known Problems and Resolutions

### Problem 1: Missing `proxmox-testbed` Roles
**Symptom**: Stage 0 fails with `the role 'host_prepare' was not found`
**Root Cause**: The `ansible.cfg` referenced `../proxmox/roles` as a secondary roles path, but the sibling `proxmox-testbed` repository was not present.
**Resolution**: Added `proxmox-testbed` as a git submodule at `proxmox/` and updated `ansible.cfg` to `roles_path = roles:proxmox/roles`.

### Problem 2: Ansible `break_system_packages` Not Supported
**Symptom**: `host_prepare` role fails with `Unsupported parameters for (ansible.builtin.pip) module: break_system_packages`
**Root Cause**: The `break_system_packages` parameter was added in Ansible 2.17, but the host runs Ansible 2.16.3.
**Resolution**: Replaced `break_system_packages: true` with `extra_args: --break-system-packages` which achieves the same result on older Ansible versions.

### Problem 3: BCM Install Timeout (45 min too short)
**Symptom**: Stage 3 fails with `Timeout exceeded` during disk write monitoring. The BCM installer completes 14 steps but takes ~75 minutes total. The original `async: 2700` (45 min) and `TIMEOUT=2700` expired before the install finished.
**Root Cause**: The BCM installer writes ~28 GB over 14 steps. Steps 6 (BCM packages, ~5 GB) and 9 (distro packages to software images) are the longest. On this hardware, the total install exceeds 45 minutes.
**Resolution**: Increased both `async` and `TIMEOUT` to `14400` (4 hours) to accommodate slower hardware. Updated the task name to reflect the new timeout.

### Problem 4: VM Won't Boot After Manual Stop During Install
**Symptom**: After the Ansible timeout killed the first BCM install attempt, manually stopping the VM and switching to disk boot resulted in a VM that wouldn't boot (0 bytes written, no serial output).
**Root Cause**: The install was interrupted at step 9/14 — GRUB was never installed on the disk (that happens in step 7, but the full install including post-install GRUB fixes hadn't completed). Additionally, `qm set --delete args` removed the custom `-chardev` serial config along with the kernel boot args, so even the serial console was gone.
**Resolution**: Destroyed the VM and re-ran `make bcm-vm-create` with the increased timeout, allowing the full 14-step install to complete.

### Problem 5: `proxmox_already_running` Fact Not Propagating
**Symptom**: Stage 0's second play attempted to run roles even when Proxmox was detected as already running.
**Root Cause**: Ansible's `include_role` validates role existence at parse time, not runtime. Even with `when: not proxmox_already_running`, Ansible checks that the role files exist before evaluating the condition. If the roles don't exist (because the submodule isn't cloned), it fails regardless of the skip condition. Additionally, the fact set in play 1 was being re-read as a string `"True"` rather than a boolean in play 2.
**Resolution**: Added `| bool` filter to the `set_fact` task. However, the real fix was adding the submodule so the roles exist.

### Problem 6: CanvOS `.arg.template` Uses Shell Syntax Instead of Jinja2
**Symptom**: CanvOS build runs but all build parameters are empty (`CUSTOM_TAG=`, `OS_DISTRIBUTION=`, etc.), causing the build to fail.
**Root Cause**: The `.arg.template` file used `${CUSTOM_TAG}` shell-style variable substitution, but Ansible's `template` module renders Jinja2. Shell `${}` syntax is treated as literal text by Jinja2.
**Resolution**: Converted the template to use Jinja2 syntax: `{{ CUSTOM_TAG }}` instead of `${CUSTOM_TAG}`.

### Problem 7: Direct Kernel Boot Reboot Loop
**Symptom**: After BCM installation completes, the VM reboots but re-enters the installer ramdisk instead of booting from disk.
**Root Cause**: This is by design. QEMU's `-kernel` and `-initrd` args override the disk's bootloader on every boot. The VM will always boot the extracted kernel/rootfs until these args are removed.
**Resolution**: The role handles this — after detecting install completion (disk write plateau), it stops the VM, runs `qm set --delete args` to remove the direct kernel boot config, and restarts from disk. The key insight is that the "reboot loop" is actually how the pipeline detects completion: writes plateau because the VM re-enters the ramdisk (which is read-only).

---

## Design Decisions and Trade-offs

### Why Direct Kernel Boot Instead of ISO Boot?
Direct kernel boot (`-kernel` + `-initrd`) bypasses the ISO bootloader entirely. This is faster (no GRUB menu timeout, no CDROM driver initialization) and more reliable (no risk of boot menu selection issues). The trade-off is that you must extract the kernel and rootfs from the ISO in stage 2, and you must manually switch to disk boot after installation.

### Why Raw Disk `dd` Instead of Traditional Installer?
The Kairos OS uses an immutable architecture with squashfs images inside partitions. Running `kairos-agent install` in QEMU pre-builds the entire disk image offline, then `dd` clones it block-for-block to the target. This is faster than running the installer on each node and ensures every node gets an identical image. The trade-off is the large image size (mitigated by sparse files and gzip compression).

### Why Stage Binaries to RAM Before `dd`?
The `install-kairos.sh` script copies `curl`, `dd`, `lz4`, `sgdisk`, and their shared libraries to `/dev/shm` (tmpfs/RAM) before running `dd`. This is because `dd` overwrites `/dev/vda` from block 0, destroying the partition table, bootloader, and filesystem. After `dd` starts, the disk is no longer readable — all execution must happen from RAM. The `sysrq-trigger` poweroff (`echo o`) is used because the normal `poweroff` binary on disk would be overwritten.

### Why HTTP Server Instead of NFS for Image Delivery?
The compressed disk image is served via a Python HTTP server on port 8888. While it could be read directly from the NFS-mounted `/cm/shared/kairos/`, HTTP streaming (`curl | lz4 -d | dd`) is more robust for large transfers — it handles retries, doesn't require mount management, and works cleanly from the RAM-staged environment. The trade-off is an additional systemd service on BCM.

### Why 80 GB Virtual Disk for ~8.8 GB of Data?
The raw disk image is 80 GB virtual but only 8.8 GB actual (after `fallocate --dig-holes`). The large virtual size ensures enough room for the Kairos partition layout (OEM, COS_STATE, COS_RECOVERY, COS_PERSISTENT). After compression, the transfer is only 1.6 GB. The trade-off is that `dd` on the compute node writes all 80 GB (including zeros), which takes longer than writing just the data. A future optimization could use `partclone` or filesystem-aware tools instead.

---

## Teardown

`make teardown` runs `playbooks/teardown.yml`:
1. Destroys VM 201 (Kairos)
2. Destroys VM 200 (BCM)
3. Removes vmbr1 bridge
4. `make clean` removes `build/` directory
5. `make clean-all` also removes `dist/` (cached ISOs)
