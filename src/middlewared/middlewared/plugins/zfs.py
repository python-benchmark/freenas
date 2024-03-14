import errno
import subprocess
import threading
import time
from collections import defaultdict

from bsd import geom
import libzfs

from middlewared.schema import Dict, List, Str, Bool, accepts
from middlewared.service import (
    CallError, CRUDService, ValidationError, ValidationErrors, filterable, job,
)
from middlewared.utils import filter_list, start_daemon_thread

SCAN_THREADS = {}


def convert_topology(zfs, vdevs):
    topology = defaultdict(list)
    for vdev in vdevs:
        children = []
        for device in vdev['devices']:
            z_cvdev = libzfs.ZFSVdev(zfs, 'disk')
            z_cvdev.type = 'disk'
            z_cvdev.path = device
            children.append(z_cvdev)

        if vdev['type'] == 'STRIPE':
            topology[vdev['root'].lower()].extend(children)
        else:
            z_vdev = libzfs.ZFSVdev(zfs, 'disk')
            z_vdev.type = vdev['type'].lower()
            z_vdev.children = children
            topology[vdev['root'].lower()].append(z_vdev)
    return topology


def find_vdev(pool, vname):
    """
    Find a vdev in the given `pool` using `vname` looking for
    guid or path

    Returns:
        libzfs.ZFSVdev object
    """
    children = []
    for vdevs in pool.groups.values():
        children += vdevs
    while children:
        child = children.pop()

        if str(vname) == str(child.guid):
            return child

        if child.type == 'disk':
            path = child.path.replace('/dev/', '')
            if path == vname:
                return child

        children += list(child.children)


