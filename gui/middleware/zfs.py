# Copyright (c) 2011 iXsystems, Inc.
# All rights reserved.
#
# Redistribution and use in source and binary forms, with or without
# modification, are permitted provided that the following conditions
# are met:
# 1. Redistributions of source code must retain the above copyright
#    notice, this list of conditions and the following disclaimer.
# 2. Redistributions in binary form must reproduce the above copyright
#    notice, this list of conditions and the following disclaimer in the
#    documentation and/or other materials provided with the distribution.
#
# THIS SOFTWARE IS PROVIDED BY THE AUTHOR AND CONTRIBUTORS ``AS IS'' AND
# ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE
# IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE
# ARE DISCLAIMED.  IN NO EVENT SHALL THE AUTHOR OR CONTRIBUTORS BE LIABLE
# FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL
# DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS
# OR SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION)
# HOWEVER CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT
# LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY
# OUT OF THE USE OF THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF
# SUCH DAMAGE.
#
from decimal import Decimal
import bisect
import libzfs
import logging
import re
import subprocess

from collections import defaultdict, OrderedDict
from django.utils.translation import ugettext_lazy as _

log = logging.getLogger('middleware.zfs')

ZPOOL_NAME_RE = r'[a-z][a-z0-9_\-\.]*'


def _is_vdev(name):
    """
    Find out if a given name is a reserved word in zfs
    """
    if (
        name in ('stripe', 'mirror', 'raidz', 'raidz1', 'raidz2', 'raidz3')
        or
        re.search(r'^(mirror|raidz|raidz1|raidz2|raidz3|spare)(-\d+)?$', name)
    ):
        return True
    return False


def _vdev_type(name):
    # raidz needs to appear after other raidz types
    supported_types = ('stripe', 'mirror', 'raidz3', 'raidz2', 'raidz', 'spare-')
    for _type in supported_types:
        if name.startswith(_type):
            return _type
    return False


class Pool(object):
    """
    Class representing a Zpool
    """
    id = None
    name = None
    scrub = None
    resilver = None

    data = None
    cache = None
    spares = None
    logs = None

    def __init__(self, pid, name, scrub, resilver=None):
        self.id = pid
        self.name = name
        self.scrub = scrub
        self.resilver = resilver

    def __getitem__(self, name):
        if hasattr(self, name):
            return getattr(self, name)
        elif name == self.name:
            return self.data
        else:
            raise KeyError

    def add_root(self, root):
        """
        Add a Root to Zpool
        Generally speaking a root can be the data vdevs,
            cache, spares or logs
        """
        if root.name == self.name:
            self.data = root
        else:
            setattr(self, root.name, root)
        root.parent = self

    def find_not_online(self):
        """
        Get disks used within this pool
        """
        unavails = []
        for key in ('data', 'cache', 'spares', 'logs'):
            if getattr(self, key):
                unavails.extend(getattr(self, key).find_not_online())
        return unavails

    def get_dev_by_name(self, name):
        for dev in self.get_devs():
            if dev.name == name:
                return dev

    def get_devs(self):
        """
        Get disks used within this pool
        """
        devs = []
        for key in ('data', 'cache', 'spares', 'logs'):
            klass = getattr(self, key)
            if klass is None:
                continue
            for vdev in klass:
                devs.extend(vdev.get_devs())
        return devs

    def get_disks(self):
        """
        Get disks used within this pool
        """
        disks = []
        for key in ('data', 'cache', 'spares', 'logs'):
            if getattr(self, key):
                disks.extend(getattr(self, key).get_disks())
        return disks

    def validate(self):
        """
        Validate the current tree
        by calling specialized methods
        """
        for key in ('data', 'cache', 'spares', 'logs'):
            if getattr(self, key):
                getattr(self, key).validate()

    def dump(self):
        data = []
        for key in ('data', 'cache', 'spares', 'logs'):
            if getattr(self, key):
                data.append(
                    getattr(self, key).dump()
                )
        return data

    def __repr__(self):
        return repr({
            'data': self.data,
            'cache': self.cache,
            'spare': self.spares,
            'logs': self.logs,
        })


