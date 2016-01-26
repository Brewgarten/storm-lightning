import fnmatch
import logging
import re
import socket
import time

from libcloud.common.types import LibcloudError
from libcloud.compute.base import NodeDriver, Node, NodeImage, NodeSize, StorageVolume, NodeLocation
from libcloud.compute.base import NodeState
from libcloud.compute.providers import set_driver

import storm.lightning.manager


log = logging.getLogger(__name__)



DEFAULT_CPU_SIZE = 2
DEFAULT_RAM_SIZE = 2048
DEFAULT_DISK_SIZE = 10

class LocalNodeDriver(NodeDriver):
    """
    Local node driver using ``libvirt`` to manage ``KVM`` virtual machines

    """
    type = "local"
    name = "LocalNodeDriver"
    features = {"create_node": ["generates_password", "password", "ssh_key"]}

    def __init__(self):
        super(LocalNodeDriver, self).__init__("local")
        self.manager = storm.lightning.manager.LibVirtManager()

    def attach_volume(self, node, volume, device=None):
        """
        Attache volume to node

        :param node: node
        :type node: :class:`~libcloud.compute.base.Node`
        :param volume: volume
        :type volume: :class:`~libcloud.compute.base.StorageVolume`
        :param device: where the device is exposed, e.g. '/dev/sdb'
        :type device: str
        :rytpe: ``bool``
        """
        storageVolume = self.manager.getStorageVolumeByPath(volume.id)
        self.manager.addDisk(node.name, storageVolume=storageVolume, device=device)
        return True

    def create_node(self, **kwargs):
        """
        Create a new node instance. This instance will be started
        automatically.

        :param auth: initial authentication the node
        :type auth: :class:`~libcloud.compute.base.NodeAuthSSHKey` or :class:`~libcloud.compute.base.NodeAuthPassword`
        :param image: image/template to be used for the node
        :type image: :class:`~libcloud.compute.base.NodeSize`
        :param location: location
        :type location: :class:`~libcloud.compute.base.NodeLocation`
        :param name: node name
        :type name: str
        :param size: size
        :type size: :class:`~libcloud.compute.base.NodeSize`
        :param subnetPrefix: subnet prefix, e.g., ``10.76``
        :type subnetPrefix: str
        :param replace: replace component with same name
        :type replace: bool
        :returns: :class:`~libcloud.compute.base.Node`
        """
        if "name" not in kwargs:
            raise ValueError("name needs to be specified")
        name = kwargs["name"]
        # TODO: implement resizing templates