class ZFSPoolService(CRUDService):

    class Config:
        namespace = 'zfs.pool'
        private = True

    @filterable
    def query(self, filters, options):
        # We should not get datasets, there is zfs.dataset.query for that
        state_kwargs = {'datasets_recursive': False}
        with libzfs.ZFS() as zfs:
            # Handle `id` filter specially to avoiding getting all pool
            if filters and len(filters) == 1 and list(filters[0][:2]) == ['id', '=']:
                try:
                    pools = [zfs.get(filters[0][2]).__getstate__(**state_kwargs)]
                except libzfs.ZFSException:
                    pools = []
            else:
                pools = [i.__getstate__(**state_kwargs) for i in zfs.pools]
        return filter_list(pools, filters, options)

    @accepts(
        Dict(
            'zfspool_create',
            Str('name', required=True),
            List('vdevs', items=[
                Dict(
                    'vdev',
                    Str('root', enum=['DATA', 'CACHE', 'LOG', 'SPARE'], required=True),
                    Str('type', enum=['RAIDZ1', 'RAIDZ2', 'RAIDZ3', 'MIRROR', 'STRIPE'], required=True),
                    List('devices', items=[Str('disk')], required=True),
                ),
            ], required=True),
            Dict('options', additional_attrs=True),
            Dict('fsoptions', additional_attrs=True),
        ),
    )
    def do_create(self, data):
        with libzfs.ZFS() as zfs:
            topology = convert_topology(zfs, data['vdevs'])
            zfs.create(data['name'], topology, data['options'], data['fsoptions'])

        return self.middleware.call_sync('zfs.pool._get_instance', data['name'])

    @accepts(Str('pool'), Dict(
        'options',
        Dict('properties', additional_attrs=True),
    ))
    def do_update(self, name, options):
        try:
            with libzfs.ZFS() as zfs:
                pool = zfs.get(name)
                for k, v in options['properties'].items():
                    prop = pool.properties[k]
                    if 'value' in v:
                        prop.value = v['value']
                    elif 'parsed' in v:
                        prop.parsed = v['parsed']
        except libzfs.ZFSException as e:
            raise CallError(str(e))

    @accepts(Str('pool'), Dict(
        'options',
        Bool('force', default=False),
    ))
    def do_delete(self, name, options):
        try:
            with libzfs.ZFS() as zfs:
                zfs.destroy(name, force=options['force'])
        except libzfs.ZFSException as e:
            raise CallError(str(e))

    @accepts(Str('pool', required=True))
    def upgrade(self, pool):
        try:
            with libzfs.ZFS() as zfs:
                zfs.get(pool).upgrade()
        except libzfs.ZFSException as e:
            raise CallError(str(e))

    @accepts(Str('pool'), Dict(
        'options',
        Bool('force', default=False),
    ))
    def export(self, name, options):
        try:
            with libzfs.ZFS() as zfs:
                # FIXME: force not yet implemented
                pool = zfs.get(name)
                zfs.export_pool(pool)
        except libzfs.ZFSException as e:
            raise CallError(str(e))

    @accepts(Str('pool'))
    def get_devices(self, name):
        try:
            with libzfs.ZFS() as zfs:
                return [i.replace('/dev/', '') for i in zfs.get(name).disks]
        except libzfs.ZFSException as e:
            raise CallError(str(e), errno.ENOENT)

    @accepts(Str('pool'))
    def get_disks(self, name):
        disks = self.get_devices(name)

        geom.scan()
        labelclass = geom.class_by_name('LABEL')
        for dev in disks:
            dev = dev.replace('.eli', '')
            find = labelclass.xml.findall(f".//provider[name='{dev}']/../consumer/provider")
            name = None
            if find:
                name = geom.provider_by_id(find[0].get('ref')).geom.name
            else:
                g = geom.geom_by_name('DEV', dev)
                if g:
                    name = g.consumer.provider.geom.name

            if name and geom.geom_by_name('DISK', name):
                yield name
            else:
                self.logger.debug(f'Could not find disk for {dev}')

    @accepts(
        Str('name'),
        List('new', default=None, null=True),
        List('existing', items=[
            Dict(
                'attachvdev',
                Str('target'),
                Str('type', enum=['DISK']),
                Str('path'),
            ),
        ], null=True, default=None),
    )
    @job()
    def extend(self, job, name, new=None, existing=None):
        """
        Extend a zfs pool `name` with `new` vdevs or attach to `existing` vdevs.
        """

        if new is None and existing is None:
            raise CallError('New or existing vdevs must be provided', errno.EINVAL)

        try:
            with libzfs.ZFS() as zfs:
                pool = zfs.get(name)

                if new:
                    topology = convert_topology(zfs, new)
                    pool.attach_vdevs(topology)

                # Make sure we can find all target vdev
                for i in (existing or []):
                    target = find_vdev(pool, i['target'])
                    if target is None:
                        raise CallError(f"Failed to find vdev for {i['target']}", errno.EINVAL)
                    i['target'] = target

                for i in (existing or []):
                    newvdev = libzfs.ZFSVdev(zfs, i['type'].lower())
                    newvdev.path = i['path']
                    i['target'].attach(newvdev)

        except libzfs.ZFSException as e:
            raise CallError(str(e), e.code)

    def __zfs_vdev_operation(self, name, label, op):
        try:
            with libzfs.ZFS() as zfs:
                pool = zfs.get(name)
                target = find_vdev(pool, label)
                if target is None:
                    raise CallError(f'Failed to find vdev for {label}', errno.EINVAL)
                op(target)
        except libzfs.ZFSException as e:
            raise CallError(str(e), e.code)

    @accepts(Str('pool'), Str('label'))
    def detach(self, name, label):
        """
        Detach device `label` from the pool `pool`.
        """
        self.__zfs_vdev_operation(name, label, lambda target: target.detach())

    @accepts(Str('pool'), Str('label'))
    def offline(self, name, label):
        """
        Offline device `label` from the pool `pool`.
        """
        self.__zfs_vdev_operation(name, label, lambda target: target.offline())

    @accepts(Str('pool'), Str('label'))
    def online(self, name, label):
        """
        Online device `label` from the pool `pool`.
        """
        self.__zfs_vdev_operation(name, label, lambda target: target.online())

    @accepts(Str('pool'), Str('label'))
    def remove(self, name, label):
        """
        Remove device `label` from the pool `pool`.
        """
        self.__zfs_vdev_operation(name, label, lambda target: target.remove())

    @accepts(Str('pool'), Str('label'), Str('dev'))
    def replace(self, name, label, dev):
        """
        Replace device `label` with `dev` in pool `name`.
        """
        try:
            with libzfs.ZFS() as zfs:
                pool = zfs.get(name)
                target = find_vdev(pool, label)
                if target is None:
                    raise CallError(f'Failed to find vdev for {label}', errno.EINVAL)

                newvdev = libzfs.ZFSVdev(zfs, 'disk')
                newvdev.path = f'/dev/{dev}'
                target.replace(newvdev)
        except libzfs.ZFSException as e:
            raise CallError(str(e), e.code)

    @accepts(
        Str('name', required=True),
        Str('action', enum=['START', 'STOP', 'PAUSE'], default='START')
    )
    @job(lock=lambda i: f'{i[0]}-{i[1] if len(i) >= 2 else "START"}')
    def scrub(self, job, name, action=None):
        """
        Start/Stop/Pause a scrub on pool `name`.
        """
        if action != 'PAUSE':
            try:
                with libzfs.ZFS() as zfs:
                    pool = zfs.get(name)

                    if action == 'START':
                        pool.start_scrub()
                    else:
                        pool.stop_scrub()
            except libzfs.ZFSException as e:
                raise CallError(str(e), e.code)
        else:
            proc = subprocess.Popen(
                f'zpool scrub -p {name}'.split(' '),
                stdout=subprocess.PIPE, stderr=subprocess.PIPE
            )
            proc.communicate()

            if proc.returncode != 0:
                raise CallError('Unable to pause scrubbing')

        def watch():
            while True:
                with libzfs.ZFS() as zfs:
                    scrub = zfs.get(name).scrub.__getstate__()

                if scrub['pause']:
                    job.set_progress(100, 'Scrub paused')
                    break

                if scrub['function'] != 'SCRUB':
                    break

                if scrub['state'] == 'FINISHED':
                    job.set_progress(100, 'Scrub finished')
                    break

                if scrub['state'] == 'CANCELED':
                    break

                if scrub['state'] == 'SCANNING':
                    job.set_progress(scrub['percentage'], 'Scrubbing')
                time.sleep(1)

        if action == 'START':
            t = threading.Thread(target=watch, daemon=True)
            t.start()
            t.join()

    @accepts()
    def find_import(self):
        with libzfs.ZFS() as zfs:
            return [i.__getstate__() for i in zfs.find_import()]

    @accepts(
        Str('name_or_guid'),
        Dict('options', additional_attrs=True),
        Bool('any_host', default=True),
        Str('cachefile', null=True, default=None),
    )
    def import_pool(self, name_or_guid, options, any_host, cachefile):
        found = False
        with libzfs.ZFS() as zfs:
            for pool in zfs.find_import(cachefile=cachefile):
                if pool.name == name_or_guid or str(pool.guid) == name_or_guid:
                    found = pool
                    break

            if not found:
                raise CallError(f'Pool {name_or_guid} not found.', errno.ENOENT)

            zfs.import_pool(found, found.name, options, any_host=any_host)

    @accepts(Str('pool'))
    async def find_not_online(self, pool):
        pool = await self.middleware.call('zfs.pool.query', [['id', '=', pool]], {'get': True})

        unavails = []
        for nodes in pool['groups'].values():
            for node in nodes:
                unavails.extend(self.__find_not_online(node))
        return unavails

    def __find_not_online(self, node):
        if len(node['children']) == 0 and node['status'] not in ('ONLINE', 'AVAIL'):
            return [node]

        unavails = []
        for child in node['children']:
            unavails.extend(self.__find_not_online(child))
        return unavails

    def get_vdev(self, name, vname):
        try:
            with libzfs.ZFS() as zfs:
                pool = zfs.get(name)
                vdev = find_vdev(pool, vname)
                return vdev.__getstate__()
        except libzfs.ZFSException as e:
            raise CallError(str(e))


