import pickle as pickle

from lockfile import LockFile

from middlewared.alert.base import Alert, AlertLevel, OneShotAlertSource, ThreadedAlertSource

VMWARELOGIN_FAILS = "/var/tmp/.vmwarelogin_fails"


class VMWareLoginFailedAlertSource(OneShotAlertSource):
    level = AlertLevel.WARNING
    title = "VMWare login failed"

    async def create(self, args):
        return Alert("VMWare login to %(hostname)s failed: %(error)s", args)

    async def delete(self, alerts, query):
        hostname = query

        return list(filter(
            lambda alert: alert.args["hostname"] != hostname,
            alerts
        ))


class LegacyVMWareLoginFailedAlertSource(ThreadedAlertSource):
    level = AlertLevel.WARNING
    title = "VMWare login failed (legacy replication)"

    def check_sync(self):
        try:
            with LockFile(VMWARELOGIN_FAILS):
                with open(VMWARELOGIN_FAILS, "rb") as f:
                    fails = pickle.load(f)
        except Exception:
            return

        alerts = []
        for oid, errmsg in list(fails.items()):
            try:
                vmware = self.middleware.call_sync("datastore.query", "storage.vmwareplugin", [["id", "=", oid]],
                                                   {"get": True})
            except IndexError:
                continue

            alerts.append(Alert(
                "VMWare %(vmware)s failed to login to snapshot: %(err)s",
                {
                    "vmware": vmware,
                    "err": errmsg,
                }
            ))

        return alerts
