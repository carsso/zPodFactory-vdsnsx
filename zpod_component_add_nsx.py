import hashlib
import ssl
import time
from typing import Any, Dict

from zpodcommon import models as M
from zpodcommon.lib.dbutils import DBUtils
from zpodcommon.lib.nsx import NsxClient
from zpodengine.lib import database


def execute_config_script(
    zpod_component_id: int,
) -> None:
    """
    NSX-T configuration equivalent to Terraform script
    Configures Compute Manager, IP Blocks/Pools, Transport Node Profiles and Host Collections

    Args:
        zpod_component_id: The zPod component ID
    """

    with database.get_session_ctx() as session:
        zpod_component = session.get(M.ZpodComponent, zpod_component_id)
        zpod = zpod_component.zpod

        if DBUtils.get_setting_value("ff_component_wait_for_status") != "true":
            raise ValueError("ff_component_wait_for_status is not true")

        nsx = NsxClient.auth_by_zpod(zpod=zpod)

        print("Accept NSX Manager EULA ...")
        response = nsx.post(url="/api/v1/eula/accept")
        response.safejson()

        # 1. Create Compute Manager
        print("Creating Compute Manager...")
        try:
            compute_manager = create_compute_manager(nsx, zpod)
        except Exception as e:
            # Handle specific NSX error for already registered compute manager
            if "error_code" in str(e) and "7050" in str(e):
                print(f"Compute Manager already registered error detected: {e}")
                print("Attempting to find and use existing compute manager...")
                vsphere_hostname = f"vcsa.{zpod.domain}"
                existing_cm = get_existing_compute_manager(nsx, vsphere_hostname)
                if existing_cm:
                    compute_manager = existing_cm
                    print(f"Using existing compute manager: {existing_cm.get('id')}")
                else:
                    print(
                        f"Could not find existing compute manager for {vsphere_hostname}"
                    )
                    raise e
            else:
                raise e

        # 2. Check Compute Manager connection status
        print("Checking Compute Manager connection status...")
        try:
            check_compute_manager_status(
                nsx, compute_manager["id"], timeout=120
            )  # 2 minutes
        except Exception as e:
            print(f"Warning: Could not verify Compute Manager status: {e}")
            print("Continuing with configuration...")

        # 3. Create IP Block
        print("Creating IP Block...")
        ip_block = create_ip_block(nsx)

        # 4. Create IP Pool
        print("Creating IP Pool...")
        ip_pool = create_ip_pool(nsx)

        # 5. Create Block Subnet
        print("Creating Block Subnet...")
        # Verify that both ip_pool and ip_block have the required 'path' key
        if "path" not in ip_pool:
            print(f"Error: IP Pool object missing 'path' key. Object: {ip_pool}")
            raise ValueError("IP Pool object missing 'path' key")
        if "path" not in ip_block:
            print(f"Error: IP Block object missing 'path' key. Object: {ip_block}")
            raise ValueError("IP Block object missing 'path' key")

        create_block_subnet(nsx, ip_pool["path"], ip_block["path"])

        # 6. Create Uplink Host Switch Profile
        print("Creating Uplink Host Switch Profile...")
        uplink_profile = create_uplink_host_switch_profile(nsx)

        # 7. Get Transport Zones
        print("Getting Transport Zones...")
        overlay_tz = get_transport_zone(nsx, "nsx-overlay-transportzone", "OVERLAY")
        vlan_tz = get_transport_zone(nsx, "nsx-vlan-transportzone", "VLAN")

        # 8. Retrieve the DVS ID from the compute manager
        print("Retrieving DVS ID from compute manager...")
        vsphere_dvs_id = get_dvs_from_compute_manager(nsx, compute_manager["id"])

        # 9. Create Transport Node Profile
        print("Creating Transport Node Profile...")
        tnp = create_transport_node_profile(
            nsx,
            ip_pool["path"],
            overlay_tz["path"],
            vlan_tz["path"],
            uplink_profile["path"],
            vsphere_dvs_id,
        )

        # 10. Get Compute Collection (Cluster)
        print("Getting Compute Collection...")
        compute_collection = get_compute_collection(nsx)

        # 11. Create Host Transport Node Collection
        print("Creating Host Transport Node Collection...")
        # Use the unique_id from Policy API response (Fabric API identifier)
        tnp_id = tnp.get("unique_id")
        htnc = create_host_transport_node_collection(
            nsx, compute_collection["external_id"], tnp_id
        )

        # 12. Wait for collection realization
        print("Waiting for Host Transport Node Collection realization...")
        # Get the path from the response, construct it if not present
        htnc_path = htnc.get("path")
        if not htnc_path and "id" in htnc:
            htnc_path = f"/transport-node-collections/{htnc['id']}"
        elif not htnc_path:
            print("Warning: Could not determine HTNC path, skipping realization wait")
            return

        wait_for_htnc_realization(nsx, htnc_path)

        # 13. Verify configuration status
        print("Verifying NSX-T configuration status...")
        verify_nsx_configuration_status(nsx, compute_collection["external_id"])

        print("NSX-T configuration completed successfully!")