class Tnode(object):
    """
    Abstract class for Root, Vdev and Dev
    """
    name = None
    children = None
    parent = None
    type = None
    status = None

    def __init__(self, name, doc, **kwargs):
        self._doc = doc
        self.name = name
        self.children = []
        self.status = kwargs.pop('status', None)
        self.read = kwargs.pop('read', 0)
        self.write = kwargs.pop('write', 0)
        self.cksum = kwargs.pop('cksum', 0)

    def find_by_name(self, name):
        """
        Find children by a given name
        """
        for child in self.children:
            if child.name == name:
                return child
        return None

    def find_not_online(self):
        """
        Find nodes of status UNAVAIL
        """
        if len(self.children) == 0 and self.status not in ('ONLINE', 'AVAIL'):
            return [self]
        unavails = []
        for child in self.children:
            unavails.extend(child.find_not_online())
        return unavails

    def append(self, tnode):
        """
        Each specialized class should implement this method
        to append children
        """
        raise NotImplementedError

    @staticmethod
    def pprint(node, level=0):
        """
        Print the tree similar to zpool status
        """
        print('   ' * level + node.name)
        for child in node.children:
            node.pprint(child, level + 1)

    def __iter__(self):
        for child in list(self.children):
            yield child

    def validate(self):
        """
        Each specialized method must validate its own instance
        """
        raise NotImplementedError

    def dump(self):
        """
        Each specialized method must dump its own instance
        """
        raise NotImplementedError


class Root(Tnode):
    """
    A Root represents data, cache, spares or logs
    Each Root may contain several Vdevs
    """

    def __repr__(self):
        return "<Root: %s>" % self.name

    def append(self, node):
        """
        Append a Vdev
        """
        if not isinstance(node, Vdev):
            raise Exception("Not a vdev: %s" % node)
        self.children.append(node)
        node.parent = self

    def dump(self):
        vdevs = []
        for vdev in self:
            vdevs.append(vdev.dump())
        return {
            'name': self.name,
            'vdevs': vdevs,
            'numVdevs': len(vdevs),
            'status': self.status if self.status else '',
        }

    def get_disks(self):
        """
        Get disks used within this root
        """
        disks = []
        for vdev in self:
            disks.extend(vdev.get_disks())
        return disks

    def validate(self):
        for vdev in self:
            vdev.validate()


class Vdev(Tnode):
    """
    A Vdev represents a ZFS Virtual Device
    May contain several Devs
    """

    def __repr__(self):
        return "<Section: %s>" % self.name

    def get_disks(self):
        disks = []
        for child in self:
            if isinstance(child, Vdev):
                disks.extend(child.get_disks())
            else:
                if child.disk:
                    disks.append(child.disk)
        return disks

    def append(self, node):
        """
        Append a Dev
        """
        if isinstance(node, Dev) or isinstance(node, Vdev):
            self.children.append(node)
            node.parent = self
        else:
            raise Exception("Not a dev/vdev: %s" % node)

    def get_devs(self):
        # Returns all children of self which are devs ( Dev objects )
        disk_objects = []
        for child in self:
            if isinstance(child, Vdev):
                disk_objects.extend(child.get_devs())
            else:
                disk_objects.append(child)
        return disk_objects

    def dump(self):
        disks = []
        for dev in self.get_devs():
            disks.append(dev.dump())
        return {
            'name': self.name,
            'disks': disks,
            'type': self.type,
            'numDisks': len(disks),
            'status': self.status,
        }

    def validate(self):
        """
        Validate the current Vdev and children (Vdevs, Devs)
        """
        for child in self:
            child.validate()
        if len(self.children) == 0:
            stripe = self.parent.find_by_name("stripe")
            if not stripe:
                stripe = Tnode("stripe", self._doc)
                stripe.type = 'stripe'
                self.parent.append(stripe)
            self.parent.children.remove(self)
            stripe.append(self)
        else:
            self.type = _vdev_type(self.name)


