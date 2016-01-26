#!/usr/bin/env python

"""
This library provides management functionality for virtual machines using ``libvirt``.

"""

import argparse
import datetime
import guestfs
import logging
import os
import re
import string
import sys
import time
import traceback
import uuid

log = logging.getLogger(__name__)

# TODO: check if still necessary for creating/installing guests
try:
    from gi.repository import Libosinfo as libosinfo
except ImportError:
    # add a dummy implementation of the libosinfo module
    import imp

    libosinfoModule = imp.new_module("gi.repository.Libosinfo")

    class OSList():

        def __init__(self):
            self.list = []

        def get_length(self):
            return len(self.list)

        def get_nth(self, n):
            return self.list[n]

    class DB(object):

        def get_os_list(self):
            log.debug("get_os_list")
            return OSList()

    class Loader(object):

        def get_db(self):
            log.debug("get_db")
            return DB()

        def process_default_path(self):
            pass

    libosinfoModule.OSList = OSList
    libosinfoModule.DB = DB
    libosinfoModule.Loader = Loader
    sys.modules["gi.repository.Libosinfo"] = libosinfoModule

    repositoryModule = imp.new_module("gi.repository")
    repositoryModule.Libosinfo = libosinfoModule
    sys.modules["gi.repository"] = repositoryModule

    giModule = imp.new_module("gi")
    giModule.repository = repositoryModule
    sys.modules["gi"] = giModule

import virtinst
import libvirt

import storm.lightning.deployment


GIGABYTE = 1024 ** 3

# disable error logging on libvirt
def libvirt_callback(ignore, err):
    if err[3] != libvirt.VIR_ERR_ERROR:
        # Don't log libvirt errors: global error handler will do that
        log.warn("Non-error from libvirt: '%s'", err[2])
libvirt.registerErrorHandler(f=libvirt_callback, ctx=None)

class LibGuestManager(object):
    """
    A utility class to work with virtual machine disks using ``libguestfs``

    :param path: path to a storage volume
    :type path: str
    :param libguestfsTrace: trace libguestfs calls, parameters and return values
    :type libguestfsTrace: bool
    """
    def __init__(self, path, libguestfsTrace=False):
        self.guestFS = guestfs.GuestFS(python_return_dict=True)
        if libguestfsTrace:
            self.guestFS.set_trace(1)

        self.guestFS.add_drive(path)

        start = datetime.datetime.utcnow()
        self.guestFS.launch()
        end = datetime.datetime.utcnow()
        log.debug("launching libguestfs took %s", end-start)

        lvmRoot = None
        for filesystem in self.guestFS.list_filesystems().keys():
            if filesystem.endswith("lv_root"):
                lvmRoot = filesystem
                break

        # mount first disk
        if lvmRoot:
            self.guestFS.mount(lvmRoot, "/")
        else:
            self.guestFS.mount("/dev/sda1", "/")


    def read(self, path):
        """
        Read contents of the file specified by the path

        :param path: file path
        :type path: str
        :returns: str
        """
        return self.guestFS.read_file(path)

    def write(self, path, content, append=False, mode=None):
        """
        Write content to the file specified by the path

        :param path: file path
        :type path: str
        :param content: content
        :type content: str
        :param append: append to file
        :type append: bool
        :param mode: permissions of the file
        :type mode: int
        """
        if append:
            self.guestFS.write_append(path, content)
        else:
            self.guestFS.write(path, content)
        if mode:
            self.guestFS.chmod(mode, path)

    def __del__(self):
        self.guestFS.close()