def get_existing_compute_manager(
    nsx: NsxClient, server_name: str
) -> Dict[str, Any] | None:
    """Check if a compute manager already exists for the given server"""
    try:
        response = nsx.get(url="/api/v1/fabric/compute-managers")
        compute_managers = response.safejson()

        for cm in compute_managers.get("results", []):
            if cm.get("server") == server_name:
                print(
                    f"Found existing Compute Manager for server {server_name}: {cm.get('id')}"
                )
                return cm

        return None
    except Exception as e:
        print(f"Error checking for existing compute managers: {e}")
        return None


def create_compute_manager(nsx: NsxClient, zpod) -> Dict[str, Any]:
    """Create vCenter Compute Manager or return existing one"""
    vsphere_hostname = f"vcsa.{zpod.domain}"
    vsphere_username = f"administrator@{zpod.domain}"
    vsphere_password = zpod.password
    vsphere_thumbprint = get_ssl_thumbprint(vsphere_hostname)

    # Check if compute manager already exists
    existing_cm = get_existing_compute_manager(nsx, vsphere_hostname)
    if existing_cm:
        print(
            f"Compute Manager for {vsphere_hostname} already exists, using existing one"
        )
        return existing_cm

    payload = {
        "description": "Compute Manager",
        "display_name": vsphere_hostname,
        "server": vsphere_hostname,
        "create_service_account": True,
        "set_as_oidc_provider": True,
        "access_level_for_oidc": "FULL",
        "credential": {
            "credential_type": "UsernamePasswordLoginCredential",
            "username": vsphere_username,
            "password": vsphere_password,
            "thumbprint": vsphere_thumbprint,
        },
        "origin_type": "vCenter",
    }

    try:
        response = nsx.post(url="/api/v1/fabric/compute-managers", json=payload)
        result = response.safejson()

        # Print debug information
        print(f"Compute Manager created with ID: {result.get('id', 'UNKNOWN')}")
        print(f"Compute Manager status: {result.get('connection_status', 'UNKNOWN')}")

        return result
    except Exception as e:
        # Check if it's the specific error about server already registered
        if "already registered with NSX" in str(e) or "error_code" in str(e):
            print(
                f"Compute Manager for {vsphere_hostname} already registered, attempting to find existing one"
            )
            # Try to find the existing compute manager again
            existing_cm = get_existing_compute_manager(nsx, vsphere_hostname)
            if existing_cm:
                return existing_cm
            else:
                print(f"Could not find existing compute manager for {vsphere_hostname}")
                raise e
        else:
            raise e


