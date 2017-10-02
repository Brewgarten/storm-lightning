"""
Copyright (c) IBM 2015-2017. All Rights Reserved.
Project name: storm-lightning
This project is licensed under the MIT License, see LICENSE

`libvirt` entities that are XML serializable
"""
import inspect
import xml.etree.ElementTree as ElementTree
from xml.dom.minidom import parseString

from storm.lightning.utils import generateMACAddress


DEFAULT_STORAGE_PATH = "/var/lib/virt/images"

GIGABYTE = 1024 ** 3

class XMLSerializable(object):
    """
    Base class that allows child classes inheriting from it to
    serialize to XML and deserialize from XML.

    :param root: root element
    :type root: :class:`~xml.etree.ElementTree.Element`
    """
    def __init__(self, root):
        self._root = root

    @classmethod
    def fromXML(cls, xmlString):
        """
        Load object from the specified XML string

        :param cls: the class to deserialize into
        :type cls: class
        :param xmlString: a XML string
        :type xmlString: str
        :returns: object instance of the respective class
        """
        # create instance based off constructor
        args = inspect.getargspec(cls.__init__)
        requiredArguments = [None] * (len(args[0]) - 1)
        instance = cls(*requiredArguments)
        instance._root = ElementTree.fromstring(xmlString)
        return instance

    def toXML(self, pretty=False):
        """
        Convert object to a XML string

        :param pretty: format XML nicely using indent
        :type pretty: bool
        :returns: str
        """
        xmlString = ElementTree.tostring(self._root)
        if pretty:
            return parseString(xmlString).toprettyxml(indent="  ").replace("<?xml version=\"1.0\" ?>\n", "")
        return xmlString

class CDROMInfo(XMLSerializable):
    """
    CD-ROM disk device

    :param path: optional path to iso
    :type path: str
    """
    def __init__(self, path=None):
        super(CDROMInfo, self).__init__(ElementTree.Element("disk", attrib={"type": "file", "device": "cdrom"}))

        ElementTree.SubElement(
            self._root, "driver",
            attrib={
                "name": "qemu",
                "type": "raw"
            }
        )
        sourceElement = ElementTree.SubElement(self._root, "source")
        if path:
            sourceElement.set("file", path)
        else:
            sourceElement.set("dev", "/dev/sr0")
        ElementTree.SubElement(
            self._root, "target",
            attrib={
                "bus": "virtio",
                "dev": "sdz"
            }
        )
        ElementTree.SubElement(self._root, "readonly")

    @property
    def dev(self):
        """
        Device, e.g., /dev/sr0
        """
        return self._root.find("target").get("dev")

    @dev.setter
    def dev(self, value):
        self._root.find("target").set("dev", value)

    @property
    def path(self):
        """
        Path to an iso file
        """
        return self._root.find("source").get("file")

class DiskInfo(XMLSerializable):
    """
    Hard disk device

    :param path: path to storage image, e.g., qcow2
    :type path: str
    :param dev: device, e.g., /dev/sr0
    :type dev: str
    """
    def __init__(self, path, dev):
        super(DiskInfo, self).__init__(ElementTree.Element("disk", attrib={"type": "file", "device": "disk"}))

        ElementTree.SubElement(
            self._root, "driver",
            attrib={
                "cache": "none",
                "name": "qemu",
                "type": "qcow2"
            }
        )
        ElementTree.SubElement(
            self._root, "source",
            attrib={
                "file": path
            }
        )
        ElementTree.SubElement(
            self._root, "target",
            attrib={
                "bus": "virtio",
                "dev": dev
            }
        )

    @property
    def dev(self):
        """
        Device, e.g., /dev/sda
        """
        return self._root.find("target").get("dev")

    @dev.setter
    def dev(self, value):
        self._root.find("target").set("dev", value)

    @property
    def path(self):
        """
        Path to an iso file
        """
        return self._root.find("source").get("file")

