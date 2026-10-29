# Configured workflow graphs

Generated from setup YAML, including conditional edges and join behavior.

## Deploy Cloud Stack in AWS

```mermaid
flowchart TD
  n0["Create Keypair"]
  n1["Create VPC"]
  n2["Ticket - Keypair Failed"]
  n3["EC2 Stats / ALL parents"]
  n4["Deploy Windows GUI Blueprint"]
  n5["Deploy Windows Core Blueprint"]
  n6["Deploy RHEL9 Blueprint"]
  n7["Deploy RHEL10 Blueprint"]
  n8["Deploy Report Server"]
  n9["Update Inventory"]
  n10["Ticket - Instance Failed"]
  n11["Publish VPC Report"]
  n12["Ticket - VPC Failed"]
  n0 -->|success| n3
  n0 -->|failure| n2
  n1 -->|success| n3
  n1 -->|failure| n12
  n3 -->|always| n4
  n3 -->|always| n6
  n3 -->|always| n7
  n3 -->|always| n5
  n3 -->|always| n8
  n4 -->|success| n9
  n4 -->|failure| n10
  n5 -->|success| n9
  n5 -->|failure| n10
  n6 -->|success| n9
  n6 -->|failure| n10
  n7 -->|success| n9
  n7 -->|failure| n10
  n8 -->|success| n9
  n8 -->|failure| n10
  n9 -->|success| n11
```

| Node | Type | Target |
| --- | --- | --- |
| Create Keypair | job_template | Cloud &#124; AWS &#124; Create Keypair |
| Create VPC | job_template | Cloud &#124; AWS &#124; Create VPC |
| Ticket - Keypair Failed | job_template | SUBMIT FEEDBACK |
| EC2 Stats | job_template | Cloud &#124; AWS &#124; Display EC2 Stats |
| Deploy Windows GUI Blueprint | job_template | Cloud &#124; AWS &#124; Create VM |
| Deploy Windows Core Blueprint | job_template | Cloud &#124; AWS &#124; Create VM |
| Deploy RHEL9 Blueprint | job_template | Cloud &#124; AWS &#124; Create VM |
| Deploy RHEL10 Blueprint | job_template | Cloud &#124; AWS &#124; Create VM |
| Deploy Report Server | job_template | Cloud &#124; AWS &#124; Create VM |
| Update Inventory | inventory_source | AWS Inventory |
| Ticket - Instance Failed | job_template | SUBMIT FEEDBACK |
| Publish VPC Report | job_template | Cloud &#124; AWS &#124; VPC Report |
| Ticket - VPC Failed | job_template | SUBMIT FEEDBACK |

## Destroy Cloud Stack in AWS

```mermaid
flowchart TD
  n0["Delete Windows GUI VM"]
  n1["Delete Windows Core VM"]
  n2["Delete RHEL9 VM"]
  n3["Delete RHEL10 VM"]
  n4["Delete Report Server"]
  n5["Update Inventory / ALL parents"]
  n6["Delete Keypair / ALL parents"]
  n7["Delete VPC / ALL parents"]
  n0 -->|success| n5
  n1 -->|success| n5
  n2 -->|success| n5
  n3 -->|success| n5
  n4 -->|success| n5
  n5 -->|success| n6
  n5 -->|success| n7
```

| Node | Type | Target |
| --- | --- | --- |
| Delete Windows GUI VM | job_template | Cloud &#124; AWS &#124; Delete Stack VM |
| Delete Windows Core VM | job_template | Cloud &#124; AWS &#124; Delete Stack VM |
| Delete RHEL9 VM | job_template | Cloud &#124; AWS &#124; Delete Stack VM |
| Delete RHEL10 VM | job_template | Cloud &#124; AWS &#124; Delete Stack VM |
| Delete Report Server | job_template | Cloud &#124; AWS &#124; Delete Stack VM |
| Update Inventory | inventory_source | AWS Inventory |
| Delete Keypair | job_template | Cloud &#124; AWS &#124; Delete Keypair |
| Delete VPC | job_template | Cloud &#124; AWS &#124; Delete VPC |

## Patch Cloud Stack in AWS

```mermaid
flowchart TD
  n0["Take Snapshot"]
  n1["Pre-check RHEL"]
  n2["Pre-check Windows"]
  n3["Patch RHEL"]
  n4["Patch Windows"]
  n5["Post-check RHEL"]
  n6["Restore RHEL"]
  n7["Post-check Windows"]
  n8["Restore Windows"]
  n9["Compliance Report / ALL parents"]
  n0 -->|success| n1
  n0 -->|success| n2
  n1 -->|success| n3
  n2 -->|success| n4
  n3 -->|success| n5
  n3 -->|failure| n6
  n4 -->|success| n7
  n4 -->|failure| n8
  n5 -->|always| n9
  n7 -->|always| n9
```