def check_compute_manager_status(
    nsx: NsxClient, compute_manager_id: str, timeout: int = 120
) -> None:
    """Check Compute Manager connection status (simplified approach)"""
    start_time = time.time()
    while time.time() - start_time < timeout:
        try:
            # Use the specific status endpoint for compute manager
            response = nsx.get(
                url=f"/api/v1/fabric/compute-managers/{compute_manager_id}/status"
            )
            status_info = response.safejson()

            connection_status = status_info.get("connection_status")
            if connection_status == "UP":
                print(f"Compute Manager {compute_manager_id} is UP and ready")
                return
            elif connection_status == "DOWN":
                print(f"Compute Manager {compute_manager_id} is DOWN, waiting...")
            elif connection_status == "CONNECTING":
                print(f"Compute Manager {compute_manager_id} is CONNECTING, waiting...")
            else:
                print(
                    f"Compute Manager {compute_manager_id} connection status: {connection_status}"
                )

        except Exception as e:
            print(f"Error checking Compute Manager {compute_manager_id}: {e}")
            print(
                f"Compute Manager {compute_manager_id} might not exist or be accessible"
            )

        time.sleep(10)  # Check every 10 seconds

    # Final check before timeout
    try:
        response = nsx.get(
            url=f"/api/v1/fabric/compute-managers/{compute_manager_id}/status"
        )
        status_info = response.safejson()

        connection_status = status_info.get("connection_status")
        if connection_status == "UP":
            print(f"Compute Manager {compute_manager_id} is UP (final check)")
            return
        else:
            print(
                f"Warning: Compute Manager {compute_manager_id} connection status is {connection_status} after {timeout}s"
            )
            print("This may be normal for compute managers that are still initializing")
            print("Continuing with configuration...")
            return  # Don't raise an error, just continue
    except Exception as final_e:
        print(f"Final check failed: {final_e}")
        print("Continuing with configuration...")
        return  # Don't raise an error, just continue


def get_existing_ip_block(nsx: NsxClient, display_name: str) -> Dict[str, Any] | None:
    """Check if an IP block already exists with the given display name"""
    try:
        response = nsx.get(url="/api/v1/pools/ip-blocks")
        ip_blocks = response.safejson()

        for block in ip_blocks.get("results", []):
            if block.get("display_name") == display_name:
                print(f"Found existing IP Block: {display_name}")
                # Ensure the existing block has a 'path' key
                if "path" not in block and "id" in block:
                    block["path"] = f"/api/v1/pools/ip-blocks/{block['id']}"
                return block

        return None
    except Exception as e:
        print(f"Error checking for existing IP blocks: {e}")
        return None


def create_ip_block(nsx: NsxClient) -> Dict[str, Any]:
    """Create IP Block or return existing one"""
    nsx_ipblock_cidr = "172.16.20.0/24"
    display_name = "ip-block-vtep"

    # Check if IP block already exists
    existing_block = get_existing_ip_block(nsx, display_name)
    if existing_block:
        print(f"IP Block '{display_name}' already exists, using existing one")
        # Ensure the existing block has a 'path' key
        if "path" not in existing_block and "id" in existing_block:
            existing_block["path"] = f"/api/v1/pools/ip-blocks/{existing_block['id']}"
        return existing_block

    payload = {
        "resource_type": "IpBlock",
        "display_name": display_name,
        "cidr": nsx_ipblock_cidr,
    }

    try:
        response = nsx.post(url="/api/v1/pools/ip-blocks", json=payload)
        ip_block = response.safejson()
        # Ensure the created block has a 'path' key
        if "path" not in ip_block and "id" in ip_block:
            ip_block["path"] = f"/api/v1/pools/ip-blocks/{ip_block['id']}"
        return ip_block
    except Exception as e:
        if "already exists" in str(e).lower() or "duplicate" in str(e).lower():
            print(
                f"IP Block '{display_name}' already exists, attempting to find existing one"
            )
            existing_block = get_existing_ip_block(nsx, display_name)
            if existing_block:
                # Ensure the existing block has a 'path' key
                if "path" not in existing_block and "id" in existing_block:
                    existing_block["path"] = (
                        f"/api/v1/pools/ip-blocks/{existing_block['id']}"
                    )
                return existing_block
        raise e