class DomainInfo(XMLSerializable):
    """
    Domain (virtual machine guest)

    :param name: name
    :type name: str
    :param cpus: number of cpus
    :type: cpus: int
    :param memory: amount of memory in bytes
    :type memory: int
    """
    def __init__(self, name, cpus=2, memory=(2 * GIGABYTE)):
        super(DomainInfo, self).__init__(ElementTree.Element("domain", attrib={"type": "kvm"}))
        ElementTree.SubElement(self._root, "name").text = name
        ElementTree.SubElement(self._root, "memory").text = str(memory)
        ElementTree.SubElement(self._root, "currentMemory").text = str(memory)
        ElementTree.SubElement(self._root, "vcpu").text = str(cpus)

        osElement = ElementTree.SubElement(self._root, "os")
        ElementTree.SubElement(osElement, "type", attrib={"arch": "x86_64"}).text = "hvm"

        featuresElement = ElementTree.SubElement(self._root, "features")
        ElementTree.SubElement(featuresElement, "acpi")
        ElementTree.SubElement(featuresElement, "apic")
        ElementTree.SubElement(featuresElement, "pae")

        ElementTree.SubElement(self._root, "clock", attrib={"offset": "utc"})

        ElementTree.SubElement(self._root, "on_poweroff").text = "destroy"
        ElementTree.SubElement(self._root, "on_reboot").text = "restart"
        ElementTree.SubElement(self._root, "on_crash").text = "restart"

        devicesElement = ElementTree.SubElement(self._root, "devices")
        ElementTree.SubElement(
            devicesElement, "console",
            attrib={
                "type": "pty"
            }
        )
        ElementTree.SubElement(
            devicesElement, "graphics",
            attrib={
                "type": "vnc",
                "port": "-1"
            }
        )
        videoElement = ElementTree.SubElement(devicesElement, "video")
        ElementTree.SubElement(
            videoElement, "model",
            attrib={
                "type": "cirrus"
            }
        )

    def addDevice(self, deviceInfo):
        """
        Add device

        :param deviceInfo: device info
        :type deviceInfo: :class:`.CDROMInfo` or :class:`.DiskInfo`
        """
        self._root.find("devices").append(deviceInfo._root)

    def getDiskInfoByName(self, name):
        """
        Get disk

        :param name: name of the disk
        :type name: str
        :returns: disk info
        :rtype: :class:`.DiskInfo`
        """
        disk = self._root.find("devices/disk[@device='disk']/target[@dev='{0}']/..".format(name))
        if disk is not None:
            return DiskInfo.fromXML(ElementTree.tostring(disk))
        return None

    @property
    def diskInfos(self):
        """
        Disks

        :returns: list of disks
        :rtype: [:class:`.DiskInfo`]
        """
        return [
            DiskInfo.fromXML(ElementTree.tostring(disk))
            for disk in self._root.iterfind("devices/disk[@device='disk']")
        ]

    @property
    def name(self):
        """
        Name
        """
        return self._root.find("name").text

    @property
    def networkInterfaceInfos(self):
        """
        Disks

        :returns: list of network interfaces
        :rtype: [:class:`.NetworkInfo`]
        """
        return [
            NetworkInterfaceInfo.fromXML(ElementTree.tostring(networkInterface))
            for networkInterface in self._root.iterfind("devices/interface[@type='network']")
        ]

    @property
    def uuid(self):
        """
        Universally unique identifier
        """
        return self._root.find("uuid").text