class Dev(Tnode):
    """
    Dev is the leaf in the tree
    """

    disk = None
    devname = None
    path = None

    def __init__(self, *args, **kwargs):
        self.replacing = kwargs.pop('replacing', False)
        super(Dev, self).__init__(*args, **kwargs)

    def __repr__(self):
        return "<Dev: %s>" % self.name

    def dump(self):
        return {
            'name': self.devname,
            'status': self.status,
        }

    def validate(self):
        # The parent of a leaf should be a vdev
        if not _is_vdev(self.parent.name) and self.parent.parent is not None:
            raise Exception(
                "Oh noes! This damn thing should be a vdev! %s" % self.parent
            )

        name = self.name
        search = self._doc.xpath(
            "//class[name = 'ELI']"
            "//provider[name = '%s']/../consumer"
            "/provider/@ref" % name
        )
        if len(search) > 0:
            search = self._doc.xpath(
                "//provider[@id = '%s']/name" % search[0]
            )
            name = search[0].text

        search = self._doc.xpath(
            "//class[name = 'LABEL']"
            "//provider[name = '%s']/../consumer"
            "/provider" % name
        )

        provider = None
        if len(search) > 0:
            self.devname = search[0].xpath("../../name")[0].text
            provider = search[0].attrib.get('ref')
        else:

            # Treat .nop as a regular dev (w/o .nop)
            if self.name.endswith(".nop"):
                self.devname = self.name[:-4]
            else:
                self.devname = self.name
            search = self._doc.xpath(
                "//class[name = 'DEV']"
                "/geom[name = '%s']"
                "//provider/@ref" % self.devname
            )
            if len(search) > 0:
                provider = search[0]
            elif self.status == 'ONLINE':
                log.warn("It should be a valid device: %s", self.name)
                self.disk = self.name
            elif self.name.isdigit():
                pool = self
                while getattr(pool, 'parent', None):
                    pool = pool.parent
                # Lets check whether it is a guid
                try:
                    with libzfs.ZFS() as zfs:
                        vdev = zfs.get(pool.name).vdev_by_guid(int(self.name))
                        if vdev:
                            self.path = vdev.path
                except libzfs.ZFSException as e:
                    if e.code.name != 'NOENT':
                        raise

        if provider:
            search = self._doc.xpath(
                "//provider[@id = '%s']/../name" % provider
            )
            self.disk = search[0].text


class ZFSList(OrderedDict):

    pools = None

    def __init__(self, *args, **kwargs):
        self.pools = {}
        super(ZFSList, self).__init__(*args, **kwargs)

    def append(self, new):
        if new.pool in self.pools:
            bisect.insort(self.pools.get(new.pool), new)
        else:
            self.pools[new.pool] = [new]
        self[new.path] = new

    def find(self, names, root=False):
        if not root:
            search = '/'.join(names[0:2])
            names = names[2:]
        else:
            search = names[0]
            names = names[1:]
        item = self.get(search, None)
        if item:
            while names:
                found = False
                search = names[0]
                names = names[1:]
                for child in item.children:
                    if child.name.rsplit('/', 1)[-1] == search:
                        item = child
                        found = True
                        break
                if not found:
                    break
        return item

    def __getitem__(self, item):
        if isinstance(item, slice):
            zlist = []
            for datasets in list(self.pools.values()):
                zlist.extend(datasets)
            return zlist.__getitem__(item)
        else:
            return super(ZFSList, self).__getitem__(item)

    def __delitem__(self, item):
        self.pools[item.pool].remove(item)
        super(ZFSList, self).__delitem__(item)


