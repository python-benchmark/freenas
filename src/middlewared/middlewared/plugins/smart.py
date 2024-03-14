import functools
from itertools import chain

from middlewared.common.camcontrol import camcontrol_list
from middlewared.common.smart.smartctl import get_smartctl_args
from middlewared.schema import accepts, Bool, Cron, Dict, Int, List, Patch, Str
from middlewared.validators import Email, Range, Unique
from middlewared.service import CRUDService, filterable, filter_list, private, SystemServiceService, ValidationErrors
from middlewared.utils import run
from middlewared.utils.asyncio_ import asyncio_map


async def annotate_disk_smart_tests(devices, disk):
    if disk["disk"] is None or disk["disk"].startswith("nvd"):
        return None

    device = devices.get(disk["disk"])
    if device:
        args = await get_smartctl_args(disk["disk"], device)
        p = await run(["smartctl", "-l", "selftest"] + args, check=False, encoding="utf8")
        tests = parse_smart_selftest_results(p.stdout)
        if tests is not None:
            return dict(tests=tests, **disk)


def parse_smart_selftest_results(stdout):
    tests = []

    # ataprint.cpp
    if "LBA_of_first_error" in stdout:
        for line in stdout.split("\n"):
            if not line.startswith("#"):
                continue

            test = {
                "num": int(line[1:3].strip()),
                "description": line[5:24].strip(),
                "status_verbose": line[25:54].strip(),
                "remaining": int(line[55:57]) / 100,
                "lifetime": int(line[60:68].strip()),
                "lba_of_first_error": line[77:].strip(),
            }

            if test["status_verbose"] == "Completed without error":
                test["status"] = "SUCCESS"
            elif test["status_verbose"] == "Self-test routine in progress":
                test["status"] = "RUNNING"
            else:
                test["status"] = "FAILED"

            if test["lba_of_first_error"] == "-":
                test["lba_of_first_error"] = None

            tests.append(test)

        return tests

    # scsiprint.cpp
    if "LBA_first_err" in stdout:
        for line in stdout.split("\n"):
            if not line.startswith("#"):
                continue

            test = {
                "num": int(line[1:3].strip()),
                "description": line[5:20].strip(),
                "status_verbose": line[23:48].strip(),
                "segment_number": line[49:52].strip(),
                "lifetime": line[55:60].strip(),
                "lba_of_first_error": line[60:78].strip(),
            }

            if test["status_verbose"] == "Completed":
                test["status"] = "SUCCESS"
            elif test["status_verbose"] == "Self test in progress ...":
                test["status"] = "RUNNING"
            else:
                test["status"] = "FAILED"

            if test["segment_number"] == "-":
                test["segment_number"] = None
            else:
                test["segment_number"] = int(test["segment_number"])

            if test["lifetime"] == "NOW":
                test["lifetime"] = None
            else:
                test["lifetime"] = int(test["lifetime"])

            if test["lba_of_first_error"] == "-":
                test["lba_of_first_error"] = None

            tests.append(test)

        return tests


