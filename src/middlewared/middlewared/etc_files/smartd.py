import functools
import logging
import re
import subprocess

from middlewared.common.camcontrol import camcontrol_list
from middlewared.common.smart.smartctl import get_smartctl_args
from middlewared.utils import run
from middlewared.utils.asyncio_ import asyncio_map

logger = logging.getLogger(__name__)


async def annotate_disk_for_smart(devices, disk):
    if disk["disk_name"] is None or disk["disk_name"].startswith("nvd"):
        return

    device = devices.get(disk["disk_name"])
    if device:
        args = await get_smartctl_args(disk["disk_name"], device)
        if args:
            if await ensure_smart_enabled(args):
                args.extend(["-d", "removable"])
                return dict(disk, smartctl_args=args)


async def ensure_smart_enabled(args):
    p = await run(["smartctl", "-i"] + args, stderr=subprocess.STDOUT, check=False, encoding="utf8", errors="ignore")
    if not re.search("SMART.*abled", p.stdout):
        logger.debug("SMART is not supported on %r", args)
        return False

    if re.search("SMART.*Enabled", p.stdout):
        return True

    p = await run(["smartctl", "-s", "on"] + args, stderr=subprocess.STDOUT, check=False)
    if p.returncode == 0:
        return True
    else:
        logger.debug("Unable to enable smart on %r", args)
        return False


def get_smartd_config(disk):
    args = " ".join(disk["smartctl_args"])

    critical = disk['smart_critical'] if disk['disk_critical'] is None else disk['disk_critical']
    difference = disk['smart_difference'] if disk['disk_difference'] is None else disk['disk_difference']
    informational = disk['smart_informational'] if disk['disk_informational'] is None else disk['disk_informational']
    config = f"{args} -n {disk['smart_powermode']} -W {difference}," \
             f"{informational},{critical}"

    if disk['smart_email']:
        config += f" -m {disk['smart_email']}"
    else:
        config += f" -m root"

    config += " -M exec /usr/local/libexec/smart_alert.py"

    if disk.get('smarttest_type'):
        config += f"\\\n-s {disk['smarttest_type']}/" + get_smartd_schedule(disk) + "\\\n"

    config += f" {disk['disk_smartoptions']}"

    return config


def get_smartd_schedule(disk):
    return "/".join([
        get_smartd_schedule_piece(disk['smarttest_month'], 1, 12, dict(zip([
            "jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"
        ], range(1, 13)))),
        get_smartd_schedule_piece(disk['smarttest_daymonth'], 1, 31),
        get_smartd_schedule_piece(disk['smarttest_dayweek'], 1, 7, dict(zip([
            "mon", "tue", "wed", "thu", "fri", "sat", "sun"
        ], range(1, 8)))),
        get_smartd_schedule_piece(disk['smarttest_hour'], 0, 23),
    ])


def get_smartd_schedule_piece(value, min, max, enum=None):
    enum = enum or {}

    width = len(str(max))

    if value == "*":
        return "." * width
    m = re.match("\*/([0-9]+)", value)
    if m:
        d = int(m.group(1))
        if d == 1:
            return "." * width
        values = [v for v in range(min, max + 1) if v % d == 0]
    else:
        values = list(filter(lambda v: v is not None,
                             map(lambda s: enum.get(s.lower(), int(s) if re.match("([0-9]+)$", s) else None),
                                 value.split(","))))
        if values == list(range(min, max + 1)):
            return "." * width

    return "(" + "|".join([f"%0{width}d" % v for v in values]) + ")"


async def render(service, middleware):
    smart_config = await middleware.call("datastore.query", "services.smart", None, {"get": True})

    disks = await middleware.call("datastore.sql", """
        SELECT *
        FROM storage_disk d
        LEFT JOIN tasks_smarttest_smarttest_disks sd ON sd.disk_id = d.disk_identifier
        LEFT JOIN tasks_smarttest s ON s.id = sd.smarttest_id OR s.smarttest_all_disks = true
        WHERE disk_togglesmart = 1 AND disk_expiretime IS NULL
    """)

    disks = [dict(disk, **smart_config) for disk in disks]

    devices = await camcontrol_list()
    disks = await asyncio_map(functools.partial(annotate_disk_for_smart, devices), disks, 16)

    config = ""
    for disk in filter(None, disks):
        config += get_smartd_config(disk) + "\n"

    with open("/usr/local/etc/smartd.conf", "w") as f:
        f.write(config)