def get_existing_ip_pool(nsx: NsxClient, display_name: str) -> Dict[str, Any] | None:
    """Check if an IP pool already exists with the given display name"""
    try:
        response = nsx.get(url="/policy/api/v1/infra/ip-pools")
        ip_pools = response.safejson()

        for pool in ip_pools.get("results", []):
            if pool.get("display_name") == display_name:
                print(f"Found existing IP Pool: {display_name}")
                # Ensure the existing pool has a 'path' key
                if "path" not in pool and "id" in pool:
                    pool["path"] = f"/policy/api/v1/infra/ip-pools/{pool['id']}"
                return pool

        return None
    except Exception as e:
        print(f"Error checking for existing IP pools: {e}")
        return None


def create_ip_pool(nsx: NsxClient) -> Dict[str, Any]:
    """Create IP Pool or return existing one"""
    display_name = "ip-pool-vtep"

    # Check if IP pool already exists
    existing_pool = get_existing_ip_pool(nsx, display_name)
    if existing_pool:
        print(f"IP Pool '{display_name}' already exists, using existing one")
        return existing_pool

    payload = {"resource_type": "IpPool", "display_name": display_name}

    try:
        # Use Policy API with PUT method and pool ID in URL for regular IP Pools
        pool_id = display_name.lower().replace(" ", "-").replace("_", "-")
        response = nsx.put(url=f"/policy/api/v1/infra/ip-pools/{pool_id}", json=payload)
        ip_pool = response.safejson()
        # Ensure the created pool has a 'path' key
        if "path" not in ip_pool and "id" in ip_pool:
            ip_pool["path"] = f"/policy/api/v1/infra/ip-pools/{ip_pool['id']}"
        return ip_pool
    except Exception as e:
        if "already exists" in str(e).lower() or "duplicate" in str(e).lower():
            print(
                f"IP Pool '{display_name}' already exists, attempting to find existing one"
            )
            existing_pool = get_existing_ip_pool(nsx, display_name)
            if existing_pool:
                return existing_pool
        raise e


def create_block_subnet(
    nsx: NsxClient, pool_path: str, block_path: str
) -> Dict[str, Any]:
    """Create Block Subnet by adding a subnet to the IP Pool"""
    # Extract pool ID from pool_path
    pool_id = pool_path.split("/")[-1]

    # First, get the current IP Pool to see its structure
    response = nsx.get(url=f"/api/v1/infra/ip-pools/{pool_id}/ip-subnets")
    response.safejson()

    # Create a subnet configuration
    subnet_config = {
        "resource_type": "IpAddressPoolStaticSubnet",
        "display_name": f"subnet-{pool_id}",
        "cidr": "172.16.20.0/24",  # Use the same CIDR as the IP block
        "dns_nameservers": [],
        "allocation_ranges": [{"start": "172.16.20.1", "end": "172.16.20.128"}],
    }

    # Check if subnet already exists by trying to get it directly
    subnet_id = f"subnet-{pool_id}"
    subnet_exists = False

    try:
        # Try to get the specific subnet to see if it exists
        check_response = nsx.get(
            url=f"/api/v1/infra/ip-pools/{pool_id}/ip-subnets/{subnet_id}"
        )
        if check_response.status_code == 200:
            print(f"Subnet {subnet_config['cidr']} already exists in IP Pool")
            subnet_exists = True
    except Exception:
        # Subnet doesn't exist, we can create it
        pass

    # Only add the subnet if it doesn't exist
    if not subnet_exists:
        print(f"Adding subnet {subnet_config['cidr']} to IP Pool")

        # Use PUT method with subnet ID to create/update a subnet in the IP pool
        response = nsx.put(
            url=f"/api/v1/infra/ip-pools/{pool_id}/ip-subnets/{subnet_id}",
            json=subnet_config,
        )
        return response.safejson()
    else:
        print("Using existing subnet configuration")
        # Return the existing subnet if it already exists
        return check_response.safejson()