| Node | Type | Target |
| --- | --- | --- |
| Take Snapshot | job_template | Cloud &#124; AWS &#124; Snapshot EC2 |
| Pre-check RHEL | job_template | Cloud &#124; AWS &#124; Patch Pre-check RHEL |
| Pre-check Windows | job_template | Cloud &#124; AWS &#124; Patch Pre-check Windows |
| Patch RHEL | job_template | Cloud &#124; AWS &#124; Patch RHEL |
| Patch Windows | job_template | Cloud &#124; AWS &#124; Patch Windows |
| Post-check RHEL | job_template | Cloud &#124; AWS &#124; Patch Post-check RHEL |
| Restore RHEL | job_template | Cloud &#124; AWS &#124; Restore EC2 from Snapshot |
| Post-check Windows | job_template | Cloud &#124; AWS &#124; Patch Post-check Windows |
| Restore Windows | job_template | Cloud &#124; AWS &#124; Restore EC2 from Snapshot |
| Compliance Report | job_template | Cloud &#124; AWS &#124; Patch Compliance Report |

## Infrastructure | ROSA - Lifecycle (Create)

```mermaid
flowchart TD
  n0["Preflight Checks"]
  n1["Create Cluster"]
  n2["Wait for Ready"]
  n3["Destroy on Failure"]
  n4["Preflight Failed"]
  n5["Creation Failed"]
  n0 -->|success| n1
  n0 -->|failure| n4
  n1 -->|success| n2
  n1 -->|failure| n3
  n2 -->|failure| n3
  n3 -->|always| n5
```

| Node | Type | Target |
| --- | --- | --- |
| Preflight Checks | job_template | Infrastructure &#124; ROSA - Preflight Checks |
| Create Cluster | job_template | Infrastructure &#124; ROSA - Create Cluster |
| Wait for Ready | job_template | Infrastructure &#124; ROSA - Wait for Ready |
| Destroy on Failure | job_template | Infrastructure &#124; ROSA - Destroy Cluster |
| Preflight Failed | job_template | SUBMIT FEEDBACK |
| Creation Failed | job_template | SUBMIT FEEDBACK |

## Infrastructure | ROSA - Lifecycle (Destroy)

```mermaid
flowchart TD
  n0["Destroy Cluster"]
  n1["Destroy Failed"]
  n0 -->|failure| n1
```

| Node | Type | Target |
| --- | --- | --- |
| Destroy Cluster | job_template | Infrastructure &#124; ROSA - Destroy Cluster |
| Destroy Failed | job_template | SUBMIT FEEDBACK |

## Infrastructure | Config Drift - Full Setup

```mermaid
flowchart TD
  n0["Check Cloud Stack"]
  n1["Cloud Stack Required"]
  n2["Provision Kafka"]
  n3["Sync Inventory"]
  n4["Deploy Filebeat"]
  n5["Setup EDA"]
  n0 -->|success| n2
  n0 -->|failure| n1
  n2 -->|success| n3
  n3 -->|success| n4
  n4 -->|success| n5
```

| Node | Type | Target |
| --- | --- | --- |
| Check Cloud Stack | job_template | Infrastructure &#124; Config Drift - Check Cloud Stack |
| Cloud Stack Required | job_template | SUBMIT FEEDBACK |
| Provision Kafka | job_template | Infrastructure &#124; AWS - Provision Kafka Queue |
| Sync Inventory | inventory_source | AWS Inventory |
| Deploy Filebeat | job_template | LINUX &#124; Config Drift - Deploy Audit and Filebeat |
| Setup EDA | job_template | Infrastructure &#124; Setup Rulebook for Kafka Queue - Config Drift & Remediation |

## Linux | Compliance Workflow

```mermaid
flowchart TD
  n0["Compliance Report"]
  n1["Update Inventory"]
  n2["Compliance Enforce"]
  n0 -->|success| n1
  n1 -->|success| n2
```

| Node | Type | Target |
| --- | --- | --- |
| Compliance Report | job_template | LINUX &#124; Multi-profile Compliance Report |
| Update Inventory | inventory_source | AWS Inventory |
| Compliance Enforce | job_template | LINUX &#124; Compliance Enforce |

## NETWORK | Panos | Demo Firewall Workflow

```mermaid
flowchart TD
  n0["Deploy"]
  n1["Sync Inventory"]
  n2["Configure Firewall"]
  n3["Configure Webserver / ALL parents"]
  n0 -->|success| n1
  n0 -->|success| n2
  n1 -->|success| n3
  n2 -->|success| n3
```

| Node | Type | Target |
| --- | --- | --- |
| Deploy | job_template | NETWORK &#124; Panos &#124; Deploy |
| Sync Inventory | inventory_source | Panos Demo Instances |
| Configure Firewall | job_template | NETWORK &#124; Panos &#124; Configure Firewall |
| Configure Webserver | job_template | NETWORK &#124; Panos &#124; Configure Webserver |