class SMARTTestService(CRUDService):

    class Config:
        datastore = 'tasks.smarttest'
        datastore_extend = 'smart.test.smart_test_extend'
        datastore_prefix = 'smarttest_'
        namespace = 'smart.test'

    async def smart_test_extend(self, data):
        disks = data.pop('disks')
        data['disks'] = [disk['disk_identifier'] for disk in disks]
        test_type = {
            'L': 'LONG',
            'S': 'SHORT',
            'C': 'CONVEYANCE',
            'O': 'OFFLINE',
        }
        data['type'] = test_type[data.pop('type')]
        Cron.convert_db_format_to_schedule(data)
        return data

    @private
    async def validate_data(self, data, schema):
        verrors = ValidationErrors()

        smart_tests = await self.query(filters=[('type', '=', data['type'])])
        configured_disks = [d for test in smart_tests for d in test['disks']]
        disks_dict = {disk['identifier']: disk['name'] for disk in (await self.middleware.call('disk.query'))}

        disks = data.get('disks')
        used_disks = []
        invalid_disks = []
        for disk in disks:
            if disk in configured_disks:
                used_disks.append(disks_dict[disk])
            if disk not in disks_dict.keys():
                invalid_disks.append(disk)

        if used_disks:
            verrors.add(
                f'{schema}.disks',
                f'The following disks already have tests for this type: {", ".join(used_disks)}'
            )

        if invalid_disks:
            verrors.add(
                f'{schema}.disks',
                f'The following disks are invalid: {", ".join(invalid_disks)}'
            )

        return verrors

    @accepts(
        Dict(
            'smart_task_create',
            Cron('schedule'),
            Str('desc'),
            Bool('all_disks', default=False),
            List('disks', items=[Str('disk')], default=[]),
            Str('type', enum=['LONG', 'SHORT', 'CONVEYANCE', 'OFFLINE'], required=True),
            register=True
        )
    )
    async def do_create(self, data):
        data['type'] = data.pop('type')[0]
        verrors = await self.validate_data(data, 'smart_test_create')

        if data['all_disks']:
            if data.get('disks'):
                verrors.add(
                    'smart_test_create.disks',
                    'This test is already enabled for all disks'
                )
        else:
            if not data.get('disks'):
                verrors.add(
                    'smart_test_create.disks',
                    'This field is required'
                )

        if verrors:
            raise verrors

        Cron.convert_schedule_to_db_format(data)

        data['id'] = await self.middleware.call(
            'datastore.insert',
            self._config.datastore,
            data,
            {'prefix': self._config.datastore_prefix}
        )

        await self._service_change('smartd', 'restart')

        return data

    @accepts(
        Int('id', validators=[Range(min=1)]),
        Patch('smart_task_create', 'smart_task_update', ('attr', {'update': True}))
    )
    async def do_update(self, id, data):
        old = await self.query(filters=[('id', '=', id)], options={'get': True})
        new = old.copy()
        new.update(data)

        new['type'] = new.pop('type')[0]
        old['type'] = old.pop('type')[0]
        new_disks = [disk for disk in new['disks'] if disk not in old['disks']]
        deleted_disks = [disk for disk in old['disks'] if disk not in new['disks']]
        if old['type'] == new['type']:
            new['disks'] = new_disks
        verrors = await self.validate_data(new, 'smart_test_update')

        new['disks'] = [disk for disk in chain(new_disks, old['disks']) if disk not in deleted_disks]

        if new['all_disks']:
            if new.get('disks'):
                verrors.add(
                    'smart_test_update.disks',
                    'This test is already enabled for all disks'
                )
        else:
            if not new.get('disks'):
                verrors.add(
                    'smart_test_update.disks',
                    'This field is required'
                )

        if verrors:
            raise verrors

        Cron.convert_schedule_to_db_format(new)

        await self.middleware.call(
            'datastore.update',
            self._config.datastore,
            id,
            new,
            {'prefix': self._config.datastore_prefix}
        )

        await self._service_change('smartd', 'restart')

        return await self.query(filters=[('id', '=', id)], options={'get': True})

    @accepts(
        Int('id')
    )
    async def do_delete(self, id):
        response = await self.middleware.call(
            'datastore.delete',
            self._config.datastore,
            id
        )

        await self._service_change('smartd', 'restart')

        return response

    @filterable
    async def results(self, filters, options):
        """
        Get disk(s) S.M.A.R.T. test(s) results.

        .. examples(websocket)::

          Get all disks tests results

            :::javascript
            {
                "id": "6841f242-840a-11e6-a437-00e04d680384",
                "msg": "method",
                "method": "smart.test.results",
                "params": []
            }

            returns

            :::javascript

            [
              # ATA disk
              {
                "disk": "ada0",
                "tests": [
                  {
                    "num": 1,
                    "description": "Short offline",
                    "status": "SUCCESS",
                    "status_verbose": "Completed without error",
                    "remaining": 0.0,
                    "lifetime": 16590,
                    "lba_of_first_error": None,
                  }
                ]
              },
              # SCSI disk
              {
                "disk": "ada1",
                "tests": [
                  {
                    "num": 1,
                    "description": "Background long",
                    "status": "FAILED",
                    "status_verbose": "Completed, segment failed",
                    "segment_number": None,
                    "lifetime": 3943,
                    "lba_of_first_error": None,
                  }
                ]
              },
            ]

          Get specific disk test results

            :::javascript
            {
                "id": "6841f242-840a-11e6-a437-00e04d680384",
                "msg": "method",
                "method": "smart.test.results",
                "params": [
                  [["disk", "=", "ada0"]],
                  {"get": true}
                ]
            }

            returns

            :::javascript

            {
              "disk": "ada0",
              "tests": [
                {
                  "num": 1,
                  "description": "Short offline",
                  "status": "SUCCESS",
                  "status_verbose": "Completed without error",
                  "remaining": 0.0,
                  "lifetime": 16590,
                  "lba_of_first_error": None,
                }
              ]
            }
        """

        get = (options or {}).pop("get", False)

        disks = filter_list(
            [{"disk": disk["name"]} for disk in await self.middleware.call("disk.query")],
            filters,
            options,
        )

        devices = await camcontrol_list()
        return filter_list(
            list(filter(None, await asyncio_map(functools.partial(annotate_disk_smart_tests, devices), disks, 16))),
            [],
            {"get": get},
        )


class SmartService(SystemServiceService):

    class Config:
        service = "smartd"
        service_model = "smart"
        datastore_extend = "smart.smart_extend"
        datastore_prefix = "smart_"

    @private
    async def smart_extend(self, smart):
        smart["powermode"] = smart["powermode"].upper()
        smart["email"] = smart["email"].split(",")
        return smart

    @accepts(Dict(
        'smart_update',
        Int('interval'),
        Str('powermode', enum=['NEVER', 'SLEEP', 'STANDBY', 'IDLE']),
        Int('difference'),
        Int('informational'),
        Int('critical'),
        List('email', validators=[Unique()], items=[Str('email', validators=[Email()])]),
        update=True
    ))
    async def do_update(self, data):
        old = await self.config()

        new = old.copy()
        new.update(data)

        new["powermode"] = new["powermode"].lower()
        new["email"] = ",".join([email.strip() for email in new["email"]])

        await self._update_service(old, new)

        if new["powermode"] != old["powermode"]:
            await self.middleware.call("service.restart", "collectd", {"onetime": False})

        await self.smart_extend(new)

        return new