def create_uplink_host_switch_profile(nsx: NsxClient) -> Dict[str, Any]:
    """Create or get existing Uplink Host Switch Profile"""
    display_name = "hsp"

    response = nsx.get(url=f"/policy/api/v1/infra/host-switch-profiles/{display_name}")
    existing_profile = response.safejson()

    # Check if the profile exists (safejson returns {} for 404 errors)
    if existing_profile and "id" in existing_profile:
        print(f"Found existing Uplink Host Switch Profile: {display_name}")
        return existing_profile
    else:
        print(
            f"Uplink Host Switch Profile '{display_name}' doesn't exist, creating it..."
        )

    overlay_vlan = 20
    payload = {
        "resource_type": "PolicyUplinkHostSwitchProfile",
        "description": "Uplink host switch profile",
        "display_name": "Uplink host switch profile",
        "transport_vlan": overlay_vlan,
        "overlay_encap": "GENEVE",
        "teaming": {
            "active_list": [{"uplink_name": "uplink1", "uplink_type": "PNIC"}],
            "standby_list": [{"uplink_name": "uplink2", "uplink_type": "PNIC"}],
            "policy": "FAILOVER_ORDER",
        },
    }

    response = nsx.put(
        url=f"/policy/api/v1/infra/host-switch-profiles/{display_name}", json=payload
    )
    return response.safejson()


def get_transport_zone(
    nsx: NsxClient, display_name: str, transport_type: str
) -> Dict[str, Any]:
    """Get Transport Zone using Policy API"""
    # Get all transport zones since Policy API doesn't support query parameters
    response = nsx.get(
        url="/policy/api/v1/infra/sites/default/enforcement-points/default/transport-zones"
    )
    zones = response.safejson()

    # Filter zones by display_name and tz_type
    # Map transport_type to the correct tz_type values used by Policy API
    tz_type_mapping = {"OVERLAY": "OVERLAY_STANDARD", "VLAN": "VLAN_BACKED"}
    expected_tz_type = tz_type_mapping.get(transport_type, transport_type)

    matching_zones = []
    for zone in zones.get("results", []):
        if (
            zone.get("display_name") == display_name
            and zone.get("tz_type") == expected_tz_type
        ):
            matching_zones.append(zone)

    if not matching_zones:
        raise ValueError(
            f"Transport Zone '{display_name}' of type '{transport_type}' not found"
        )

    zone = matching_zones[0]

    # Ensure the zone has a 'path' key with Policy API path
    if "path" not in zone and "id" in zone:
        zone["path"] = (
            f"/infra/sites/default/enforcement-points/default/transport-zones/{zone['id']}"
        )

    return zone


def get_existing_transport_node_profile(
    nsx: NsxClient, display_name: str
) -> Dict[str, Any] | None:
    """Check if a transport node profile already exists with the given display name"""
    try:
        response = nsx.get(url="/policy/api/v1/infra/host-transport-node-profiles")
        tnps = response.safejson()

        for tnp in tnps.get("results", []):
            if tnp.get("display_name") == display_name:
                print(f"Found existing Transport Node Profile: {display_name}")
                return tnp

        return None
    except Exception as e:
        print(f"Error checking for existing transport node profiles: {e}")
        return None


def get_dvs_from_compute_manager(nsx: NsxClient, compute_manager_id: str) -> str:
    """Retrieves the UUID of the DVS (Distributed Virtual Switch) from a compute manager via the search API"""
    try:
        # Use the search API to find DVS from the compute manager
        query = f"(resource_type:DistributedVirtualSwitch AND _meta.productInfo.version:>=7000000000 AND !owner_nsx:OTHER AND origin_id:{compute_manager_id})"
        response = nsx.get(url=f"/policy/api/v1/search?query={query}")
        search_results = response.safejson()

        # Search for DVS in the results
        for result in search_results.get("results", []):
            if result.get("resource_type") == "DistributedVirtualSwitch":
                dvs_uuid = result.get("uuid")
                if dvs_uuid:
                    print(f"Found DVS with UUID: {dvs_uuid}")
                    return dvs_uuid

        # If no DVS found, use the default name
        print("No DVS found, using default name 'DSwitch'")
        return "DSwitch"

    except Exception as e:
        print(f"Error retrieving DVS: {e}")
        print("Using default name 'DSwitch'")
        return "DSwitch"