class ZFSDatasetService(CRUDService):

    class Config:
        namespace = 'zfs.dataset'
        private = True

    @filterable
    def query(self, filters=None, options=None):
        with libzfs.ZFS() as zfs:
            # Handle `id` filter specially to avoiding getting all datasets
            if filters and len(filters) == 1 and list(filters[0][:2]) == ['id', '=']:
                try:
                    datasets = [zfs.get_dataset(filters[0][2]).__getstate__()]
                except libzfs.ZFSException:
                    datasets = []
            else:
                datasets = [i.__getstate__() for i in zfs.datasets]
        return filter_list(datasets, filters, options)

    @accepts(Dict(
        'dataset_create',
        Str('name', required=True),
        Str('type', enum=['FILESYSTEM', 'VOLUME'], default='FILESYSTEM'),
        Dict(
            'properties',
            Bool('sparse'),
            additional_attrs=True,
        ),
    ))
    def do_create(self, data):
        """
        Creates a ZFS dataset.
        """

        verrors = ValidationErrors()

        if '/' not in data['name']:
            verrors.add('name', 'You need a full name, e.g. pool/newdataset')

        if verrors:
            raise verrors

        properties = data.get('properties') or {}
        sparse = properties.pop('sparse', False)
        params = {}

        for k, v in data['properties'].items():
            params[k] = v

        try:
            with libzfs.ZFS() as zfs:
                pool = zfs.get(data['name'].split('/')[0])
                pool.create(data['name'], params, fstype=getattr(libzfs.DatasetType, data['type']), sparse_vol=sparse)
        except libzfs.ZFSException as e:
            self.logger.error('Failed to create dataset', exc_info=True)
            raise CallError(f'Failed to create dataset: {e}')

    @accepts(
        Str('id'),
        Dict(
            'dataset_update',
            Dict(
                'properties',
                additional_attrs=True,
            ),
        ),
    )
    def do_update(self, id, data):
        try:
            with libzfs.ZFS() as zfs:
                dataset = zfs.get_dataset(id)

                if 'properties' in data:
                    for k, v in data['properties'].items():

                        # If prop already exists we just update it,
                        # otherwise create a user property
                        prop = dataset.properties.get(k)
                        if prop:
                            if v.get('source') == 'INHERIT':
                                if isinstance(prop, libzfs.ZFSUserProperty):
                                    # Workaround because libzfs crashes when trying to inherit user property
                                    subprocess.check_call(["zfs", "inherit", k, id])
                                else:
                                    prop.inherit()
                            elif 'value' in v and (
                                prop.value != v['value'] or prop.source.name == 'INHERITED'
                            ):
                                prop.value = v['value']
                            elif 'parsed' in v and (
                                prop.parsed != v['parsed'] or prop.source.name == 'INHERITED'
                            ):
                                prop.parsed = v['parsed']
                        else:
                            if v.get('source') == 'INHERIT':
                                pass
                            else:
                                if 'value' not in v:
                                    raise ValidationError('properties', f'properties.{k} needs a "value" attribute')
                                if ':' not in k:
                                    raise ValidationError('properties', f'User property needs a colon (:) in its name`')
                                prop = libzfs.ZFSUserProperty(v['value'])
                                dataset.properties[k] = prop

        except libzfs.ZFSException as e:
            self.logger.error('Failed to update dataset', exc_info=True)
            raise CallError(f'Failed to update dataset: {e}')

    def do_delete(self, id, options=None):
        options = options or {}
        force = options.get('force', False)
        recursive = options.get('recursive', False)

        args = []
        if force:
            args += ['-f']
        if recursive:
            args += ['-r']

        # Destroying may take a long time, lets not use py-libzfs as it will block
        # other ZFS operations.
        try:
            subprocess.run(
                ['zfs', 'destroy'] + args + [id], text=True, capture_output=True, check=True,
            )
        except subprocess.CalledProcessError as e:
            self.logger.error('Failed to delete dataset', exc_info=True)
            raise CallError(f'Failed to delete dataset: {e.stderr.strip()}')

    @accepts(Str('name'), Dict('options', Bool('recursive', default=False)))
    def mount(self, name, options):
        try:
            with libzfs.ZFS() as zfs:
                dataset = zfs.get_dataset(name)
                if options['recursive']:
                    dataset.mount_recursive()
                else:
                    dataset.mount()
        except libzfs.ZFSException as e:
            self.logger.error('Failed to mount dataset', exc_info=True)
            raise CallError(f'Failed to mount dataset: {e}')

    def promote(self, name):
        try:
            with libzfs.ZFS() as zfs:
                dataset = zfs.get_dataset(name)
                dataset.promote()
        except libzfs.ZFSException as e:
            self.logger.error('Failed to promote dataset', exc_info=True)
            raise CallError(f'Failed to promote dataset: {e}')

    def inherit(self, name, prop, recursive=False):
        try:
            with libzfs.ZFS() as zfs:
                dataset = zfs.get_dataset(name)
                zprop = dataset.properties.get(prop)
                if not zprop:
                    raise CallError(f'Property {prop!r} not found.', errno.ENOENT)
                zprop.inherit(recursive=recursive)
        except libzfs.ZFSException as e:
            raise CallError(str(e))