#         if "size" not in kwargs:
#             raise ValueError("size needs to be specified")
        if "image" not in kwargs:
            raise ValueError("image needs to be specified")
        image = kwargs["image"]

        if "subnetPrefix" not in kwargs:
            raise ValueError("subnetPrefix needs to be specified")
        subnetPrefix = kwargs["subnetPrefix"]

        if "location" in kwargs:
            log.warn("This driver currently does not support different locations")
        if "size" in kwargs:
            log.warn("This driver currently does not support different locations")

        log.debug("creating '%s' using '%s' in subnet '%s'", name, image.name, subnetPrefix)

        if "auth" in kwargs:
            log.debug("using authentication")
        replace = False
        if "replace" in kwargs:
            replace = kwargs["replace"]

        networkName = "net{0}".format(subnetPrefix)
        poolName = "pool{0}".format(subnetPrefix)

        self.manager.createNetwork(networkName, subnetPrefix=subnetPrefix, replace=replace)
        self.manager.createStoragePool(poolName, [], [], replace=replace)
        self.manager.createGuestUsingTemplate(name, poolName, networkName, image.name)

        # TODO: allow adding disks here or only in separate attach_volume method?

        # start node automatically unless specified otherwise
        if kwargs.get("start", True):
            self.ex_start_node(name)
        return self._guest_info_to_node(self.manager.getGuestInfoByName(name))

    def create_volume(self, size, name, location=None, snapshot=None):
        """
        Create a new volume

        :param size: size of volume in gigabytes
        :type size: int
        :param name: fully qualified '<poolname>/<name>' name of the volume to be created
        :type name: str
        :param location: location (ignored by driver)
        :param snapshot:  snapshot (ignored by driver)

        :returns: :class:`~libcloud.compute.base.StorageVolume`
        """
        names = name.split("/")
        if len(names) != 2:
            log.error("name needs to be fully qualified '<poolname>/<name>'")
            return None

        sizeInBytes = size * 1024 * 1024 * 1024
        storageVolume = self.manager.createStorageVolume(names[0], names[1], capacity=sizeInBytes)
        return StorageVolume(storageVolume.key(),
                             storageVolume.name(),
                             size,
                             self,
                             extra={"capacity" : sizeInBytes})

    def destroy_node(self, node):
        """
        Destroy a node.

        :param node: node
        :type node: :class:`~libcloud.compute.base.Node`
        :returns: ``True`` if the destroy was successful, ``False`` otherwise.
        """
        self.manager.deleteGuest(node.name)
        return True

    def destroy_volume(self, volume):
        """
        Destroy a storage volume.

        :param volume: volume
        :type volume: :class:`~libcloud.compute.base.StorageVolume`
        :returns: ``True`` if the destroy was successful, ``False`` otherwise.
        """
        if volume is None:
            return False
        names = volume.id.split("/")
        if len(names) < 2:
            log.error("name needs to be fully qualified '<poolname>/<name>'")
            return False
        self.manager.deleteStorageVolume(names[-2], names[-1])
        return True

    def _guest_info_to_node(self, guestInfo):
        """
        Convert guest info into a libcloud Node

        :param guestInfo: guest info
        :type guestInfo: :class:`~virtinst.Guest`
        :returns: node
        :rtype: :class:`~libcloud.compute.base.Node`
        """
        state = NodeState.STOPPED

        publicIps = []
        networks = self.manager.getNetworkInfosByGuest(guestInfo.name)
        if networks:
            staticHost = self.manager.getStaticHostByName(networks[0].name, guestInfo.name)
            if staticHost:
                publicIps.append(staticHost.ip)

                sshSocket = socket.socket()
                sshSocket.settimeout(0.1)
                try:
                    sshSocket.connect((str(staticHost.ip), 22))
                    sshSocket.recv(256)
                    sshSocket.close()
                    state = NodeState.RUNNING
                except:
                    pass

        # TODO: size, images, and extra information
        disks = []
        for device in guestInfo.get_all_devices():
            if hasattr(device, "is_disk") and device.is_disk():
                storageVolume = self.manager.getStorageVolumeInfoByPath(device.path)
                if storageVolume:
                    disks.append(storageVolume.capacity / (1024 ** 3))

        extra={"password": "password",
               "disks" : { 'num': len(disks), 'details':disks } }
        return Node(guestInfo.uuid,
                    guestInfo.name,
                    state,
                    publicIps,
                    [],
                    self,
                    extra=extra)

    def ex_create_image_from_iso(self, name, iso, ks, cpus=2, memory=2048, capacity=None, replace=None):
        """
        Create a node from an iso and kickstart file

        :param name: name of the node
        :type name: str
        :param iso: path to iso
        :type iso: str
        :param ks: path to kickstart file
        :type ks: str
        :param capacity: capacity of OS disk
        :type capacity: int

        :returns: :class:`~libcloud.compute.base.NodeImage`
        """
        self.manager.createTemplate(name, iso, ks, cpus=cpus, memory=memory, capacity=capacity, replace=replace)
        return self.ex_get_image_by_name(name)

    def ex_destroy_network(self, netName):
        """
        Destroy and undefine a network

        :param netName: name of the network
        :type netName: str
        """
        self.manager.deleteNetwork(netName)

    def ex_destroy_storage_pool(self, poolName):
        """
        Destroy and undefine a storage pool

        :param poolName: name of the pool
        :type poolName: str
        """
        self.manager.deleteStoragePool(poolName)

    def ex_get_image_by_name(self, name):
        """
        Get image by name

        :param name: name
        :type name: str
        :returns: :class:`~libcloud.compute.base.NodeImage`
        """
        guestInfo = self.manager.getGuestInfoByName(name)
        if guestInfo:
            return NodeImage(guestInfo.uuid,
                        guestInfo.name,
                        self)
        return None

    def ex_get_node_by_name(self, name):
        """
        Get node by name

        :param name: name
        :type name: str
        :returns: :class:`~libcloud.compute.base.Node`
        """
        guestInfo = self.manager.getGuestInfoByName(name)
        if guestInfo:
            return self._guest_info_to_node(guestInfo)
        return None

    def ex_get_volume_by_name(self, name):
        """
        Get volume by name

        :param name: fully qualified '<poolname>/<name>' name of the volume
        :type name: str
        :returns: :class:`~libcloud.compute.base.StorageVolume`
        """
        storageVolume = self.manager.getStorageVolumeInfoByPath("/tmp/{0}".format(name))
        if storageVolume:
            sizeInGB = storageVolume.capacity/(1024*1024*1024)
            return StorageVolume(storageVolume.key,
                                storageVolume.name,
                                sizeInGB,
                                self,
                                extra={"capacity" : storageVolume.capacity})
        return None

    def ex_start_node(self, node, wait=True):
        """
        Start the node

        :param node: node
        :type node: :class:`~libcloud.compute.base.Node`
        :param wait: wait for the node to finish starting
        :type wait: bool
        """
        guest = self.manager.getGuestByName(node.name)
        guest.create()
        if wait:
            self.ex_wait_for_ready(node)

    def ex_wait_for_ready(self, node, timeout=600):
        """
        Wait until the node is ready, that is its network interface is up

        :param node: node
        :type node: :class:`~libcloud.compute.base.Node`
        :param timeout: timeout
        :type timeout: int
        """
        if node:
            sshSocket = socket.socket()
            sshSocket.settimeout(1)
            start = time.time()
            end = start + timeout
            while time.time() < end:

                try:
                    sshSocket.connect((str(node.public_ips[0]), 22))
                    sshSocket.recv(256)
                    sshSocket.close()
                    break
                except:
                    time.sleep(1)

            if time.time() > end:
                raise LibcloudError(
                        value="Could not start node {0}, timed out after {1} seconds".format(node.name, timeout),
                        driver=self)

    def list_images(self, location=None, listPublic=False, details=False):
        """
        Get a list of images

        :param location: location (ignored by driver)
        :param bool listPublic: (ignored by driver)
        :returns: [:class:`~libcloud.compute.base.NodeImage`]
        """
        images = []
        imageId = 1
        guests = sorted(self.manager.getGuestInfos(), key=lambda guest: guest.name)
        for guest in guests:
            networks = self.manager.getNetworkInfosByGuest(guest.name)
            # for now only check first network
            if networks[0].name == "templates":
                # TODO: extra information
                disks = []
                if details:
                    for device in guest.get_all_devices():
                        if hasattr(device, "is_disk") and device.is_disk():
                            storageVolume = self.manager.getStorageVolumeInfoByPath(device.path)
                            if storageVolume:
                                disks.append(storageVolume.capacity / (1024 ** 3))

                extra={"guid": guest.uuid,
                       "disks" : { 'num': len(disks), 'details':disks } }
                images.append(NodeImage(imageId,
                                  guest.name,
                                  self,
                                  extra=extra))
                imageId += 1
        return images

    def list_locations(self):
        """
        List data centers for a provider

        :return: list of node location objects
        :rtype: ``list`` of :class:`.NodeLocation`
        """
        return [NodeLocation(1, "local", "US", self)]

    def list_nodes(self):
        """
        Get a list of nodes

        :returns: [:class:`~libcloud.compute.base.Node`]
        """
        nodes = []
        for guestInfo in self.manager.getGuestInfos():
            networks = self.manager.getNetworkInfosByGuest(guestInfo.name)
            # for now only check first network and for specific naming convention
            if re.match("^net\d{1,3}\.\d{1,3}", networks[0].name):
                nodes.append(self._guest_info_to_node(guestInfo))
        nodes = sorted(nodes, key=lambda node: node.name)
        return nodes

    def ex_list_nodes(self, hostname=None, domain=None):
        """
        Get a list of nodes, optionally filtered by hostname

        ..note::
            The domain filtering is not supported for local

        :param hostname: A hostname or pattern to filter list of nodes
        :type hostname: str
        :param domain: Unused
        :type domain: str
        :returns: [:class:`~libcloud.compute.base.Node`]
        """
        return [
            node for node in self.list_nodes()
            if hostname is None or fnmatch.fnmatch(node.name, hostname)
        ]

    def list_sizes(self, location=None):
        """
        Get a list of sizes

        :param location: location (ignored by driver)
        :returns: [:class:`~libcloud.compute.base.NodeSize`]
        """
        sizes = []
        # TODO: define more sizes
        sizes.append(NodeSize(1, "1 CPU, 1GB ram, 10GB", 1024, 10, 1000, 0.0, self, {"cpus": 1}))
        sizes.append(NodeSize(2, "2 CPU, 2GB ram, 10GB", 2048, 10, 1000, 0.0, self, {"cpus": 2}))
        return sizes

    def list_volumes(self):
        """
        Get a list of volumes

        :returns: [:class:`~libcloud.compute.base.StorageVolume`]
        """
        volumes = []
        for storageVolume in self.manager.getStorageVolumeInfos():
            # limit volumes to qcow2 for now
            if storageVolume.format == "qcow2":
                sizeInGB = storageVolume.capacity/(1024*1024*1024)
                volumes.append(StorageVolume(storageVolume.key,
                                             storageVolume.name,
                                             sizeInGB,
                                             self,
                                             extra={"capacity" : storageVolume.capacity}))
        return volumes

set_driver(LocalNodeDriver.type, LocalNodeDriver.__module__, LocalNodeDriver.name)