class ZFSDataset(object):

    category = 'filesystem'
    name = None
    path = None
    pool = None
    parent = None
    children = None

    properties = [
        'atime', 'used', 'usedsnap', 'usedds', 'usedrefreserv', 'usedchild',
        'avail', 'refer', 'mountpoint', 'sync', 'compression', 'dedup',
        'description', 'quota', 'refquota', 'readonly', 'exec', 'recordsize',
        'reservation', 'refreservation',
    ]

    def __init__(self, path=None, props=None, local=None, default=None, inherit=None, include_root=False):
        self.path = path
        if path:
            if include_root:
                self.pool = path.split('/', 1)[0]
                self.name = path
            else:
                if '/' in path:
                    self.pool, self.name = path.split('/', 1)
                else:
                    self.pool = ''
                    self.name = path
        self.__props = props or {}
        self.parent = None
        self.local = local or []
        self.default = default or []
        self.inherit = inherit or []
        self.children = []

    def __repr__(self):
        return "<Dataset: %s>" % self.path

    def __lt__(self, other):
        return self.path < other.path

    def __getattribute__(self, attr):
        if attr in object.__getattribute__(self, 'properties'):
            return self.__props.get(attr)
        else:
            return object.__getattribute__(self, attr)

    @property
    def full_name(self):
        if self.pool:
            return "%s/%s" % (self.pool, self.name)
        return self.name

    def append(self, child):
        child.parent = self
        self.children.append(child)

    def _get_used_pct(self):
        try:
            return int((float(self.used) / float(self.avail + self.used)) * 100.0)
        except:
            return _("Error")

    used_pct = property(_get_used_pct)


class ZFSVol(object):

    category = 'volume'
    name = None
    path = None
    pool = None
    parent = None
    children = None

    properties = [
        'used', 'usedsnap', 'usedds', 'usedrefreserv', 'usedchild',
        'avail', 'refer', 'volsize', 'sync', 'compression', 'dedup',
        'description', 'readonly',
    ]

    def __init__(self, path=None, props=None):
        self.path = path
        if path:
            if '/' in path:
                self.pool, self.name = path.split('/', 1)
            else:
                self.pool = ''
                self.name = path
        self.__props = props
        self.parent = None
        self.children = []

    def __repr__(self):
        return "<ZFSVol: %s>" % self.path

    def __lt__(self, other):
        return self.path < other.path

    def __getattribute__(self, attr):
        if attr in object.__getattribute__(self, 'properties'):
            return self.__props.get(attr)
        else:
            return object.__getattribute__(self, attr)

    @property
    def full_name(self):
        if self.pool:
            return "%s/%s" % (self.pool, self.name)
        return self.name

    def append(self, child):
        child.parent = self
        self.children.append(child)

    def _get_used_pct(self):
        try:
            return int((float(self.used) / float(self.avail + (self.used - self.usedrefreserv))) * 100.0)
        except:
            return _("Error")

    used_pct = property(_get_used_pct)


class Snapshot(object):

    name = None
    filesystem = None
    used = None
    refer = None
    mostrecent = False
    parent_type = None
    vmsynced = False

    def __init__(
        self,
        name,
        filesystem,
        used,
        refer,
        mostrecent=False,
        parent_type=None,
        vmsynced=False
    ):
        self.name = name
        self.filesystem = filesystem
        self.used = used
        self.refer = refer
        self.mostrecent = mostrecent
        self.parent_type = parent_type
        self.vmsynced = vmsynced

    def __repr__(self):
        return "<Snapshot: %s>" % self.fullname

    @property
    def fullname(self):
        return "%s@%s" % (self.filesystem, self.name)


