from django.utils.translation import ugettext_lazy as _
from freenasUI.freeadmin.tree import TreeNode
from freenasUI.middleware.notifier import notifier
from freenasUI.system.models import Support

BLACKLIST = [
    'NTPServer',
    'CertificateAuthority',
    'Certificate'
]
NAME = _('System')
ICON = 'SystemIcon'
ORDER = 1


class Advanced(TreeNode):

    gname = 'Advanced'
    name = _('Advanced')
    icon = "SettingsIcon"
    type = 'opensystem'
    order = -90
    replace_only = True
    append_to = 'system'


class BootEnv(TreeNode):

    gname = 'BootEnv'
    name = _('Boot')
    icon = 'BootIcon'
    type = 'opensystem'
    order = -92


class Email(TreeNode):

    gname = 'Email'
    name = _('Email')
    icon = 'EmailIcon'
    type = 'opensystem'
    order = -85
    replace_only = True
    append_to = 'system'


class General(TreeNode):

    gname = 'Settings'
    name = _('General')
    icon = "SettingsIcon"
    type = 'opensystem'
    order = -95
    replace_only = True
    append_to = 'system'


class Info(TreeNode):

    gname = 'SysInfo'
    name = _('Information')
    icon = "InfoIcon"
    type = 'opensystem'
    order = -100


class SystemDataset(TreeNode):

    gname = 'SystemDataset'
    name = _('System Dataset')
    icon = "SysDatasetIcon"
    type = 'opensystem'
    order = -80
    replace_only = True
    append_to = 'system'


class Reporting(TreeNode):

    gname = 'Reporting'
    name = _('Reporting')
    icon = 'ReportingIcon'
    type = 'opensystem'
    order = -79
    replace_only = True
    append_to = 'system'


class TunableView(TreeNode):

    gname = 'View'
    type = 'opensystem'
    append_to = 'system.Tunable'


class AlertDefaultSettingsView(TreeNode):

    gname = 'View'
    type = 'opensystem'
    replace_only = True
    append_to = 'system.SystemDataset'


class AlertServiceView(TreeNode):

    gname = 'View'
    type = 'opensystem'
    append_to = 'system.AlertService'


class Update(TreeNode):

    gname = 'Update'
    name = _('Update')
    type = 'opensystem'
    icon = 'UpdateIcon'


class CertificateAuthorityView(TreeNode):

    gname = 'CertificateAuthority.View'
    name = _('CAs')
    type = 'opensystem'
    icon = 'CertificateAuthorityIcon'
    order = 10


class CertificateView(TreeNode):

    gname = 'Certificate.View'
    name = _('Certificates')
    type = 'opensystem'
    icon = 'CertificateIcon'
    order = 15


class ACMEDNSAuthenticator(TreeNode):

    gname = 'ACMEDNSAuthenticator'
    replace_only = True
    append_to = 'system'


class SupportTree(TreeNode):

    gname = 'Support'
    name = _('Support')
    icon = "SupportIcon"
    type = 'opensystem'
    order = 20


class ProactiveSupport(TreeNode):

    gname = 'ProactiveSupport'
    name = _(u'Proactive Support')
    icon = u"SupportIcon"
    type = 'opensystem'
    order = 25

    def pre_build_options(self):
        if not Support.is_available()[0]:
            raise ValueError


class ViewEnclosure(TreeNode):

    gname = 'ViewEnclosure'
    name = _(u'View Enclosure')
    icon = u"ViewAllVolumesIcon"
    type = 'opensystem'
    order = 30

    def pre_build_options(self):
        if notifier().is_freenas():
            raise ValueError
