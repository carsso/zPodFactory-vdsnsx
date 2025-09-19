Param (
  [String]$zPodHostname = $(throw "-zPodHostname required"),
  [String]$zPodUsername = $(throw "-zPodUsername required"),
  [String]$zPodPassword = $(throw "-zPodPassword required"),
  [String[]]$zPodESXiList = $(throw "-zPodESXiList required")
)

$PSStyle.OutputRendering = 'PlainText'
$ProgressPreference = "SilentlyContinue"

Set-PowerCLIConfiguration -WebOperationTimeoutSeconds 60 -Scope Session -Confirm:$false | Out-Null
Set-PowerCLIConfiguration -WebOperationTimeoutSeconds 60 -Confirm:$false | Out-Null

$retry = 30
while (1) {
  $vi = Connect-VIServer -Server $zPodHostname -User $zPodUsername -Password $zPodPassword -ErrorAction SilentlyContinue
  if ($vi -or $retry -lt 1) {
    break
  }

  Write-Host "Waiting for vcsa to be accessible. Sleeping 10s... - [Remaining retries: $($retry)]"
  Start-Sleep -Seconds 10
  $retry--
}

# Fetch the only vCenter Folder
$datacenter = Get-Datacenter -Name "Datacenter"

# Create a new VDS
$vds_name = "DSwitch"
if ($vds = Get-VDSwitch -Name $vds_name -ErrorAction SilentlyContinue) {
  Write-Host "VDS" $vds_name "already exists"
} else {
  Write-Host "Create VDS" $vds_name
  $vds = New-VDSwitch -Name $vds_name -Location $datacenter
}

$vm_network_pg_name = "VM Network"
# Create a new DVPortgroup 
Write-Host "Create portgroup" $vm_network_pg_name
New-VDPortgroup -Name $vm_network_pg_name -Vds $vds -ErrorAction SilentlyContinue | Out-Null
$vm_network_pg = Get-VDPortgroup -Name $vm_network_pg_name -Vds $vds

Write-Host "Add Hosts to VDS"
# Loop through ESXi list and add them to VDS
foreach ($esxi in $zPodESXiList) {
  Write-Host "Add" $esxi "to" $vds_name
  $vds | Add-VDSwitchVMHost -VMHost $esxi -ErrorAction SilentlyContinue | Out-Null

  $vmhostNetworkAdapter = Get-VMHost $esxi | Get-VMHostNetworkAdapter -Physical -Name vmnic1
  Write-Host "Add" $vmhostNetworkAdapter "physical network adapter to" $vds_name
  $vds | Add-VDSwitchPhysicalNetworkAdapter -VMHostNetworkAdapter $vmhostNetworkAdapter -Confirm:$false
}

$vms = Get-VM | Where-Object { $_.Name -notlike "vCLS-*" }
foreach ($vm in $vms) {
  Write-Host "Change" $vm "network portgroup to" $vm_network_pg
  $vm | Get-NetworkAdapter | Set-NetworkAdapter -PortGroup $vm_network_pg -Confirm:$false | Out-Null
}

Write-Host "Add Hosts to VDS"
# Loop through ESXi list and add them to VDS
foreach ($esxi in $zPodESXiList) {
  Write-Host "Migrate" $esxi "vmk0 to" $vm_network_pg
  $vmk = Get-VMHostNetworkAdapter -Name vmk0 -VMHost $esxi
  Set-VMHostNetworkAdapter -PortGroup $vm_network_pg -VirtualNic $vmk -Confirm:$false | Out-Null
}

foreach ($esxi in $zPodESXiList) {
  Write-Host "Add" $esxi "physical network adapters to" $vds_name
  $vmhostNetworkAdapters = Get-VMHost $esxi | Get-VMHostNetworkAdapter -Physical
  foreach ($vmhostNetworkAdapter_name in $vmhostNetworkAdapters) {
    Write-Host "Add" $esxi "physical network adapter" $vmhostNetworkAdapter_name "to" $vds_name
    $vds | Add-VDSwitchPhysicalNetworkAdapter -VMHostNetworkAdapter $vmhostNetworkAdapter_name -Confirm:$false
    Write-Host "Add" $esxi "physical network adapter" $vmhostNetworkAdapter_name "to" $vds_name "again"
    $vds | Add-VDSwitchPhysicalNetworkAdapter -VMHostNetworkAdapter $vmhostNetworkAdapter_name -Confirm:$false
  }
}

foreach ($esxi in $zPodESXiList) {
  Write-Host "Remove" $esxi "vSwitch0 portgroups"
  $vswitch = Get-VirtualSwitch -VMHost $esxi -Name vSwitch0
  $vswitchportgroups = Get-VirtualPortGroup -VirtualSwitch $vswitch
  foreach ($vswitchportgroup in $vswitchportgroups) {
    Write-Host "Remove" $esxi "portgroup" $vswitchportgroup
    Remove-VirtualPortgroup -VirtualPortgroup $vswitchportgroup -Confirm:$false | Out-Null
  }
}

Write-Host "VDS Configuration Done !"