def parse_status(name, doc, data):

    """
    Parse the scrub statistics from zpool status
    The scrub is within scan: tag and may have multiple lines
    """
    scan = re.search(r'scan: (scrub.+?)\b[a-z]+:', data, re.M | re.S)
    scrub = {}
    if scan:
        scan = scan.group(1)
        if scan.find('in progress') != -1 or scan.find('scrub paused') != -1:
            scrub.update({
                'progress': None,
                'repaired': None,
                'scanned': None,
                'total': None,
                'togo': None,
            })
            if scan.find('in progress') != -1:
                scrub_status = 'IN_PROGRESS'
                scrub_statusv = _('In Progress')
            else:
                scrub_status = 'PAUSED'
                scrub_statusv = _('Paused')

            reg = re.search(r'(\S+)% done', scan)
            if reg:
                scrub['progress'] = Decimal(reg.group(1))

            reg = re.search(r'(\S+) repaired,', scan)
            if reg:
                scrub['repaired'] = reg.group(1)

            reg = re.search(r'(\S+) scanned out of (\S+)', scan)
            if reg:
                scrub['scanned'] = reg.group(1)
                scrub['total'] = reg.group(2)

            reg = re.search(r'(\S+) to go', scan)
            if reg:
                scrub['togo'] = reg.group(1)

        elif scan.find('scrub repaired') != -1:
            scrub.update({
                'repaired': None,
                'errors': None,
                'date': None,
            })
            scrub_status = 'COMPLETED'
            scrub_statusv = _('Completed')
            reg = re.search(r'with (\S+) errors', scan)
            if reg:
                scrub['errors'] = reg.group(1)

            reg = re.search(r'repaired (\S+) in', scan)
            if reg:
                scrub['repaired'] = reg.group(1)

            reg = re.search(r'on (.+\d{2} \d{4})', scan)
            if reg:
                scrub['date'] = reg.group(1)

        elif scan.find('scrub canceled') != -1:
            scrub_status = 'CANCELED'
            scrub_statusv = _('Canceled')

        else:
            scrub_status = 'UNKNOWN'
            scrub_statusv = _('Unknown')

        scrub['status'] = scrub_status
        scrub['status_verbose'] = scrub_statusv
    else:
        scrub['status'] = 'NONE'
        scrub['status_verbose'] = _('None requested')

    """
    Parse the resilver statistics from zpool status
    """
    scan = re.search(r'scan: (resilver.+?)\b[a-z]+:', data, re.M | re.S)
    resilver = {}
    if scan:
        scan = scan.group(1)
        if scan.find('in progress') != -1:
            resilver.update({
                'progress': None,
                'scanned': None,
                'total': None,
                'togo': None,
            })
            resilver_status = 'IN_PROGRESS'
            resilver_statusv = _('In Progress')
            reg = re.search(r'(\S+)% done', scan)
            if reg:
                resilver['progress'] = Decimal(reg.group(1))

            reg = re.search(r'(\S+) scanned out of (\S+)', scan)
            if reg:
                resilver['scanned'] = reg.group(1)
                resilver['total'] = reg.group(2)

            reg = re.search(r'(\S+) to go', scan)
            if reg:
                resilver['togo'] = reg.group(1)

        elif scan.find('resilvered') != -1:
            resilver.update({
                'errors': None,
                'date': None,
            })
            resilver_status = 'COMPLETED'
            resilver_statusv = _('Completed')
            reg = re.search(r'with (\S+) errors', scan)
            if reg:
                resilver['errors'] = reg.group(1)

            reg = re.search(r'on (.+\d{2} \d{4})', scan)
            if reg:
                resilver['date'] = reg.group(1)

        elif scan.find('resilver canceled') != -1:
            resilver_status = 'CANCELED'
            resilver_statusv = _('Canceled')

        else:
            resilver_status = 'UNKNOWN'
            resilver_statusv = _('Unknown')

        resilver['status'] = resilver_status
        resilver['status_verbose'] = resilver_statusv
    else:
        resilver['status'] = 'NONE'
        resilver['status_verbose'] = _('None requested')

    status = data.split('config:')[1]
    pid = re.search(r'id: (?P<id>\d+)', data)
    if pid:
        pid = pid.group("id")
    else:
        pid = None
    pool = Pool(pid=pid, name=name, scrub=scrub, resilver=resilver)
    lastident = None
    pnode = None
    for line in status.split('\n'):
        if not line.startswith('\t'):
            continue

        try:
            spaces, word, status, read, write, cksum = re.search(
                r'''^(?P<spaces>[ ]*)  # Group spaces to know identation
                    (?P<word>\S+)\s+
                    (?P<status>\S+)\s+
                    (?P<read>\S+)\s+(?P<write>\S+)\s+(?P<cksum>\S+)''',
                line[1:],
                re.X
            ).groups()
        except Exception:
            spaces, word, status = re.search(
                r'^(?P<spaces>[ ]*)(?P<word>\S+)(?:\s+(?P<status>\S+))?',
                line[1:]
            ).groups()
            read, write, cksum = 0, 0, 0
        ident = int(len(spaces) / 2)
        if (
                (
                    ident < 2 or
                    (pnode.name.startswith('spare-') and ident == 2)
                ) and
                lastident is not None and
                ident < lastident
        ):
            for x in range(lastident - ident):
                pnode = pnode.parent

        if ident == 0:
            if word != 'NAME':
                tree = Root(
                    word,
                    doc,
                    read=read,
                    write=write,
                    cksum=cksum,
                )
                tree.status = status
                pnode = tree
                pool.add_root(tree)

        elif ident == 1:
            if _is_vdev(word):
                node = Vdev(
                    word,
                    doc,
                    status=status,
                    read=read,
                    write=write,
                    cksum=cksum,
                )
                tree.append(node)
                pnode = node
            else:
                if lastident != ident:
                    node = Vdev(
                        "stripe",
                        doc,
                        read=read,
                        write=write,
                        cksum=cksum,
                    )
                    node.status = status
                    pnode.append(node)
                else:
                    node = pnode
                    pnode = node.parent

                node2 = Dev(
                    word,
                    doc,
                    status=status,
                    read=read,
                    write=write,
                    cksum=cksum,
                )
                node.append(node2)
                pnode = node
        elif ident >= 2:
            if not word.startswith('replacing'):
                if ident == 2 and word.startswith('spare-'):
                    node = Vdev(
                        word,
                        doc,
                        read=read,
                        write=write,
                        cksum=cksum,
                    )
                    node.status = status
                    pnode.append(node)
                    pnode = node
                else:
                    if ident == 3:
                        replacing = True
                    else:
                        replacing = False
                    node = Dev(
                        word,
                        doc,
                        status=status,
                        replacing=replacing,
                        read=read,
                        write=write,
                        cksum=cksum,
                    )
                    pnode.append(node)
            if not pnode.name.startswith('spare-'):
                ident = 2

        lastident = ident
    pool.validate()
    return pool