class LibVirtManager(object):
    """
    A utility class to manage virtual machine components using ``libvirt``

    :param uri: hypervisor uri
    :type uri: str
    """
    def __init__(self, uri="qemu:///system"):
        self.connection = virtinst.cli.getConnection(uri)

    def addDisk(self, name, storageVolume=None, device=None):
        """
        Add a disk to the specified guest

        :param name: name of the guest
        :type name: str
        :param storageVolume: storage volume
        :type storageVolume: :class:`~libvirt.virStorageVol`
        :param device: target device, e.g., ``sdb``
        :type device: str
        """
        # TODO: instance checks for parameters
        if storageVolume is None or device is None:
            # get existing storage volumes for the guest
            storageVolumes = self.getStorageVolumesByGuest(name)
            storagePoolName = storageVolumes[0].storagePoolLookupByVolume().name()
            availableDevices = string.ascii_lowercase
            for storageVolume in storageVolumes:
                availableDevices = availableDevices.replace(storageVolume.name().split("-")[-1], "")

        if storageVolume is None:
            # create a new storage volume
            storageVolume = self.createStorageVolume(storagePoolName, "{0}-{1}".format(name, availableDevices[0]))

        if device is None:
            device = "sd{0}".format(availableDevices[0])
        # TODO: investigate listing of devices because Linux autosorts them

        # define disk interface
        disk = virtinst.VirtualDisk(self.connection)
        disk.type = disk.TYPE_FILE
        disk.driver_name = disk.DRIVER_QEMU
        disk.driver_type = "qcow2"
        disk.driver_cache = disk.CACHE_MODE_NONE
        disk.path = storageVolume.path()
        disk.target = device.replace("/dev/", "")
        disk.bus = "virtio"

        # attach disk to guest
        guest = self.getGuestByName(name)
        guest.attachDeviceFlags(disk.get_xml_config(), libvirt.VIR_DOMAIN_AFFECT_CONFIG)

    def cloneGuest(self, name, cloneName, replace=False, newPoolName=None, newNetworkName=None):
        """
        Clone guest

        :param name: name of the original guest
        :type name: str
        :param cloneName: name of the clone guest
        :type cloneName: str
        """
        try:
            if not os.geteuid() == 0:
                raise RuntimeError("Cloning must be performed as root")

            cloner = virtinst.Cloner(self.connection)
            cloner.replace = replace
            cloner.original_guest = name
            cloner.clone_name = cloneName

            cloner.setup_original()

            # gather paths for clone disks
            cloneDiskPaths = []
            for originalDisk in cloner.original_disks:

                originalDiskPathElements = originalDisk.path.split("/")
                originalDiskPathElements[-1] = originalDiskPathElements[-1].replace(cloner.original_guest, cloner.clone_name)
                cloneDiskPath = "/".join(originalDiskPathElements)

                # check if a new storage pool was specified
                if newPoolName:
                    oldStoragePoolName = self.getStorageVolumeByPath(originalDisk.path).storagePoolLookupByVolume().name()
                    # TODO: make sure this simple replacement works in all cases
                    cloneDiskPath = "{0}/{1}".format("/".join(originalDiskPathElements[:-1]).replace(oldStoragePoolName, newPoolName),
                                                     originalDiskPathElements[-1])

                # check if clone disk already exists
                cloneDisk = self.getStorageVolumeByPath(cloneDiskPath)
                if cloneDisk:
                    log.warn("removing existing storage volume '%s'", cloneDiskPath)
                    cloneDisk.delete()
                cloneDiskPaths.append(cloneDiskPath)

            cloner.clone_paths = cloneDiskPaths

            # make sure defined clone disks are qcow2
            for cloneDisk in cloner.clone_disks:
                cloneDisk.driver_type = "qcow2"

            # monkey patch create storage method to make sure that cloned disk storage volumes are 'qcow2'
            from virtinst.devicedisk import VirtualDisk
            oldSet_create_storage = VirtualDisk.set_create_storage
            def set_create_storage(self, size=None, sparse=True,
                           fmt="qcow2", vol_install=None,
                           clone_path=None, backing_store=None,
                           fake=False):
                oldSet_create_storage(self, size, sparse, fmt, vol_install, clone_path, backing_store, fake)
            VirtualDisk.set_create_storage = set_create_storage

            cloner.setup_clone()
            log.debug(cloner.clone_xml)

            start = datetime.datetime.utcnow()

            # clone guest and its storage volumes
            cloner.start_duplicate()

            # adjust permissions of the storage volumes
            for cloneDiskPath in cloneDiskPaths:
                os.chmod(cloneDiskPath, 0777)

            # adjust hostname and network interface for clone
            clonedGuest = virtinst.Guest(self.connection, parsexml=cloner.clone_xml)
            networkInterfaces = clonedGuest.get_devices(virtinst.VirtualDevice.VIRTUAL_DEV_NET)
            if networkInterfaces:

                # check if new network was specified
                if newNetworkName:
                    networkInterfaces[0].source = newNetworkName
                    clonedGuestDomain = self.getGuestByName(cloneName)
                    clonedGuestDomain.updateDeviceFlags(networkInterfaces[0].get_xml_config(), libvirt.VIR_DOMAIN_AFFECT_CONFIG)

                osDiskPath = None
                for cloneDiskPath in cloneDiskPaths:
                    if cloneDiskPath.endswith("-a"):
                        osDiskPath = cloneDiskPath
                        break

                if osDiskPath:
                    guestfsManager = LibGuestManager(osDiskPath)
                    storm.lightning.deployment.ChangeHostname(cloneName, networkInterfaces[0].macaddr).deploy(guestfsManager)
                    del guestfsManager
                else:
                    log.error("could not determine OS disk for '%s' from paths '%s'", cloneName, ",".join(cloneDiskPaths))

                # add static host entry to network
                self.createStaticHost(networkInterfaces[0].source, networkInterfaces[0].macaddr, cloneName)

            end = datetime.datetime.utcnow()
            log.info("cloning '%s' to '%s' took %s",
                         cloner.original_guest, cloner.clone_name, end-start)

        except Exception as e:
            log.error(e)
            log.error(traceback.format_exc())

    # TODO: define Units enum
    def createGuest(self, name, poolName, networkName, iso, ks_file, memory=2048, cpus=2, replace=False):
        """
        Create a new guest using the specified storage pool and network names

        :param name: name of the new guest
        :type name: str
        :param poolName: name of an existing storage pool for this guest
        :type poolName: str
        :param networkName: name of an existing network for this guest
        :type networkName: str
        :param iso: path to the iso
        :type iso: str
        :param ks_file: path to the kickstart file
        :type ks_file: str
        :param memory: memory in MB
        :type memory: int
        :param cpus: number of cpus
        :type cpus: int
        :param replace: whether to replace an existing vm with the same name
        :type replace: bool
        :returns: :class:`~libvirt.virDomain`
        """
        guest = virtinst.Guest(self.connection)
        guest.replace = replace
        guest.type = "kvm"
        guest.name = name
        # TODO: make configurable
        guest.memory = int(memory * 1024)
        guest.maxmemory = int(memory * 1024)
        guest.uuid = str(uuid.uuid4())
        guest.vcpus = cpus

        guest.os_variant = "linux"

        guest.os.arch = "x86_64"
        guest.os.os_type = "hvm"
        guest.clock.offset = "utc"

        guest.features.acpi = True
        guest.features.apic = True
        guest.features.pae = True

        # TODO: make more flexible
        guest.installer.location = iso
        guest.installer.initrd_injections = [ks_file]
        # guest.installer.extraargs = "ks=file:/ks_base.cfg text console=tty0 utf8 console=ttyS0,115200"
        guest.installer.extraargs = "ks=file:/ks_base.cfg"

        guest.on_poweroff = "destroy"
        guest.on_reboot = "reboot"
        guest.on_crash = "reboot"


        # get all disks in the storage pool
        storagePool = self.getStoragePoolByName(poolName)
        storageVolumes = storagePool.listAllVolumes()
        disks = []
        for storageVolume in storageVolumes:
            # storage volume assumes pattern: nodeName-device
            # e.g., node1-a
            info = storageVolume.name().split("-")
            storageVolumeNode = "-".join(info[:-1])
            storageVolumeDevice = info[-1]

            if storageVolumeNode == name:
                disk = virtinst.VirtualDisk(self.connection)
                disk.type = disk.TYPE_FILE
                disk.driver_name = disk.DRIVER_QEMU
                disk.driver_type = "qcow2"
                disk.driver_cache = disk.CACHE_MODE_NONE
                disk.path = storageVolume.path()
                disk.target = "sd{0}".format(storageVolumeDevice)
                disk.bus = "virtio"
                disks.append(disk)

        if not disks:
            log.error("no storage volumes found for '%s'", name)
            return

        # add disks in sorted order
        def path(disk):
            return disk.path
        disks.sort(key=path)
        for disk in disks:
            guest.add_device(disk)

        # add network interface
        interface = virtinst.VirtualNetworkInterface(self.connection)
        interface.type = interface.TYPE_VIRTUAL
        interface.source = networkName
        interface.target_dev = "vnet0"
        interface.model = "virtio"
        interface.macaddr = interface.generate_mac(self.connection)
        guest.add_device(interface)

        guest.add_default_devices()

        log.debug(guest.get_xml_config())
        guest.validate()

        start = datetime.datetime.utcnow()
        domain = guest.start_install(noboot=True)
        # give domain some time to start
        while domain.info()[0] != libvirt.VIR_DOMAIN_RUNNING:
            time.sleep(1)

        # check on the guest
        while True:
            info = domain.info()
            if info[0] == libvirt.VIR_DOMAIN_SHUTOFF:
                break
            elif info[0] != libvirt.VIR_DOMAIN_RUNNING:
                log.info("'{0}' not running anymore, state: {1}".format(name, info[0]))
                break
            else:
                time.sleep(1)

        # add static host entry to network
        self.createStaticHost(networkName, interface.macaddr, name)
        end = datetime.datetime.utcnow()
        log.info("creating '{0}' took {1}".format(name, end-start))
        return domain

    def createGuestUsingTemplate(self, name, poolName, networkName, template, cpus=2, replace=False):
        """
        Create a new guest using the specified storage pool and network names

        :param template: template name
        :type template: str
        :param name: name of the new guest
        :type name: str
        :param newPoolName: name of an existing storage pool for this guest
        :type newPoolName: str
        :param newNetworkName: name of an existing network for this guest
        :type newNetworkName: str
        :param cpus: number of cpus
        :type cpus: int
        :param replace: whether to replace an existing vm with the same name
        :type replace: bool
        """
        self.cloneGuest(template, name, replace=replace, newPoolName=poolName, newNetworkName=networkName)

    def createNetwork(self, name, subnetPrefix, replace=False, enableNAT=True):
        """
        Create a new network using the specified subnet prefix

        :param name: name of the new network
        :type name: str
        :param subnetPrefix: subnet prefix, e.g., ``10.76``
        :type subnetPrefix: str
        :param replace: replace existing network
        :type replace: bool
        :param enableNAT: enable NAT such that VM can access host network/Internet
        :type enableNAT: bool
        :returns: :class:`~libvirt.virNetwork`
        """
        try:
            network = virtinst.Network(self.connection)
            if replace:
                try:
                    self.deleteNetwork(name)
                except:
                    pass
            network.name = name
            network.bridge = name + "b0"
            network.stp = "on"
            network.delay = 0
            if enableNAT:
                network.forward.mode = "nat"
            # setup ip address
            ip = network.add_ip()
            ip.address = "{0}.0.254".format(subnetPrefix)
            ip.netmask = "255.255.255.0"
            # setup DHCP
            range = ip.add_range()
            range.start = "{0}.0.1".format(subnetPrefix)
            range.end = "{0}.0.250".format(subnetPrefix)
            ip.validate()
            network.validate()
            log.debug(network.get_xml_config())
            net = network.install()
            return net
        except (libvirt.libvirtError, ValueError) as e:
            log.debug(e)
        return None

    def createStaticHost(self, networkName, mac, hostname, ip=None):
        """
        Create static host for the specified host. Note that if no IP address
        is given one is chosen automatically from the networks DHCP range

        :param networkName: name of the network
        :type networkName: str
        :param mac: MAC address
        :type mac: str
        :param hostname: host name
        :type hostname: str
        :param ip: IP address
        :type ip: str
        :returns: assigned IP address
        """
        try:
            network = self.connection.networkLookupByName(networkName)

            networkInfo = virtinst.Network(self.connection, parsexml=network.XMLDesc())
            usedIps = set()
            for staticHost in networkInfo.ips[0].hosts:
                if staticHost.macaddr == mac:
                    log.error("'%s' already has entry with matching MAC: Hostname '%s' MAC '%s' and IP '%s'",
                                  networkName, staticHost.name, staticHost.macaddr, staticHost.ip)
                    return

                if staticHost.name == hostname:
                    log.error("'%s' already has entry with matching hostname: Hostname '%s' MAC '%s' and IP '%s'",
                                  networkName, staticHost.name, staticHost.macaddr, staticHost.ip)
                    return

                if ip and staticHost.ip == ip:
                    log.error("'%s' already has entry with matching IP: Hostname '%s' MAC '%s' and IP '%s'",
                                  networkName, staticHost.name, staticHost.macaddr, staticHost.ip)
                    return

                usedIps.add(staticHost.ip)

            if not ip:
                # find available ip address
                ipStart = networkInfo.ips[0].ranges[0].start
                ipEnd = networkInfo.ips[0].ranges[0].end
                ipStartInfo = ipStart.split(".")
                ipEndInfo = ipEnd.split(".")
                subnetPrefix = ".".join(ipStartInfo[:-1])

                for hostIp in range(int(ipStartInfo[-1]), int(ipEndInfo[-1])+1):
                    potentialIp = "{0}.{1}".format(subnetPrefix, hostIp)
                    if potentialIp not in usedIps:
                        ip = potentialIp
                        break

                if not ip:
                    log.error("no available ip found")
                    return

            host = virtinst.network._NetworkDHCPHost(self.connection)
            host.macaddr = mac
            host.name = hostname
            host.ip = ip
            network.update(libvirt.VIR_NETWORK_UPDATE_COMMAND_ADD_LAST,
                           libvirt.VIR_NETWORK_SECTION_IP_DHCP_HOST,
                           -1, host.get_xml_config())

        except libvirt.libvirtError as e:
            log.error(e)
        return ip

    def createStoragePool(self, name, nodes, disks, replace=False):
        """
        Create a new storage pool for the nodes with the specified number
        of disks

        :param name: name of the storage pool
        :type name: str
        :param nodes: list of node names
        :type nodes: [str]
        :param disks: list of disk sizes (in GB)
        :type disks: [int]
        :param replace: whether to replace a pre-existing pool
        :type replace: bool
        :returns: :class:`~libvirt.virStoragePool`
        """
        try:
            storagePool = virtinst.StoragePool(self.connection)
            if replace:
                try:
                    self.deleteStoragePool(name)
                except:
                    pass
            storagePool.type = storagePool.TYPE_DIR
            storagePool.name = name
            storagePool.target_path = "/tmp/{0}".format(name)
            storagePool.permissions.mode = "0777"
            # TODO: check if necessary
            # storagePool.permissions.owner = "500"
            # storagePool.permissions.group = "500"
            storagePool.validate()
            pool = storagePool.install(create=True, build=True, autostart=True)

            # add storage volumes for the nodes
            if isinstance(nodes, str):
                nodes = [nodes]
            for node in nodes:
                self.createStorageVolumes(name, node, disks)
            return pool
        except (libvirt.libvirtError, ValueError) as e:
            log.debug(e)
        return None

    def createStorageVolume(self, poolName, name, capacity=(10 * GIGABYTE)):
        """
        Create a new storage volume in the specified pool
        of disks

        :param poolName: name of the storage pool
        :type poolName: str
        :param name: name of the storage volume
        :type name: str
        :returns: :class:`~libvirt.virStorageVol`
        """
        try:
            storageVolume = virtinst.StorageVolume(self.connection)
            storageVolume.pool = self.getStoragePoolByName(poolName)
            storageVolume.name = name
            storageVolume.format = "qcow2"
            # TODO: make configurable
            storageVolume.allocation = int(capacity/10)
            storageVolume.capacity = int(capacity)
            storageVolume.permissions.mode = "0777"
            # TODO: check if necessary
            # storageVolume.permissions.owner = "500"
            # storageVolume.permissions.group = "500"
            storageVolume.validate()
            volume = storageVolume.install()
            return volume
        except (libvirt.libvirtError, ValueError) as e:
            log.error(e)
        return None

    def createStorageVolumes(self, poolName, node, disks):
        """
        Create multiple storage volumes in the specified pool of disks

        :param poolName: name of the storage pool
        :type poolName: str
        :param name: name of the storage volume
        :type name: str
        :param disks: list of disk sizes
        :type disks: [int]

        :returns: [:class:`~libvirt.virStorageVol] or `None`
        """
        storage_volumes = []
        for disk in range(len(disks)):
            disk_size = disks[disk] * GIGABYTE
            storage_volume = self.createStorageVolume(poolName, "{0}-{1}".format(node, string.ascii_lowercase[disk]),\
                                                            capacity=disk_size)
            if storage_volume is not None:
                storage_volumes.append(storage_volume)
        if storage_volumes:
            return storage_volumes
        else:
            return None

    def createTemplate(self, name, iso, kickstart, memory=2048, cpus=2, capacity=None, replace=None):
        """
        Create template using the specified iso and kickstart file

        :param name: name of the template
        :type name: str
        :param iso: path to the iso
        :type iso: str
        :param kickstart: path to the kickstart file
        :type kickstart: str
        :param capacity: size of OS disk
        :type capacity: int
        """
        storagePool = self.getStoragePoolByName("templates")
        if storagePool is None:
            self.createStoragePool("templates", [], [])

        if capacity:
            self.createStorageVolume("templates", "{0}-a".format(name), capacity=capacity)
        else:
            self.createStorageVolume("templates", "{0}-a".format(name))

        network = self.getNetworkByName("templates")
        if network is None:
            self.createNetwork("templates", "1.0")

        self.createGuest(name, "templates", "templates", iso, kickstart, replace=replace, cpus=cpus, memory=memory)

    def changeGuest(self, cpu=None, memory=None):
        raise NotImplementedError
        # TODO: implement cpu and memory changes
        # http://libvirt.org/guide/html/ch03s04s04.html
        log.fatal(self.connection.getInfo())

    def deleteGuest(self, name):
        """
        Delete guest and its associated storage volumes

        :param name: name of the guest
        :type name: str
        """
        try:
            guest = self.connection.lookupByName(name)
        except libvirt.libvirtError as e:
            log.error(e)
            return
        try:
            storageVolumes = self.getStorageVolumesByGuest(name)
        except libvirt.libvirtError as e:
            log.debug(e)
        try:
            networks = self.getNetworksByGuest(name)
        except libvirt.libvirtError as e:
            log.debug(e)

        if not guest.isActive():
            try:
                guest.create()
            except:
                guest.undefine()

        try:
            log.debug("Attempting to destroy {0}".format(guest.name()))
            guest.destroy()
        except libvirt.libvirtError as e:
            log.debug(e)
        try:
            guest.undefine()
        except libvirt.libvirtError as e:
            log.debug(e)

        try:
            if storageVolumes:
                storagePool = storageVolumes[0].storagePoolLookupByVolume()
                for storageVolume in storageVolumes:
                    storageVolume.delete()

                # check if storage pool is now empty and can be deleted
                if not storagePool.listAllVolumes():
                    if not storagePool.isActive():
                        storagePool.create()
                    storagePool.destroy()
                    storagePool.delete()
                    storagePool.undefine()
        except libvirt.libvirtError as e:
            log.debug(e)
        try:
            if networks:
                self.deleteStaticHost(networks[0].name(), name)
        except libvirt.libvirtError as e:
            log.debug(e)

    def deleteNetwork(self, name):
        """
        Delete network

        :param name: name of the network
        :type name: str
        """
        try:
            network = self.connection.networkLookupByName(name)

            if not network.isActive():
                network.create()

            network.destroy()
            network.undefine()
        except libvirt.libvirtError as e:
            if not re.search('not found', e.get_error_message()):
                log.error(e)

    def deleteStaticHost(self, networkName, hostname):
        """
        Delete static host for the host

        :param networkName: name of the network
        :type networkName: str
        :param hostname: host name
        :type hostname: str
        """
        try:
            network = self.connection.networkLookupByName(networkName)

            networkInfo = virtinst.Network(self.connection, parsexml=network.XMLDesc())
            host = None
            for staticHost in networkInfo.ips[0].hosts:
                if staticHost.name == hostname:
                    host = staticHost
                    break

            if not host:
                log.debug("static host '%s' does not exist in '%s'", hostname, networkName)
                return

            network.update(libvirt.VIR_NETWORK_UPDATE_COMMAND_DELETE,
                           libvirt.VIR_NETWORK_SECTION_IP_DHCP_HOST,
                           -1, host.get_xml_config())

        except libvirt.libvirtError as e:
            if not re.search('not found', e.get_error_message()):
                log.error(e)

    def deleteStoragePool(self, name):
        """
        Delete storage pool and its associated storage volumes

        :param name: name of the storage pool
        :type name: str
        """
        try:
            storagePool = self.connection.storagePoolLookupByName(name)

            if not storagePool.isActive():
                storagePool.create()

            storageVolumes = storagePool.listAllVolumes()
            for storageVolume in storageVolumes:
                storageVolume.delete()
            storagePool.destroy()
            storagePool.delete()
            storagePool.undefine()
        except libvirt.libvirtError as e:
            if not re.search('not found', e.get_error_message()):
                log.error(e)

    def deleteStorageVolume(self, poolName, name):
        """
        Delete storage volume in the specified pool

        :param poolName: name of the storage pool
        :type poolName: str
        :param name: name of the storage volume
        :type name: str
        """
        try:
            storagePool  = self.getStoragePoolByName(poolName)
            storageVolume = storagePool.storageVolLookupByName(name)
            storageVolume.delete()
        except libvirt.libvirtError as e:
            log.error(e)

    def getGuestInfos(self):
        """
        Return a list of all guest infos

        :returns: [:class:`~virtinst.Guest`]
        """
        try:
            self.connection._fetch_cache.clear()
            return self.connection.fetch_all_guests()
        except libvirt.libvirtError as e:
            if not re.search('not found', e.get_error_message()):
                log.error(e)
        return []

    def getGuestByName(self, name):
        """
        Get guest

        :param name: name of the guest
        :type name: str
        :returns: :class:`~libvirt.virDomain` or ``None``
        """
        try:
            return self.connection.lookupByName(name)
        except libvirt.libvirtError as e:
            if not re.search('not found', e.get_error_message()):
                log.error(e)
        return None

    def getGuestInfoByName(self, name):
        """
        Get guest info

        :param name: name of the guest
        :type name: str
        :returns: :class:`~virtinst.Guest` or ``None``
        """
        guest = self.getGuestByName(name)
        if guest:
            return virtinst.Guest(self.connection, parsexml=guest.XMLDesc())
        return None

    def getNetworkByName(self, name):
        """
        Get network

        :param name: name of the network
        :type name: str
        :returns: :class:`~libvirt.virNetwork` or ``None``
        """
        try:
            return self.connection.networkLookupByName(name)
        except libvirt.libvirtError as e:
            if not re.search('not found', e.get_error_message()):
                log.error(e)
        return None

    def getNetworksByGuest(self, name):
        """
        Get networks for the guest

        :param name: name of the guest
        :type name: str
        :returns: [:class:`~libvirt.virNetwork`]
        """
        networks = []
        try:
            guestInfo = self.getGuestInfoByName(name)
            if guestInfo:
                for device in guestInfo.get_devices(virtinst.VirtualDevice.VIRTUAL_DEV_NET):
                    network = self.getNetworkByName(device.source)
                    if network:
                        networks.append(network)

        except libvirt.libvirtError as e:
            if not re.search('not found', e.get_error_message()):
                log.error(e)
        return networks

    def getNetworkInfosByGuest(self, name):
        """
        Get networks info for the guest

        :param name: name of the guest
        :type name: str
        :returns: [:class:`~virtinst.Network`]
        """
        networksInfo = []
        networks = self.getNetworksByGuest(name)
        for network in networks:
            networksInfo.append(virtinst.Network(self.connection, parsexml=network.XMLDesc()))
        return networksInfo

    def getStaticHostByName(self, networkName, hostname):
        """
        Get static host

        :param networkName: name of the network
        :type networkName: str
        :param hostname: host name
        :type hostname: str
        :returns: :class:`~virtinst.network._NetworkDHCPHost` or ``None``
        """
        try:
            network = self.connection.networkLookupByName(networkName)
            networkInfo = virtinst.Network(self.connection, parsexml=network.XMLDesc())
            for staticHost in networkInfo.ips[0].hosts:
                if staticHost.name == hostname:
                    return staticHost
        except libvirt.libvirtError as e:
            if not re.search('not found', e.get_error_message()):
                log.error(e)
        return None

    def getStoragePoolByName(self, name):
        """
        Get storage pool

        :param name: name of the storage pool
        :type name: str
        :returns: :class:`~libvirt.virStoragePool` or ``None``
        """
        try:
            return self.connection.storagePoolLookupByName(name)
        except libvirt.libvirtError as e:
            if not re.search('not found', e.get_error_message()):
                log.error(e)
        return None

    def getStorageVolumeByPath(self, path):
        """
        Get storage volume

        :param path: path of the storage volume
        :type path: str
        :returns: :class:`~libvirt.virStorageVol` or ``None``
        """
        try:
            return self.connection.storageVolLookupByPath(path)
        except libvirt.libvirtError as e:
            if not re.search('not found', e.get_error_message()):
                log.error(e)
        return None

    def getStorageVolumeInfoByPath(self, path):
        """
        Get storage volume info

        :param path: path of the storage volume
        :type path: str
        :returns: :class:`~virtinst.StorageVolume` or ``None``
        """
        storageVolume = self.getStorageVolumeByPath(path)
        if storageVolume:
            return virtinst.StorageVolume(self.connection, parsexml=storageVolume.XMLDesc())
        return None

    def getStorageVolumesByGuest(self, name):
        """
        Get storage volumes for the guest

        :param name: name of the guest
        :type name: str
        :returns: [:class:`~libvirt.virStorageVol`]
        """
        storageVolumes = []
        try:
            domain = self.connection.lookupByName(name)

            xmlDesc = domain.XMLDesc()
            guest = virtinst.Guest(self.connection, parsexml=xmlDesc)

            for device in guest.get_devices(virtinst.VirtualDevice.VIRTUAL_DEV_DISK):

                if device.device == virtinst.VirtualDevice.VIRTUAL_DEV_DISK:
                    storageVolume = self.getStorageVolumeByPath(device.path)
                    if storageVolume:
                        storageVolumes.append(storageVolume)

        except libvirt.libvirtError as e:
            if not re.search('not found', e.get_error_message()):
                log.error(e)
        return storageVolumes

    def getStorageVolumeInfos(self):
        """
        Return a list of all storage volume infos

        :returns: [:class:`~virtinst.StorageVolume`]
        """
        try:
            self.connection._fetch_cache.clear()
            return self.connection.fetch_all_vols()
        except libvirt.libvirtError as e:
            if not re.search('not found', e.get_error_message()):
                log.error(e)
        return []