class ZFSSnapshot(CRUDService):

    class Config:
        namespace = 'zfs.snapshot'

    @filterable
    def query(self, filters=None, options=None):
        # Special case for faster listing of snapshot names (#53149)
        if options and options.get('select') == ['name']:
            # Using zfs list -o name is dozens of times faster than py-libzfs
            cmd = ['zfs', 'list', '-H', '-o', 'name', '-t', 'snapshot']
            order_by = options.get('order_by')
            # -s name makes it even faster
            if not order_by or order_by == ['name']:
                cmd += ['-s', 'name']
            cp = subprocess.run(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                universal_newlines=True,
            )
            if cp.returncode != 0:
                raise CallError(f'Failed to retrieve snapshots: {cp.stderr}')
            snaps = [{'name': i} for i in cp.stdout.strip().split('\n')]
            if filters:
                return filter_list(snaps, filters, options)
            return snaps
        with libzfs.ZFS() as zfs:
            # Handle `id` filter to avoid getting all snapshots first
            if filters and len(filters) == 1 and list(filters[0][:2]) == ['id', '=']:
                try:
                    snapshots = [zfs.get_snapshot(filters[0][2]).__getstate__()]
                except libzfs.ZFSException as e:
                    if e.code != libzfs.Error.NOENT:
                        raise
                    snapshots = []
            else:
                snapshots = [i.__getstate__() for i in list(zfs.snapshots)]
        # FIXME: awful performance with hundreds/thousands of snapshots
        return filter_list(snapshots, filters, options)

    @accepts(Dict(
        'snapshot_create',
        Str('dataset'),
        Str('name'),
        Bool('recursive'),
        Bool('vmware_sync', default=False),
        Dict('properties', additional_attrs=True)
    ))
    def do_create(self, data):
        """
        Take a snapshot from a given dataset.

        Returns:
            bool: True if succeed otherwise False.
        """

        dataset = data.get('dataset', '')
        name = data.get('name', '')
        recursive = data.get('recursive', False)
        properties = data.get('properties', None)

        if not dataset or not name:
            return False

        vmware_context = None
        if data['vmware_sync']:
            vmware_context = self.middleware.call_sync('vmware.snapshot_begin', dataset, recursive)

        try:
            with libzfs.ZFS() as zfs:
                ds = zfs.get_dataset(dataset)
                ds.snapshot(f'{dataset}@{name}', recursive=recursive, fsopts=properties)

                if vmware_context and vmware_context['vmsynced']:
                    ds.properties['freenas:vmsynced'] = libzfs.ZFSUserProperty('Y')

            self.logger.info(f"Snapshot taken: {dataset}@{name}")
            return True
        except libzfs.ZFSException as err:
            self.logger.error(f"{err}")
            return False
        finally:
            if vmware_context:
                self.middleware.call_sync('vmware.snapshot_end', vmware_context)

    @accepts(Dict(
        'snapshot_remove',
        Str('dataset', required=True),
        Str('name', required=True),
        Bool('defer_delete')
    ))
    def remove(self, data):
        """
        Remove a snapshot from a given dataset.

        Returns:
            bool: True if succeed otherwise False.
        """
        self.logger.debug('zfs.snapshot.remove is deprecated, use zfs.snapshot.delete')
        snapshot_name = data['dataset'] + '@' + data['name']
        try:
            self.do_delete(snapshot_name, {'defer': data.get('defer_delete') or False})
        except Exception:
            return False
        return True

    @accepts(
        Str('id'),
        Dict('options', Bool('defer', default=False)),
    )
    def do_delete(self, id, options):
        """
        Delete snapshot of name `id`.

        `options.defer` will defer the deletion of snapshot.
        """
        try:
            with libzfs.ZFS() as zfs:
                snap = zfs.get_snapshot(id)
                snap.delete(defer=options['defer'])
        except libzfs.ZFSException as e:
            raise CallError(str(e))

    @accepts(Dict(
        'snapshot_clone',
        Str('snapshot'),
        Str('dataset_dst'),
    ))
    def clone(self, data):
        """
        Clone a given snapshot to a new dataset.

        Returns:
            bool: True if succeed otherwise False.
        """

        snapshot = data.get('snapshot', '')
        dataset_dst = data.get('dataset_dst', '')

        if not snapshot or not dataset_dst:
            return False

        try:
            with libzfs.ZFS() as zfs:
                snp = zfs.get_snapshot(snapshot)
                snp.clone(dataset_dst)
            self.logger.info("Cloned snapshot {0} to dataset {1}".format(snapshot, dataset_dst))
            return True
        except libzfs.ZFSException as err:
            self.logger.error("{0}".format(err))
            return False

    @accepts(
        Str('id'),
        Dict(
            'options',
            Bool('recursive', default=False),
            Bool('recursive_clones', default=False),
            Bool('force', default=False),
        ),
    )
    def rollback(self, id, options):
        """
        Rollback to a given snapshot `id`.

        `options.recursive` will destroy any snapshots and bookmarks more recent than the one
        specified.

        `options.recursive_clones` is just like `recursive` but will also destroy any clones.

        `options.force` will force unmount of any clones.
        """
        args = []
        if options['force']:
            args += ['-f']
        if options['recursive']:
            args += ['-r']
        if options['recursive_clones']:
            args += ['-R']

        try:
            subprocess.run(
                ['zfs', 'rollback'] + args + [id], text=True, capture_output=True, check=True,
            )
        except subprocess.CalledProcessError as e:
            raise CallError(f'Failed to rollback snapshot: {e.stderr.strip()}')