## NETWORK | Deploy Containerlab Stack

```mermaid
flowchart TD
  n0["Provision Hypervisor"]
  n1["Deploy Topology"]
  n2["Sync ContainerLab Inventory"]
  n3["Ticket - Provision Failed"]
  n0 -->|success| n1
  n0 -->|failure| n3
  n1 -->|success| n2
```

| Node | Type | Target |
| --- | --- | --- |
| Provision Hypervisor | job_template | NETWORK &#124; Containerlab &#124; Provision Hypervisor |
| Deploy Topology | job_template | NETWORK &#124; Containerlab &#124; Deploy Topology |
| Sync ContainerLab Inventory | inventory_source | ContainerLab Hypervisor |
| Ticket - Provision Failed | job_template | SUBMIT FEEDBACK |

## NETWORK | Destroy Containerlab Stack

```mermaid
flowchart TD
  n0["Teardown Topology"]
  n1["Teardown Hypervisor"]
  n0 -->|always| n1
```

| Node | Type | Target |
| --- | --- | --- |
| Teardown Topology | job_template | NETWORK &#124; Containerlab &#124; Teardown Topology |
| Teardown Hypervisor | job_template | NETWORK &#124; Containerlab &#124; Teardown Hypervisor |

## Patch Dev

```mermaid
flowchart TD
  n0["Update Inventory"]
  n1["Publish New Patches"]
  n2["Check for Updates / ALL parents"]
  n3["Ticket - Update Check"]
  n4["Approve Patches"]
  n5["Apply Patches"]
  n6["Ticket - Update Apply"]
  n0 -->|success| n2
  n1 -->|success| n2
  n2 -->|success| n4
  n2 -->|failure| n3
  n4 -->|success| n5
  n5 -->|failure| n6
```

| Node | Type | Target |
| --- | --- | --- |
| Update Inventory | inventory_source | Satellite Inventory |
| Publish New Patches | job_template | SATELLITE &#124; Publish Content View Version |
| Check for Updates | job_template | LINUX &#124; Patching |
| Ticket - Update Check | job_template | SUBMIT FEEDBACK |
| Approve Patches | workflow_approval | Push patches to Dev? |
| Apply Patches | job_template | LINUX &#124; Patching |
| Ticket - Update Apply | job_template | SUBMIT FEEDBACK |

## Setup Active Directory Domain

```mermaid
flowchart TD
  n0["Create Keypair"]
  n1["Create VPC"]
  n2["Create Domain Controller"]
  n3["Create Computer (1)"]
  n4["Create Computer (2)"]
  n5["Inventory Sync / ALL parents"]
  n6["Test Connectivity"]
  n7["Create Domain"]
  n8["Join Domain"]
  n9["AWS Inventory Sync"]
  n10["AD LDAP Inventory Sync"]
  n11["Cleanup Resources"]
  n12["PowerShell Validation"]
  n13["Kerberos Validation"]
  n0 -->|success| n1
  n1 -->|success| n2
  n1 -->|success| n3
  n1 -->|success| n4
  n2 -->|success| n5
  n3 -->|success| n5
  n4 -->|success| n5
  n5 -->|success| n6
  n6 -->|success| n7
  n6 -->|failure| n11
  n7 -->|success| n8
  n7 -->|failure| n11
  n8 -->|success| n9
  n8 -->|success| n10
  n8 -->|failure| n11
  n9 -->|success| n12
  n10 -->|success| n13
```

| Node | Type | Target |
| --- | --- | --- |
| Create Keypair | job_template | Cloud &#124; AWS &#124; Create Keypair |
| Create VPC | job_template | Cloud &#124; AWS &#124; Create VPC |
| Create Domain Controller | job_template | Cloud &#124; AWS &#124; Create VM |
| Create Computer (1) | job_template | Cloud &#124; AWS &#124; Create VM |
| Create Computer (2) | job_template | Cloud &#124; AWS &#124; Create VM |
| Inventory Sync | inventory_source | AWS Inventory |
| Test Connectivity | job_template | WINDOWS &#124; Test Connectivity |
| Create Domain | job_template | WINDOWS &#124; AD &#124; Create Domain |
| Join Domain | job_template | WINDOWS &#124; AD &#124; Join Domain |
| AWS Inventory Sync | inventory_source | AWS Inventory |
| AD LDAP Inventory Sync | inventory_source | AD LDAP Inventory |
| Cleanup Resources | job_template | WINDOWS &#124; Rollback |
| PowerShell Validation | job_template | WINDOWS &#124; Run PowerShell |
| Kerberos Validation | job_template | WINDOWS &#124; Run PowerShell &#124; Kerberos |