class NetworkInfo(XMLSerializable):
    """
    Network

    :param name: name
    :type name: str
    :param subnetPrefix: subnet prefix, e.g., ``10.76``
    :type subnetPrefix: str
    """
    def __init__(self, name, subnetPrefix):
        super(NetworkInfo, self).__init__(ElementTree.Element("network"))
        ElementTree.SubElement(self._root, "name")

        forwardElement = ElementTree.SubElement(
            self._root, "forward",
            attrib={
                "mode": "nat"
            }
        )
        natElement = ElementTree.SubElement(forwardElement, "nat")
        ElementTree.SubElement(
            natElement, "port",
            attrib={
                "start": "1024",
                "end": "65535"
            }
        )

        ElementTree.SubElement(
            self._root, "bridge",
            attrib={
                "name": "{0}b0".format(name),
                "stp": "on",
                "delay": "0"
            }
        )

        ipElement = ElementTree.SubElement(
            self._root, "ip",
            attrib={
                "address": "{0}.0.254".format(subnetPrefix),
                "netmask": "255.255.255.0"
            }
        )
        dhcpElement = ElementTree.SubElement(ipElement, "dhcp")
        ElementTree.SubElement(
            dhcpElement, "range",
            attrib={
                "start": "{0}.0.1".format(subnetPrefix),
                "end": "{0}.0.250".format(subnetPrefix),
            }
        )

        self.name = name

    def getStaticHostInfoByName(self, name):
        """
        Get static host name information

        :param name: name of the host
        :type name: str
        :return: static host information
        :rtype: :class:`.StaticHostInfo`
        """
        host = self._root.find("ip/dhcp/host[@name='{0}']".format(name))
        if host is not None:
            return StaticHostInfo.fromXML(ElementTree.tostring(host))
        return None

    @property
    def dhcpRange(self):
        """
        DHCP range
        """
        rangeElement = self._root.find("ip/dhcp/range")
        return rangeElement.get("start"), rangeElement.get("end")

    @property
    def ipAddress(self):
        """
        IP address
        """
        return self._root.find("ip").get("address")

    @property
    def name(self):
        """
        Name
        """
        return self._root.find("name").text

    @name.setter
    def name(self, value):
        self._root.find("name").text = value

    @property
    def staticHosts(self):
        """
        Static hosts information

        :return: list of static host information
        :rtype: :class:`.StaticHostInfo`
        """
        return [
            StaticHostInfo.fromXML(ElementTree.tostring(host))
            for host in self._root.iterfind("ip/dhcp/host")
        ]

    @property
    def uuid(self):
        """
        Universally unique identifier
        """
        return self._root.find("uuid").text

class NetworkInterfaceInfo(XMLSerializable):
    """
    Network interface

    :param network: name of the associated network
    :type network: str
    :param mac: optional MAC address, generated if not specified
    :type mac: str
    """
    def __init__(self, network, mac=None):
        super(NetworkInterfaceInfo, self).__init__(ElementTree.Element("interface", attrib={"type": "network"}))
        if not mac:
            mac = generateMACAddress()
        ElementTree.SubElement(self._root, "source", attrib={"network": network})
        ElementTree.SubElement(self._root, "mac", attrib={"address": mac})
        ElementTree.SubElement(self._root, "model", attrib={"type": "virtio"})

    @property
    def mac(self):
        """
        MAC address
        """
        return self._root.find("mac").get("address")

    @property
    def network(self):
        """
        Associated network name
        """
        return self._root.find("source").get("network")

class StaticHostInfo(XMLSerializable):
    """
    Static network host information, i.e., host with statically assigned IP address

    :param mac: MAC address
    :type mac: str
    :param name: host name
    :type name: str
    :param ip: IP address
    :type ip: str
    """
    def __init__(self, mac, name, ip):
        super(StaticHostInfo, self).__init__(ElementTree.Element(
            "host",
            attrib={
                "mac": mac,
                "name": name,
                "ip": ip
            }
        ))

    @property
    def ip(self):
        """
        IP address
        """
        return self._root.get("ip")

    @property
    def mac(self):
        """
        MAC address
        """
        return self._root.get("mac")

    @property
    def name(self):
        """
        Host name
        """
        return self._root.get("name")

