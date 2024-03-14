from django.utils.translation import ugettext_lazy as _
from freenasUI.freeadmin.tree import TreeNode

NAME = _('Tasks')
BLACKLIST = []
ICON = 'TasksIcon'
ORDER = 5


class CloudSync(TreeNode):

    gname = 'CloudSync'
    replace_only = True
    append_to = 'tasks'


class CronJobView(TreeNode):

    gname = 'View'
    type = 'opentasks'
    append_to = 'tasks.CronJob'


class InitShutdownView(TreeNode):

    gname = 'View'
    type = 'opentasks'
    append_to = 'tasks.InitShutdown'


class RsyncView(TreeNode):

    gname = 'View'
    type = 'opentasks'
    append_to = 'tasks.Rsync'


class SMARTTestView(TreeNode):

    gname = 'View'
    type = 'opentasks'
    append_to = 'tasks.SMARTTest'