class ScanWatch(object):

    def __init__(self, middleware, pool):
        self.middleware = middleware
        self.pool = pool
        self._cancel = threading.Event()

    def run(self):

        while not self._cancel.wait(2):
            with libzfs.ZFS() as zfs:
                scan = zfs.get(self.pool).scrub.__getstate__()
            if scan['state'] == 'SCANNING':
                self.send_scan(scan)
            elif scan['state'] == 'FINISHED':
                # Since this thread finishes on scrub/resilver end the event is sent
                # on devd event arrival
                break

    def send_scan(self, scan=None):
        if not scan:
            with libzfs.ZFS() as zfs:
                scan = zfs.get(self.pool).scrub.__getstate__()
        self.middleware.send_event('zfs.pool.scan', 'CHANGED', fields={
            'scan': scan,
            'name': self.pool,
        })

    def cancel(self):
        self._cancel.set()


async def _handle_zfs_events(middleware, event_type, args):
    data = args['data']
    if data.get('type') in ('misc.fs.zfs.resilver_start', 'misc.fs.zfs.scrub_start'):
        pool = data.get('pool_name')
        if not pool:
            return
        if pool in SCAN_THREADS:
            return
        scanwatch = ScanWatch(middleware, pool)
        SCAN_THREADS[pool] = scanwatch
        start_daemon_thread(target=scanwatch.run)

    elif data.get('type') in (
        'misc.fs.zfs.resilver_finish', 'misc.fs.zfs.scrub_finish', 'misc.fs.zfs.scrub_abort',
    ):
        pool = data.get('pool_name')
        if not pool:
            return
        scanwatch = SCAN_THREADS.pop(pool, None)
        if not scanwatch:
            return
        await middleware.run_in_thread(scanwatch.cancel)

        # Send the last event with SCRUB/RESILVER as FINISHED
        await middleware.run_in_thread(scanwatch.send_scan)

    if data.get('type') == 'misc.fs.zfs.scrub_finish':
        await middleware.call('mail.send', {
            'subject': 'scrub finished',
            'text': f"scrub of pool '{data.get('pool_name')}' finished",
        })


def setup(middleware):
    middleware.event_subscribe('devd.zfs', _handle_zfs_events)
