# BCM + Bare-Metal Provisioning: Network Requirements

## If BCM's VM Is Already On the Provisioning VLAN

If the BCM VM is already connected to the same VLAN as the bare-metal nodes through your existing Proxmox networking, then **no additional bridge or network configuration is needed**. BCM just needs to run its services (DHCP, TFTP, HTTP, NFS) on that interface, and bare-metal nodes on the same VLAN will PXE boot from it.

## What Needs to Be True for PXE Boot to Work

| Requirement | Check |
|-------------|-------|
| BCM VM is on the same L2/VLAN as the bare-metal nodes | Can BCM ping the nodes (or vice versa)? |
| No competing DHCP server on that VLAN | Another DHCP server would race BCM and break PXE |
| DHCP broadcast is not blocked | Some switches filter broadcast between ports — ensure DHCP relay isn't needed |
| TFTP/HTTP ports are open | BCM serves boot files on TFTP (69) and HTTP (8080/8888) |
| Bare-metal nodes are set to PXE/network boot in BIOS | Boot order: network first |

## What BCM Runs on the Provisioning Interface

Once PXE boot starts, BCM handles everything automatically:

```
Bare-metal node powers on
  → BIOS PXE boot → DHCP broadcast on VLAN
  → BCM responds: IP address + TFTP server + boot file path
  → Node downloads kernel + initrd via TFTP
  → Node boots into BCM's node-installer via NFS
  → BCM provisions the node (rsyncs image to disk)
  → kairos-install service runs → dd writes Kairos to disk
  → Node reboots into Kairos
```

## If There IS a Competing DHCP Server

If the VLAN already has a DHCP server (for the training environment), there are two options:

1. **Separate VLAN for provisioning** — isolate BCM's DHCP to a dedicated VLAN. Only bare-metal nodes being provisioned are on this VLAN.

2. **DHCP reservations** — configure the existing DHCP server to not serve the MAC addresses of the bare-metal nodes, letting BCM's DHCP handle them exclusively.

## Summary

If BCM's VM can already talk to the bare-metal nodes on the same VLAN, there's nothing to add. BCM runs DHCP + TFTP + NFS on its provisioning interface, bare-metal nodes PXE boot from it. The only concern is making sure there's no competing DHCP server on that VLAN that would interfere with PXE.