def create_transport_node_profile(
    nsx: NsxClient,
    ip_pool_path: str,
    overlay_tz_path: str,
    vlan_tz_path: str,
    uplink_profile_path: str,
    vsphere_dvs_id: str,
) -> Dict[str, Any]:
    """Create Transport Node Profile or return existing one"""
    display_name = "tnp"

    # Check if transport node profile already exists
    existing_tnp = get_existing_transport_node_profile(nsx, display_name)
    if existing_tnp:
        print(
            f"Transport Node Profile '{display_name}' already exists, using existing one"
        )
        return existing_tnp

    payload = {
        "resource_type": "TransportNodeProfile",
        "display_name": display_name,
        "host_switch_spec": {
            "resource_type": "StandardHostSwitchSpec",
            "host_switches": [
                {
                    "host_switch_id": vsphere_dvs_id,
                    "host_switch_type": "VDS",
                    "host_switch_mode": "STANDARD",
                    "host_switch_profile_ids": [
                        {"key": "UplinkHostSwitchProfile", "value": uplink_profile_path}
                    ],
                    "transport_zone_endpoints": [
                        {
                            "transport_zone_id": overlay_tz_path,
                            "transport_zone_profile_ids": [],
                        },
                        {
                            "transport_zone_id": vlan_tz_path,
                            "transport_zone_profile_ids": [],
                        },
                    ],
                    "ip_assignment_spec": {
                        "resource_type": "StaticIpPoolSpec",
                        "ip_pool_id": ip_pool_path,
                    },
                    "uplinks": [
                        {"uplink_name": "uplink1", "vds_uplink_name": "uplink1"},
                        {"uplink_name": "uplink2", "vds_uplink_name": "uplink2"},
                    ],
                }
            ],
        },
    }

    try:
        # Use the Fabric API for Transport Node Profile
        response = nsx.put(
            url=f"/policy/api/v1/infra/host-transport-node-profiles/{display_name}",
            json=payload,
        )
        return response.safejson()
    except Exception as e:
        if "already exists" in str(e).lower() or "duplicate" in str(e).lower():
            print(
                f"Transport Node Profile '{display_name}' already exists, attempting to find existing one"
            )
            existing_tnp = get_existing_transport_node_profile(nsx, display_name)
            if existing_tnp:
                return existing_tnp
        raise e


def get_compute_collection(nsx: NsxClient) -> Dict[str, Any]:
    """Get Compute Collection (Cluster)"""
    cluster_name = "Cluster"

    response = nsx.get(
        url=f"/api/v1/fabric/compute-collections?display_name={cluster_name}"
    )
    collections = response.safejson()

    if not collections.get("results"):
        raise ValueError(f"Compute Collection '{cluster_name}' not found")

    return collections["results"][0]


def get_existing_host_transport_node_collection(
    nsx: NsxClient, display_name: str
) -> Dict[str, Any] | None:
    """Check if a host transport node collection already exists with the given display name"""
    try:
        response = nsx.get(url="/api/v1/transport-node-collections")
        htncs = response.safejson()

        for htnc in htncs.get("results", []):
            if htnc.get("display_name") == display_name:
                print(f"Found existing Host Transport Node Collection: {display_name}")
                return htnc

        return None
    except Exception as e:
        print(f"Error checking for existing host transport node collections: {e}")
        return None


def create_host_transport_node_collection(
    nsx: NsxClient, compute_collection_id: str, tnp_path: str
) -> Dict[str, Any]:
    """Create Host Transport Node Collection or return existing one"""
    display_name = "Host transport node collection"

    # Check if host transport node collection already exists
    existing_htnc = get_existing_host_transport_node_collection(nsx, display_name)
    if existing_htnc:
        print(
            f"Host Transport Node Collection '{display_name}' already exists, using existing one"
        )
        return existing_htnc

    payload = {
        "resource_type": "HostTransportNodeCollection",
        "display_name": display_name,
        "compute_collection_id": compute_collection_id,
        "transport_node_profile_id": tnp_path,
    }

    try:
        response = nsx.post(
            url="/api/v1/transport-node-collections",
            json=payload,
        )
        return response.safejson()
    except Exception as e:
        if "already exists" in str(e).lower() or "duplicate" in str(e).lower():
            print(
                f"Host Transport Node Collection '{display_name}' already exists, attempting to find existing one"
            )
            existing_htnc = get_existing_host_transport_node_collection(
                nsx, display_name
            )
            if existing_htnc:
                return existing_htnc
        raise e