def main():
    """
    Main function for managing virtual machines using ``libvirt``
    """
    parser = argparse.ArgumentParser(description="A utility to help with and automate virtual machine setup and configuration")
    parser.add_argument("-v", "--verbose", action="store_true", help="display debug information")

    commandParser = parser.add_subparsers(dest="command")

    addParser = commandParser.add_parser("add", help="add help")
    addParser.add_argument("-v", "--verbose", action="store_true", help="display debug information")
    addTypeParser = addParser.add_subparsers(dest="type")

    addDiskParser = addTypeParser.add_parser("disk", help="disk")
    addDiskParser.add_argument('name', help="the guest name")
    addDiskParser.add_argument("-v", "--verbose", action="store_true", help="display debug information")

    createParser = commandParser.add_parser("create", help="create help")
    createParser.add_argument("-v", "--verbose", action="store_true", help="display debug information")
    createTypeParser = createParser.add_subparsers(dest="type")

    createGuestParser = createTypeParser.add_parser("guest", help="guest/domain")
    createGuestParser.add_argument('name', help="the guest name")
    createGuestParser.add_argument('pool', help="the storage pool name")
    createGuestParser.add_argument('network', help="the network name")
    createGuestParser.add_argument("--template", help="the template name")
    createGuestParser.add_argument("--iso", help="the path to an iso file")
    createGuestParser.add_argument("--kickstart", help="the path to kickstart file")
    createGuestParser.add_argument("-v", "--verbose", action="store_true", help="display debug information")

    createNetworkParser = createTypeParser.add_parser("network", help="network")
    createNetworkParser.add_argument('name', help="the network name")
    createNetworkParser.add_argument('subnet', help="the subnet prefix, e.g., 10.76")
    createNetworkParser.add_argument("-v", "--verbose", action="store_true", help="display debug information")

    createPoolParser = createTypeParser.add_parser("pool", help="storage pool")
    createPoolParser.add_argument('name', help="the pool name")
    createPoolParser.add_argument('nodes', nargs="+", help="list of nodes")
    createPoolParser.add_argument('-d', "--disks", default=1, type=int, action="store", help="number of disks")
    createPoolParser.add_argument("-v", "--verbose", action="store_true", help="display debug information")

    createTemplateParser = createTypeParser.add_parser("template", help="template")
    createTemplateParser.add_argument('name', help="the template name")
    createTemplateParser.add_argument('iso', help="the path to an iso")
    createTemplateParser.add_argument('kickstart', help="the path to a kickstart file")
    createTemplateParser.add_argument("-v", "--verbose", action="store_true", help="display debug information")

    createVolumeParser = createTypeParser.add_parser("volume", help="storage volume")

    cloneParser = commandParser.add_parser("clone", help="clone help")
    cloneParser.add_argument('name', help="the guest to clone")
    cloneParser.add_argument('cloneName', help="the clone to create")
    cloneParser.add_argument("-v", "--verbose", action="store_true", help="display debug information")

    deleteParser = commandParser.add_parser("delete", help="delete help")
    deleteTypeParser = deleteParser.add_subparsers(dest="type")

    deleteGuestParser = deleteTypeParser.add_parser("guest", help="guest/domain")
    deleteNetworkParser = deleteTypeParser.add_parser("network", help="network")
    deletePoolParser = deleteTypeParser.add_parser("pool", help="storage pool")
    deleteVolumeParser = deleteTypeParser.add_parser("volume", help="storage volume")

    deleteParser.add_argument('name', nargs="+", help="the name(s) to delete")
    deleteParser.add_argument("-v", "--verbose", action="store_true", help="display debug information")

    args = parser.parse_args()

    manager = LibVirtManager()

    if args.verbose:
        logging.root.setLevel(logging.DEBUG)
        log.setLevel(logging.DEBUG)

    if args.command == "add":

        if args.type == "disk":
            manager.addDisk(args.name)

        else:
            raise NotImplementedError("add %s" % args.type)

    elif args.command == "create":

        if args.type == "guest":

            if args.template:
                manager.createGuestUsingTemplate(args.name, args.pool, args.network, args.template)

            elif args.iso and args.kickstart:
                manager.createGuest(args.name, args.pool, args.network, args.iso, args.kickstart)

            else:
                parser.error("need to specify either a template or an iso and kickstart file")

        elif args.type == "network":
            manager.createNetwork(args.name, args.subnet)

        elif args.type == "pool":
            manager.createStoragePool(args.name, args.nodes, args.disks)

        elif args.type == "template":
            manager.createTemplate(args.name, args.iso, args.kickstart)

        else:
            raise NotImplementedError("create %s" % args.type)

    elif args.command == "clone":

        manager.cloneGuest(args.name, args.cloneName)

    elif args.command == "delete":

        if args.type == "guest":
            for name in args.name:
                manager.deleteGuest(name)

        elif args.type == "network":
            for name in args.name:
                manager.deleteNetwork(name)

        elif args.type == "pool":
            for name in args.name:
                manager.deleteStoragePool(name)

        else:
            raise NotImplementedError("delete %s" % args.type)

if __name__ == '__main__':
    main()
