# BCM Dual NIC Same Subnet Issue

## Summary

BCM head node VM (VMID 146 on `swlk-prxmx04`) has two NICs on the same VLAN (3103) and same subnet (172.16.103.0/24). This causes ARP resolution failures for eth0, breaking PXE provisioning for compute nodes.

## Current Configuration

| Interface | MAC | IP | Proxmox Config | Role |
|-----------|-----|-----|----------------|------|
| eth0 (net0) | BC:24:11:7F:33:7C | 172.16.103.201/24 | virtio, vmbr0, tag=3103 | Internal / Provisioning (DHCP, TFTP, PXE) |
| eth1 (net1) | BC:24:11:ED:21:50 | 172.16.103.202/24 | e1000, vmbr0, tag=3103 | External / Management |

## Routing Table (inside BCM)

```
default via 172.16.103.1 dev eth1 onlink
172.16.103.0/24 dev eth1 proto kernel scope link src 172.16.103.202
172.16.103.0/24 dev eth0 proto kernel scope link src 172.16.103.201
```

## Problem

Linux's weak host model does not bind ARP replies to the interface that owns the IP. When an external host sends an ARP request for 172.16.103.201 (eth0), the kernel may reply from eth1's MAC, or route the reply out eth1 due to the default route. The upstream switch sees a MAC/port mismatch and drops it.

**Result:** 172.16.103.201 is unreachable via unicast from other hosts on VLAN 3103, even though both NICs are on that VLAN.

## Impact on PXE Provisioning

BCM's DHCP server listens on eth0 and responds to broadcast DHCPDISCOVER with:

```
IP Offered: 172.16.103.17
Server Identifier: 172.16.103.201
Router: 172.16.103.201
DNS: 172.16.103.201
TFTP next-server: 172.16.103.201
```

A compute node on VLAN 3103 receives this DHCP lease via broadcast, but then cannot reach the TFTP server (172.16.103.201) to download the PXE boot files because unicast ARP for that IP fails.

## Verified Behavior

- `ping 172.16.103.201` from 172.16.103.2 (same VLAN): **100% packet loss**, ARP state `FAILED`
- `ping 172.16.103.202` from 172.16.103.2: **works**, ARP state `REACHABLE`
- DHCP broadcast discover from 172.16.103.2: **works**, gets offer from 172.16.103.201

## Recommended Solutions

### Option A: Separate VLANs (preferred)

Put eth0 on a dedicated provisioning VLAN (e.g., VLAN 3104) with its own subnet (e.g., 10.141.0.0/16). This matches BCM's intended architecture where internal and external networks are isolated.

- net0: `vmbr0,tag=3104` (provisioning)
- net1: `vmbr0,tag=3103` (management)
- Compute nodes on VLAN 3104 for PXE boot

### Option B: Fix ARP on BCM

Apply kernel sysctl settings to enforce strict ARP behavior:

```
net.ipv4.conf.all.arp_filter = 1
net.ipv4.conf.all.arp_announce = 2
net.ipv4.conf.eth0.arp_filter = 1
net.ipv4.conf.eth0.arp_announce = 2
```

Add a source-based routing rule so traffic from 172.16.103.201 always goes out eth0:

```
ip rule add from 172.16.103.201 table 100
ip route add 172.16.103.0/24 dev eth0 src 172.16.103.201 table 100
ip route add default via 172.16.103.1 dev eth0 table 100
```

### Option C: Single NIC

Use only eth1 (172.16.103.202) for both management and provisioning. Reconfigure BCM's internalnet to use 172.16.103.202 as the provisioning IP.