def wait_for_htnc_realization(
    nsx: NsxClient, htnc_path: str, timeout: int = 300
) -> None:
    """Wait for Host Transport Node Collection to be realized"""
    start_time = time.time()
    while time.time() - start_time < timeout:
        # Use Policy API realized state endpoint with proper path format
        # Extract ID from path and construct realized path
        htnc_id = htnc_path.split("/")[-1] if "/" in htnc_path else htnc_path
        realized_path = f"/infra/realized-state/enforcement-points/default/transport-node-collections/{htnc_id}"
        response = nsx.get(
            url=f"/policy/api/v1/infra/realized-state/realized-entity?realized_path={realized_path}"
        )
        state = response.safejson()

        if state.get("state") == "REALIZED":
            print(f"Host Transport Node Collection {htnc_path} realized successfully")
            return

        time.sleep(10)

    raise TimeoutError(
        f"Timeout: Host Transport Node Collection {htnc_path} not realized after {timeout}s"
    )


def verify_nsx_configuration_status(nsx: NsxClient, compute_collection_id: str) -> None:
    """Wait for all hosts to be prepared and verify NSX-T configuration status"""
    print("Verifying NSX-T configuration status...")
    print(f"Compute Collection ID: {compute_collection_id}")

    max_attempts = 60  # 30 attempts = 10 minutes
    attempt = 0

    while attempt < max_attempts:
        attempt += 1
        print(f"Attempt {attempt}/{max_attempts} - Checking transport nodes status...")

        try:
            tn_response = nsx.get(url="/api/v1/transport-nodes")
            transport_nodes = tn_response.safejson()

            # Filter transport nodes for this compute collection
            relevant_nodes = []
            for node in transport_nodes.get("results", []):
                # Check both direct compute_collection_id and in node_deployment_info
                node_cc_id = node.get("compute_collection_id")
                if not node_cc_id and "node_deployment_info" in node:
                    node_cc_id = node["node_deployment_info"].get(
                        "compute_collection_id"
                    )

                if node_cc_id == compute_collection_id:
                    relevant_nodes.append(node)

            if not relevant_nodes:
                print("No transport nodes found for this compute collection yet...")
                if attempt < max_attempts:
                    print("Waiting 10 seconds before retry...")
                    time.sleep(10)
                    continue
                else:
                    print("ERROR: No transport nodes found after maximum attempts")
                    raise Exception("No transport nodes found for compute collection")

            print(
                f"Found {len(relevant_nodes)} transport node(s) for this compute collection:"
            )

            all_ready = True
            failed_nodes = []
            preparing_hosts = []

            for node in relevant_nodes:
                node_id = node.get("id", "Unknown")
                display_name = node.get("display_name", "Unknown")

                # Get detailed status for each node
                try:
                    status_response = nsx.get(
                        url=f"/api/v1/transport-nodes/{node_id}/status"
                    )
                    status_data = status_response.safejson()

                except Exception as e:
                    print(f"ERROR: Failed to get status for node {node_id}: {e}")
                    failed_nodes.append(display_name)
                    preparing_hosts.append(display_name)
                    all_ready = False

                print(f"Transport Node: {display_name} (ID: {node_id})")
                # Display status information
                if "status" in status_data:
                    status = status_data["status"]
                    print(f"  Status: {status}")

                    # Check host node deployment status for more details
                    if (
                        "node_status" in status_data
                        and "host_node_deployment_status" in status_data["node_status"]
                    ):
                        deployment_status = status_data["node_status"][
                            "host_node_deployment_status"
                        ]
                        print(f"  Deployment Status: {deployment_status}")

                        # Host is ready only if both deployment_status is INSTALL_SUCCESSFUL AND status is UP
                        if deployment_status == "INSTALL_SUCCESSFUL" and status == "UP":
                            print(f"  OK: {display_name} is ready")
                        elif (
                            deployment_status == "INSTALL_SUCCESSFUL" and status != "UP"
                        ):
                            print(
                                f"  WAITING: {display_name} deployment successful but status is {status}"
                            )
                            preparing_hosts.append(display_name)
                            all_ready = False
                        elif (
                            "ERROR" in deployment_status or "FAIL" in deployment_status
                        ):
                            print(
                                f"  ERROR: {display_name} deployment failed - {deployment_status}"
                            )
                            failed_nodes.append(display_name)
                            all_ready = False
                        else:
                            print(
                                f"  WAITING: {display_name} deployment status: {deployment_status}, status: {status}"
                            )
                            preparing_hosts.append(display_name)
                            all_ready = False
                    else:
                        # Fallback to general status if deployment status not available
                        if status == "UP":
                            print(f"  OK: {display_name} is ready")
                        elif status == "DOWN":
                            print(f"  ERROR: {display_name} is down")
                            failed_nodes.append(display_name)
                            all_ready = False
                        elif status == "DEGRADED":
                            print(f"  WARNING: {display_name} is degraded")
                            preparing_hosts.append(display_name)
                            all_ready = False
                        elif status == "UNKNOWN":
                            print(f"  WAITING: {display_name} status unknown")
                            preparing_hosts.append(display_name)
                            all_ready = False
                        else:
                            print(f"  WAITING: {display_name} status: {status}")
                            preparing_hosts.append(display_name)
                            all_ready = False

                if "host_switch_spec" in status_data:
                    print(f"  Host Switch Spec: Available")

                if "transport_zone_endpoints" in status_data:
                    tz_count = len(status_data["transport_zone_endpoints"])
                    print(f"  Transport Zones: {tz_count}")

                # Check for any errors or warnings
                if "errors" in status_data and status_data["errors"]:
                    print(f"  Errors: {status_data['errors']}")

                if "warnings" in status_data and status_data["warnings"]:
                    print(f"  Warnings: {status_data['warnings']}")

            # Check if any nodes failed
            if failed_nodes:
                print(f"ERROR: The following hosts failed to prepare:")
                for failed_node in failed_nodes:
                    print(f"  - {failed_node}")
                raise Exception(
                    f"Host preparation failed for: {', '.join(failed_nodes)}"
                )

            # Check if all nodes are ready
            if all_ready:
                print(f"SUCCESS: All {len(relevant_nodes)} transport nodes are ready!")
                print("All hosts have been successfully prepared as transport nodes.")
                return
            else:
                print(f"WAITING: {len(preparing_hosts)} host(s) still preparing:")
                for host in preparing_hosts:
                    print(f"  - {host}")

                if attempt < max_attempts:
                    print("Waiting 10 seconds before retry...")
                    time.sleep(10)
                else:
                    print("ERROR: Timeout waiting for host preparation")
                    raise Exception("Timeout waiting for host preparation")

        except Exception as e:
            if "failed" in str(e).lower() or "error" in str(e).lower():
                print(f"ERROR: {e}")
                raise e
            else:
                print(f"ERROR getting transport nodes: {e}")
                if attempt < max_attempts:
                    print("Waiting 10 seconds before retry...")
                    time.sleep(10)
                else:
                    raise e


def get_ssl_thumbprint(hostname: str, port: int = 443) -> str:
    """Get SSL certificate thumbprint from hostname:port"""

    # Create SSL context
    context = ssl.create_default_context()
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE

    # Connect to the host and get certificate
    with ssl.create_connection((hostname, port), timeout=10) as sock:
        with context.wrap_socket(sock, server_hostname=hostname) as ssock:
            cert = ssock.getpeercert(binary_form=True)

    # Get certificate thumbprint (SHA-256)

    thumbprint = hashlib.sha256(cert).hexdigest()

    # Format as colon-separated pairs (VMware format)
    formatted_thumbprint = ":".join(
        thumbprint[i : i + 2].upper() for i in range(0, len(thumbprint), 2)
    )

    print(f"Retrieved SSL thumbprint for {hostname}: {formatted_thumbprint}")
    return formatted_thumbprint
