# VM 146 — No Network Connectivity on VLAN 3103

## Summary

VM 146 (`lab103-edge-vm-1`) on `swlk-prxmx04` is running and the guest OS is fully booted, but the VM has zero network connectivity. The VM cannot reach any host on VLAN 3103, including the Proxmox host itself and the default gateway.

## VM Configuration

```
net0: virtio=BC:24:11:7F:33:7C,bridge=vmbr0,tag=3103
net1: e1000=BC:24:11:ED:21:50,bridge=vmbr0,tag=3103
```

## What We Tested Inside the VM

| Test | Result |
|------|--------|
| `ip link show ens18` | State UP, link detected |
| `ip addr` | `172.16.10.250/24` manually assigned |
| `ip route` | Default via `172.16.10.1` |
| `ping 172.16.10.1` (gateway) | No response |
| `ping 172.16.10.101` (Proxmox host) | No response |
| `arping -I ens18 172.16.10.101` | No response |
| `ssh root@127.0.0.1` | Works (OS and SSH are functional) |

The `arping` failure confirms this is a Layer 2 issue — ARP frames from the VM are not reaching the physical network through `vmbr0`. This is not a routing, firewall, or guest OS issue.

## What We Need

The VM's traffic tagged to VLAN 3103 needs to actually reach the physical switch. Something between the VM's tap interface and the physical network is not passing frames.

**Can you verify:**

1. Is `vmbr0` configured as VLAN-aware? (`bridge-vlan-aware yes` in `/etc/network/interfaces` on `swlk-prxmx04`)
2. What does `bridge vlan show` output on `swlk-prxmx04`? (Shows which VLANs are allowed on which bridge ports)
3. Is the physical NIC bridged into `vmbr0` trunking VLAN 3103 at the switch level?
4. Has any VM on this node ever had working connectivity on `vmbr0` with `tag=3103`?
5. Are there any SDN zones, firewall rules, or traffic policies that could be filtering VM traffic?

**Alternatively**, if you can share the output of these commands from `swlk-prxmx04`:

```bash
cat /etc/network/interfaces
bridge vlan show
ip link show vmbr0
ovs-vsctl show   # if using OVS
```

That would help us diagnose exactly where the traffic is being dropped.

## Context

We are deploying a BCM (Bright Cluster Manager) head node as a VM on this Proxmox cluster. BCM needs network connectivity on VLAN 3103 to:
- Receive an IP address (static or DHCP)
- Serve PXE boot to bare-metal compute nodes on the same VLAN
- Provide SSH management access

The VM OS is installed and functional — we just need the network bridge to pass traffic.
