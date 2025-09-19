import os

from zpodcommon import models as M
from zpodengine.lib import database
from zpodengine.lib.commands import cmd_execute


def execute_config_script(
    zpod_component_id: int,
) -> None:
    """
    Sample config script

    Args:
        zpod_component_id: The zPod component ID
    """

    with database.get_session_ctx() as session:
        zpod_component = session.get(M.ZpodComponent, zpod_component_id)
        zpod = zpod_component.zpod

        # Fetch list of esxi components attached to this zpod
        zpod_esxi_list = [
            cur_component.fqdn
            for cur_component in zpod.components
            if cur_component.component.component_name == "esxi"
        ]

        # Configure vcsa component
        current_dir = os.path.dirname(os.path.abspath(__file__))
        cmd = (
            f"{current_dir}/vcsa_vds.ps1"
            f" -zPodHostname {zpod_component.fqdn}"
            f" -zPodUsername administrator@{zpod.domain}"
            f" -zPodPassword {zpod.password}"
            f" -zPodESXiList {','.join(zpod_esxi_list)}"
        )
        cmd_execute(f'pwsh -c "& {cmd}"').check_returncode()
