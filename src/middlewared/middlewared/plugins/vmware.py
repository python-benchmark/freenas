from datetime import datetime
import errno
import socket
import ssl
import uuid

from middlewared.async_validators import resolve_hostname
from middlewared.schema import accepts, Bool, Dict, Int, Str, Patch
from middlewared.service import CallError, CRUDService, private, ValidationErrors

from pyVim import connect, task as VimTask
from pyVmomi import vim, vmodl


class VMWareService(CRUDService):

    class Config:
        datastore = 'storage.vmwareplugin'
        datastore_extend = 'vmware.item_extend'

    @private
    async def item_extend(self, item):
        item['password'] = await self.middleware.call('pwenc.decrypt', item['password'])
        return item

    @private
    async def validate_data(self, data, schema_name):
        verrors = ValidationErrors()

        await resolve_hostname(self.middleware, verrors, f'{schema_name}.hostname', data['hostname'])

        if data['filesystem'] not in (await self.middleware.call('pool.filesystem_choices')):
            verrors.add(
                f'{schema_name}.filesystem',
                'Invalid ZFS filesystem'
            )

        datastore = data.get('datastore')
        try:
            ds = await self.middleware.run_in_thread(
                self.get_datastores,
                {
                    'hostname': data.get('hostname'),
                    'username': data.get('username'),
                    'password': data.get('password'),
                }
            )

            datastores = []
            for i in ds.values():
                datastores += i.keys()
            if data.get('datastore') not in datastores:
                verrors.add(
                    f'{schema_name}.datastore',
                    f'Datastore "{datastore}" not found on the server'
                )
        except Exception as e:
            verrors.add(
                f'{schema_name}.datastore',
                'Failed to connect: ' + str(e)
            )

        if verrors:
            raise verrors

    @accepts(
        Dict(
            'vmware_create',
            Str('datastore', required=True),
            Str('filesystem', required=True),
            Str('hostname', required=True),
            Str('password', private=True, required=True),
            Str('username', required=True),
            register=True
        )
    )
    async def do_create(self, data):
        await self.validate_data(data, 'vmware_create')

        data['password'] = await self.middleware.call('pwenc.encrypt', data['password'])

        data['id'] = await self.middleware.call(
            'datastore.insert',
            self._config.datastore,
            data
        )

        return await self._get_instance(data['id'])

    @accepts(
        Int('id', required=True),
        Patch('vmware_create', 'vmware_update', ('attr', {'update': True}))
    )
    async def do_update(self, id, data):
        old = await self._get_instance(id)
        new = old.copy()

        new.update(data)

        await self.validate_data(new, 'vmware_update')

        new['password'] = await self.middleware.call('pwenc.encrypt', new['password'])

        await self.middleware.call(
            'datastore.update',
            self._config.datastore,
            id,
            new,
        )

        return await self._get_instance(id)

    @accepts(
        Int('id')
    )
    async def do_delete(self, id):

        response = await self.middleware.call(
            'datastore.delete',
            self._config.datastore,
            id
        )

        return response

    @accepts(Dict(
        'vmware-creds',
        Str('hostname'),
        Str('username'),
        Str('password'),
    ))
    def get_datastores(self, data):
        """
        Get datastores from VMWare.
        """
        try:
            ssl_context = ssl.SSLContext(ssl.PROTOCOL_SSLv23)
            ssl_context.verify_mode = ssl.CERT_NONE
            server_instance = connect.SmartConnect(
                host=data['hostname'],
                user=data['username'],
                pwd=data['password'],
                sslContext=ssl_context,
            )
        except (vim.fault.InvalidLogin, vim.fault.NoPermission, vim.fault.RestrictedVersion) as e:
            raise CallError(e.msg, errno.EPERM)
        except vmodl.RuntimeFault as e:
            raise CallError(e.msg)
        except (socket.gaierror, socket.error, OSError) as e:
            raise CallError(str(e), e.errno)

        content = server_instance.RetrieveContent()
        objview = content.viewManager.CreateContainerView(
            content.rootFolder, [vim.HostSystem], True
        )

        esxi_hosts = objview.view
        objview.Destroy()

        datastores = {}
        for esxi_host in esxi_hosts:
            storage_system = esxi_host.configManager.storageSystem
            datastores_host = {}

            if storage_system.fileSystemVolumeInfo is None:
                continue

            for host_mount_info in storage_system.fileSystemVolumeInfo.mountInfo:
                if host_mount_info.volume.type == 'VMFS':
                    datastores_host[host_mount_info.volume.name] = {
                        'type': host_mount_info.volume.type,
                        'uuid': host_mount_info.volume.uuid,
                        'capacity': host_mount_info.volume.capacity,
                        'vmfs_version': host_mount_info.volume.version,
                        'local': host_mount_info.volume.local,
                        'ssd': host_mount_info.volume.ssd
                    }
                elif host_mount_info.volume.type == 'NFS':
                    datastores_host[host_mount_info.volume.name] = {
                        'type': host_mount_info.volume.type,
                        'capacity': host_mount_info.volume.capacity,
                        'remote_host': host_mount_info.volume.remoteHost,
                        'remote_path': host_mount_info.volume.remotePath,
                        'remote_hostnames': host_mount_info.volume.remoteHostNames,
                        'username': host_mount_info.volume.userName,
                    }
                elif host_mount_info.volume.type in ('OTHER', 'VFFS'):
                    # Ignore VFFS type, it does not store VM's
                    # Ignore OTHER type, it does not seem to be meaningful
                    pass
                else:
                    self.logger.debug(f'Unknown volume type "{host_mount_info.volume.type}": {host_mount_info.volume}')
                    continue
            datastores[esxi_host.name] = datastores_host

        connect.Disconnect(server_instance)
        return datastores

    @accepts(Int('pk'))
    async def get_virtual_machines(self, pk):

        item = await self.query([('id', '=', pk)], {'get': True})

        ssl_context = ssl.SSLContext(ssl.PROTOCOL_SSLv23)
        ssl_context.verify_mode = ssl.CERT_NONE
        server_instance = connect.SmartConnect(
            host=item['hostname'],
            user=item['username'],
            pwd=item['password'],
            sslContext=ssl_context,
        )

        content = server_instance.RetrieveContent()
        objview = content.viewManager.CreateContainerView(content.rootFolder, [vim.VirtualMachine], True)
        vm_view = objview.view
        objview.Destroy()

        vms = {}
        for vm in vm_view:
            data = {
                'uuid': vm.config.uuid,
                'name': vm.name,
                'power_state': vm.summary.runtime.powerState,
            }
            vms[vm.config.uuid] = data
        return vms

    @accepts(Str('dataset'), Bool('recursive'))
    def dataset_has_vms(self, dataset, recursive):
        return len(self._dataset_get_vms(dataset, recursive)) > 0

    def _dataset_get_vms(self, dataset, recursive):
        f = ["filesystem", "=", dataset]
        if recursive:
            f = [
                "OR", [
                    f,
                    ["filesystem", "^", dataset + "/"],
                ],
            ]
        return self.middleware.call_sync("vmware.query", f)

    @private
    def snapshot_begin(self, dataset, recursive):
        # If there's a VMWare Plugin object for this filesystem
        # snapshot the VMs before taking the ZFS snapshot.
        # Once we've taken the ZFS snapshot we're going to log back in
        # to VMWare and destroy all the VMWare snapshots we created.
        # We do this because having VMWare snapshots in existence impacts
        # the performance of your VMs.
        qs = self._dataset_get_vms(dataset, recursive)

        # Generate a unique snapshot name that (hopefully) won't collide with anything
        # that exists on the VMWare side.
        vmsnapname = str(uuid.uuid4())

        # Generate a helpful description that is visible on the VMWare side.  Since we
        # are going to be creating VMWare snaps, if one gets left dangling this will
        # help determine where it came from.
        vmsnapdescription = f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} FreeNAS Created Snapshot"

        # We keep track of snapshots per VMWare "task" because we are going to iterate
        # over all the VMWare tasks for a given ZFS filesystem, do all the VMWare snapshotting
        # then take the ZFS snapshot, then iterate again over all the VMWare "tasks" and undo
        # all the snaps we created in the first place.
        vmsnapobjs = []
        for vmsnapobj in qs:
            # Data structures that will be used to keep track of VMs that are snapped,
            # as wel as VMs we tried to snap and failed, and VMs we realized we couldn't
            # snapshot.
            snapvms = []
            snapvmfails = []
            snapvmskips = []

            try:
                ssl_context = ssl.SSLContext(ssl.PROTOCOL_SSLv23)
                ssl_context.verify_mode = ssl.CERT_NONE
                si = connect.SmartConnect(host=vmsnapobj["hostname"], user=vmsnapobj["username"],
                                          pwd=vmsnapobj["password"], sslContext=ssl_context)
                content = si.RetrieveContent()
            except Exception as e:
                self.logger.warn("VMware login to %s failed", vmsnapobj["hostname"], exc_info=True)
                self._alert_vmware_login_failed(vmsnapobj, e)
                continue

            # There's no point to even consider VMs that are paused or powered off.
            vm_view = content.viewManager.CreateContainerView(content.rootFolder, [vim.VirtualMachine], True)
            for vm in vm_view.view:
                if vm.summary.runtime.powerState != "poweredOn":
                    continue

                if self._doesVMDependOnDataStore(vm, vmsnapobj.datastore):
                    try:
                        if self._canSnapshotVM(vm):
                            if not self._doesVMSnapshotByNameExists(vm, vmsnapname):
                                # have we already created a snapshot of the VM for this volume
                                # iteration? can happen if the VM uses two datasets (a and b)
                                # where both datasets are mapped to the same ZFS volume in FreeNAS.
                                VimTask.WaitForTask(vm.CreateSnapshot_Task(
                                    name=vmsnapname,
                                    description=vmsnapdescription,
                                    memory=False, quiesce=False,
                                ))
                            else:
                                self.logger.debug("Not creating snapshot %s for VM %s because it "
                                                  "already exists", vmsnapname, vm)
                        else:
                            # TODO:
                            # we can try to shutdown the VM, if the user provided us an ok to do
                            # so (might need a new list property in obj to know which VMs are
                            # fine to shutdown and a UI to specify such exceptions)
                            # otherwise can skip VM snap and then make a crash-consistent zfs
                            # snapshot for this VM
                            self.logger.info("Can't snapshot VM %s that depends on "
                                             "datastore %s and filesystem %s. "
                                             "Possibly using PT devices. Skipping.",
                                             vm.name, vmsnapobj.datastore, dataset)
                            snapvmskips.append(vm.config.uuid)
                    except Exception as e:
                        self.logger.warning("Snapshot of VM %s failed", vm.name, exc_info=True)
                        self.middleware.call("alert.oneshot_create", "VMWareSnapshotCreateFailed", {
                            "hostname": vmsnapobj["hostname"],
                            "vm": vm.name,
                            "snapshot": vmsnapname,
                            "error": str(e),
                        })
                        snapvmfails.append([vm.config.uuid, vm.name])

                    snapvms.append(vm.config.uuid)

            connect.Disconnect(si)

            vmsnapobjs.append({
                "vmsnapobj": vmsnapobj,
                "snapvms": snapvms,
                "snapvmfails": snapvmfails,
                "snapvmskips": snapvmskips,
            })

        # At this point we've completed snapshotting VMs.

        if not vmsnapobjs:
            return None

        return {
            "vmsnapname": vmsnapname,
            "vmsnapobjs": vmsnapobjs,
            "vmsynced": vmsnapobjs and all(len(vmsnapobj["snapvms"]) > 0 and len(vmsnapobj["snapvmfails"]) == 0
                                           for vmsnapobj in vmsnapobjs)
        }

    @private
    async def snapshot_end(self, context):
        vmsnapname = context["vmsnapname"]

        for elem in context["vmsnapobjs"]:
            vmsnapobj = elem["vmsnapobj"]

            try:
                ssl_context = ssl.SSLContext(ssl.PROTOCOL_SSLv23)
                ssl_context.verify_mode = ssl.CERT_NONE
                si = connect.SmartConnect(host=vmsnapobj["hostname"], user=vmsnapobj["username"],
                                          pwd=vmsnapobj["password"], sslContext=ssl_context)
                self._delete_vmware_login_failed_alert(vmsnapobj)
            except Exception as e:
                self.logger.warning("VMware login failed to %s", vmsnapobj["hostname"])
                self._alert_vmware_login_failed(vmsnapobj, e)
                continue

            # vm is an object, so we'll dereference that object anywhere it's user facing.
            for vm_uuid in elem["snapvms"]:
                vm = si.content.searchIndex.FindByUuid(None, vm_uuid, True)
                if not vm:
                    self.logger.debug("Could not find VM %s", vm_uuid)
                    continue
                if [vm_uuid, vm.name] not in elem["snapvmfails"] and vm_uuid not in elem["snapvmskips"]:
                    # The test above is paranoia.  It shouldn't be possible for a vm to
                    # be in more than one of the three dictionaries.
                    snap = self._doesVMSnapshotByNameExists(vm, vmsnapname)
                    try:
                        if snap is not False:
                            VimTask.WaitForTask(snap.RemoveSnapshot_Task(True))
                    except Exception as e:
                        self.logger.debug("Exception removing snapshot %s on %s", vmsnapname, vm.name, exc_info=True)
                        self.middleware.call("alert.oneshot_create", "VMWareSnapshotDeleteFailed", {
                            "hostname": vmsnapobj["hostname"],
                            "vm": vm.name,
                            "snapshot": vmsnapname,
                            "error": str(e),
                        })

            connect.Disconnect(si)

    @private
    def periodic_snapshot_task_begin(self, task_id):
        task = self.middleware.call_sync("pool.snapshottask.query",
                                         [["id", "=", task_id]],
                                         {"get": True})

        return self.snapshot_begin(task["dataset"], task["recursive"])

    @private
    async def periodic_snapshot_task_end(self, context):
        return self.snapshot_end(context)

    # Check if a VM is using a certain datastore
    def _doesVMDependOnDataStore(self, vm, dataStore):
        try:
            # simple case, VM config data is on a datastore.
            # not sure how critical it is to snapshot the store that has config data, but best to do so
            for i in vm.datastore:
                if i.info.name.startswith(dataStore):
                    return True
            # check if VM has disks on the data store
            # we check both "diskDescriptor" and "diskExtent" types of files
            for device in vm.config.hardware.device:
                if device.backing is None:
                    continue
                if hasattr(device.backing, 'fileName'):
                    if device.backing.datastore.info.name == dataStore:
                        return True
        except Exception:
            self.logger.debug('Exception in doesVMDependOnDataStore', exc_info=True)

        return False

    # check if VMware can snapshot a VM
    def _canSnapshotVM(self, vm):
        try:
            # check for PCI pass-through devices
            for device in vm.config.hardware.device:
                if isinstance(device, vim.VirtualPCIPassthrough):
                    return False
            # consider supporting more cases of VMs that can't be snapshoted
            # https://kb.vmware.com/selfservice/microsites/search.do?language=en_US&cmd=displayKC&externalId=1006392
        except Exception:
            self.logger.debug('Exception in canSnapshotVM', exc_info=True)

        return True

    # check if there is already a snapshot by a given name
    def _doesVMSnapshotByNameExists(self, vm, snapshotName):
        try:
            tree = vm.snapshot.rootSnapshotList
            while tree[0].childSnapshotList is not None:
                snap = tree[0]
                if snap.name == snapshotName:
                    return snap.snapshot
                if len(tree[0].childSnapshotList) < 1:
                    break
                tree = tree[0].childSnapshotList
        except Exception:
            self.logger.debug('Exception in doesVMSnapshotByNameExists', exc_info=True)

        return False

    def _alert_vmware_login_failed(self, vmsnapobj, e):
        if hasattr(e, "msg"):
            vmlogin_fail = e.msg
        else:
            vmlogin_fail = str(e)

        self.middleware.call_sync("alert.oneshot_create", "VMWareLoginFailed", {
            "hostname": vmsnapobj["hostname"],
            "error": vmlogin_fail,
        })

    def _delete_vmware_login_failed_alert(self, vmsnapobj):
        self.middleware.call_sync("alert.oneshot_delete", "VMWareLoginFailed", vmsnapobj["hostname"])
