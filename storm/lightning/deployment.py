"""
This library provides basic deployment functionality for virtual machines using ``libguestfs`` and ``libvirt``.

"""

import re


class DeploymentAction(object):
    """
    A deployment action consisting of:

    1. deployment step where file are added/modified/deleted on storage volumes
    2. execution step where scripts and/or commands are run
    """
    def deploy(self, guestfsManager):
        """
        Deploy items on storage volumes of the virtual machine

        :param guestfsManager: guestfs manager
        :type guestfsManager: :class:`~storm.lightning.manager.LibGuestManager`
        """
        pass

    def execute(self, guestName):
        """
        Execute commands on the virtual machine

        :param guestName: guest name
        :type guestName: str
        :param guestfsManager: guestfs manager
        :type guestfsManager: :class:`~storm.lightning.manager.LibGuestManager`
        """
        pass

    def run(self, guestName, guestfsManager):
        """
        Deploy items on storage volumes of the virtual machine and
        execute commands on the virtual machine

        :param guestName: guest name
        :type guestName: str
        :param guestfsManager: guestfs manager
        :type guestfsManager: :class:`~storm.lightning.manager.LibGuestManager`
        """
        self.deploy(guestfsManager)
        self.execute(guestName)

class ChangeHostname(DeploymentAction):
    """
    Change the host name of the guest

    :param newHostname: new host name
    :type newHostname: str
    :param newMAC: new MAC address
    :type newMAC: str
    """
    def __init__(self, newHostname, newMAC):
        super(ChangeHostname, self).__init__()
        self.newHostname = newHostname
        self.newMAC = newMAC

    def deploy(self, guestfsManager):
        # adjust hostname
        network = guestfsManager.read("/etc/sysconfig/network")
        network = re.sub("HOSTNAME=(.+)", "HOSTNAME={0}".format(self.newHostname), network)
        guestfsManager.write("/etc/sysconfig/network", network)

        # adjust network interface
        interface = guestfsManager.read("/etc/sysconfig/network-scripts/ifcfg-eth0")
        interface = re.sub("DHCP_HOSTNAME=(.+)", "DHCP_HOSTNAME={0}".format(self.newHostname), interface)
        # TODO: check if necessary
        # newUUID = str(uuid.uuid4())
        # interface = re.sub("UUID=(.+)", "UUID={0}".format(newUUID), interface)
        interface = re.sub("UUID=(.+)\n", "", interface)
        interface = re.sub("HWADDR=(.+)", "HWADDR={0}".format(self.newMAC), interface)
        guestfsManager.write("/etc/sysconfig/network-scripts/ifcfg-eth0", interface)