class StoragePoolInfo(XMLSerializable):
    """
    Storage pool, i.e., a folder containing storage volumes

    :param name: name
    :type name: str
    """
    def __init__(self, name):
        super(StoragePoolInfo, self).__init__(ElementTree.Element("pool", attrib={"type": "dir"}))
        ElementTree.SubElement(self._root, "name")

        targetElement = ElementTree.SubElement(self._root, "target")
        ElementTree.SubElement(targetElement, "path")

        permissionsElement = ElementTree.SubElement(targetElement, "permissions")
        ElementTree.SubElement(permissionsElement, "mode").text = "0777"
        ElementTree.SubElement(permissionsElement, "owner").text = "0"
        ElementTree.SubElement(permissionsElement, "group").text = "0"

        self.name = name
        self.path = "{0}/{1}".format(DEFAULT_STORAGE_PATH, name)

    @property
    def allocation(self):
        """
        Allocated size in bytes
        """
        return int(self._root.find("allocation").text)

    @property
    def available(self):
        """
        Available size in bytes
        """
        return int(self._root.find("available").text)

    @property
    def capacity(self):
        """
        Capacity in bytes
        """
        return int(self._root.find("capacity").text)

    @property
    def group(self):
        """
        Group permissions
        """
        return int(self._root.find("target/permissions/group").text)

    @group.setter
    def group(self, value):
        self._root.find("target/permissions/group").text = str(value)

    @property
    def name(self):
        """
        Name
        """
        return self._root.find("name").text

    @name.setter
    def name(self, value):
        self._root.find("name").text = value

    @property
    def owner(self):
        """
        Owner permissions
        """
        return int(self._root.find("target/permissions/owner").text)

    @owner.setter
    def owner(self, value):
        self._root.find("target/permissions/owner").text = str(value)

    @property
    def path(self):
        """
        Storage path
        """
        return self._root.find("target/path").text

    @path.setter
    def path(self, value):
        self._root.find("target/path").text = value

    @property
    def uuid(self):
        """
        Universally unique identifier
        """
        return self._root.find("uuid").text

class StorageVolumeInfo(XMLSerializable):
    """
    Storage volume, e.g., qcow2 image

    :param name: unique name within storage pool
    :type name: str
    :param capacity: image size in bytes
    :type capacity: int
    """
    def __init__(self, name, capacity=(10 * GIGABYTE)):
        super(StorageVolumeInfo, self).__init__(ElementTree.Element("volume", attrib={"type": "file"}))
        ElementTree.SubElement(self._root, "name")
        # need to make sure we are able to deserialize otherwise we get error
        if capacity is None:
            capacity = (10 * GIGABYTE)
        ElementTree.SubElement(self._root, "allocation", attrib={"unit": "bytes"}).text = str(capacity/10)
        ElementTree.SubElement(self._root, "capacity", attrib={"unit": "bytes"}).text = str(capacity)

        targetElement = ElementTree.SubElement(self._root, "target")
        ElementTree.SubElement(targetElement, "format", attrib={"type": "qcow2"})

        permissionsElement = ElementTree.SubElement(targetElement, "permissions")
        ElementTree.SubElement(permissionsElement, "mode").text = "0777"
        ElementTree.SubElement(permissionsElement, "owner").text = "0"
        ElementTree.SubElement(permissionsElement, "group").text = "0"

        self.name = name

    @property
    def allocation(self):
        """
        Allocated size in bytes
        """
        return int(self._root.find("allocation").text)

    @property
    def capacity(self):
        """
        Capacity in bytes
        """
        return int(self._root.find("capacity").text)

    @property
    def format(self):
        """
        Image format, e.g., qcow2
        """
        return self._root.find("target/format").get("type")

    @property
    def group(self):
        """
        Group permissions
        """
        return int(self._root.find("target/permissions/group").text)

    @group.setter
    def group(self, value):
        self._root.find("target/permissions/group").text = str(value)

    @property
    def key(self):
        """
        Unique name
        """
        return self._root.find("key").text

    @property
    def name(self):
        """
        Unique name within storage pool
        """
        return self._root.find("name").text

    @name.setter
    def name(self, value):
        self._root.find("name").text = value

    @property
    def owner(self):
        """
        Owner permissions
        """
        return int(self._root.find("target/permissions/owner").text)

    @owner.setter
    def owner(self, value):
        self._root.find("target/permissions/owner").text = str(value)

    @property
    def path(self):
        """
        Full image path
        """
        return self._root.find("target/path").text

    @property
    def poolName(self):
        """
        Name of the storage pool this volume belongs to
        """
        path = self._root.find("target/path").text
        path = path.replace(DEFAULT_STORAGE_PATH + "/", "")
        return path.split("/")[0]