def zfs_list(path="", recursive=False, hierarchical=False, include_root=False,
             types=None):
    """
    Return a dictionary that contains all ZFS dataset list and their
    mountpoints
    """
    args = [
        "/sbin/zfs",
        "get",
        "-p",
        "-H",
        "-t", ",".join(types or ['filesystem', 'volume']),
        "-o", "name,property,value,source",
        "all",
    ]
    if recursive:
        args.insert(3, "-r")

    if path:
        args.append(path)

    zfsproc = subprocess.Popen(
        args,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        encoding='utf8')

    zfs_output, zfs_err = zfsproc.communicate()
    zfs_output = zfs_output.split('\n')
    zfsget = defaultdict(OrderedDict)
    for line in zfs_output:
        if not line:
            continue
        data = line.split('\t')
        zfsget[data[0]][data[1]] = (data[2], data[3])

    zfslist = ZFSList()
    for path, props in zfsget.items():
        names = path.split('/')
        depth = len(names)
        # root filesystem is not treated as dataset by us
        if depth == 1 and not include_root:
            continue

        zprops = {}
        for pname, dname, ptype in (
            ('available', 'avail', int),
            ('used', 'used', int),
            ('usedbysnapshots', 'usedsnap', int),
            ('usedbydataset', 'usedds', int),
            ('usedbyrefreservation', 'usedrefreserv', int),
            ('usedbychildren', 'usedchild', int),
            ('referenced', 'refer', int),
            ('sync', 'sync', str),
            ('compression', 'compression', str),
            ('dedup', 'dedup', str),
            ('readonly', 'readonly', str),
            ('org.freenas:description', 'description', str),
        ):
            if pname not in props:
                zprops[dname] = None
                continue
            if ptype is int:
                if props[pname][0].isdigit():
                    zprops[dname] = int(props[pname][0])
                else:
                    zprops[dname] = None
            else:
                zprops[dname] = props[pname][0]

        local_props = []
        default_props = []
        inherit_props = []
        for k, v in props.items():
            if v[1] == 'local':
                local_props.append(k)
            elif v[1] == 'default':
                default_props.append(k)
            elif v[1].startswith('inherited'):
                inherit_props.append(k)

        _type = props['type'][0]
        if _type == 'filesystem':
            zprops['atime'] = props['atime'][0]
            zprops['mountpoint'] = props['mountpoint'][0]
            zprops['quota'] = int(props['quota'][0]) if props['quota'][0].isdigit() else None
            zprops['refquota'] = int(props['refquota'][0]) if props['refquota'][0].isdigit() else None
            zprops['reservation'] = int(props['reservation'][0]) if props['reservation'][0].isdigit() else None
            zprops['refreservation'] = int(props['refreservation'][0]) if props['refreservation'][0].isdigit() else None
            zprops['recordsize'] = int(props['recordsize'][0]) if props['recordsize'][0].isdigit() else None
            zprops['exec'] = props['exec'][0]
            if props['exec'][1].startswith('inherited'):
                inherit_props.append('exec')
            item = ZFSDataset(
                path=path,
                include_root=include_root,
                props=zprops,
                local=local_props,
                default=default_props,
                inherit=inherit_props,
            )
        elif _type == 'volume':
            zprops['volsize'] = int(props['volsize'][0]) if props['volsize'][0].isdigit() else None
            item = ZFSVol(
                path=path,
                props=zprops,
            )
        else:
            raise NotImplementedError

        if not hierarchical:
            zfslist.append(item)
            continue

        parentds = zfslist.find(names, root=include_root)
        if parentds:
            parentds.append(item)
        else:
            zfslist.append(item)

    return zfslist


def list_datasets(path="", recursive=False, hierarchical=False,
                  include_root=False):
    return zfs_list(
        path=path,
        recursive=recursive,
        hierarchical=hierarchical,
        include_root=include_root,
        types=["filesystem"],
    )


def zpool_list(name=None):
    zfsproc = subprocess.Popen([
        'zpool',
        'list',
        '-o', 'name,size,alloc,free,cap',
        '-p',
        '-H',
    ] + ([name] if name else []), stdout=subprocess.PIPE, stderr=subprocess.PIPE, encoding='utf8')

    output = zfsproc.communicate()[0].strip('\n')
    if zfsproc.returncode != 0:
        raise SystemError('zpool list failed')

    rv = {}
    for line in output.split('\n'):
        data = line.split('\t')
        attrs = {
            'name': data[0],
            'size': int(data[1]) if data[1].isdigit() else None,
            'alloc': int(data[2]) if data[2].isdigit() else None,
            'free': int(data[3]) if data[3].isdigit() else None,
            'capacity': int(data[4]) if data[4].isdigit() else None,
        }
        rv[attrs['name']] = attrs

    if name:
        return rv[name]
    return rv


def zfs_ashift_from_label(pool, label):
    with libzfs.ZFS() as zfs:
        pool = zfs.get(pool)
        if not pool:
            return None
        if label.isdigit():
            vdev = pool.vdev_by_guid(int(label))
        else:
            vdev = vdev_by_path(pool.groups, '/dev/' + label)
        if not vdev:
            return None
        return vdev.stats.configured_ashift


def iterate_vdevs(topology):
    for group in list(topology.values()):
        for vdev in group:
            if vdev.type == 'disk':
                yield vdev
            elif vdev.children:
                for subvdev in vdev.children:
                    yield subvdev


def vdev_by_path(topology, path):
    for i in iterate_vdevs(topology):
        if i.path == path:
            return i
    return None
