# +
# Copyright 2010 iXsystems, Inc.
# All rights reserved
#
# Redistribution and use in source and binary forms, with or without
# modification, are permitted providing that the following conditions
# are met:
# 1. Redistributions of source code must retain the above copyright
#    notice, this list of conditions and the following disclaimer.
# 2. Redistributions in binary form must reproduce the above copyright
#    notice, this list of conditions and the following disclaimer in the
#    documentation and/or other materials provided with the distribution.
#
# THIS SOFTWARE IS PROVIDED BY THE AUTHOR ``AS IS'' AND ANY EXPRESS OR
# IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE IMPLIED
# WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE
# ARE DISCLAIMED.  IN NO EVENT SHALL THE AUTHOR BE LIABLE FOR ANY
# DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL
# DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS
# OR SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION)
# HOWEVER CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT,
# STRICT LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING
# IN ANY WAY OUT OF THE USE OF THIS SOFTWARE, EVEN IF ADVISED OF THE
# POSSIBILITY OF SUCH DAMAGE.
#
#####################################################################

import errno
import json
import logging
import math
import os
import pickle as pickle
import re
import stat
import subprocess
import threading
import time
from collections import OrderedDict, defaultdict
from datetime import datetime

from django.conf import settings
from django.core.cache import cache
from django.core.files.storage import FileSystemStorage
from django.db.models import Q
from django.forms import FileField
from django.forms.formsets import BaseFormSet, formset_factory
from django.http import HttpResponse
from django.shortcuts import render_to_response
from django.utils import timezone
from django.utils.html import escapejs
from django.utils.safestring import mark_safe
from django.utils.translation import ugettext_lazy as _, ugettext as __
from dojango import forms
from formtools.wizard.views import SessionWizardView
from freenasOS import Configuration, Update
from freenasUI import choices
from freenasUI.account.models import bsdGroups, bsdUsers
from freenasUI.common import humanize_number_si, humanize_size, humansize_to_bytes
from freenasUI.common.forms import Form, ModelForm
from freenasUI.common.freenasldap import FreeNAS_ActiveDirectory, FreeNAS_LDAP
from freenasUI.directoryservice.forms import (ActiveDirectoryForm, LDAPForm,
                                              NISForm)
from freenasUI.directoryservice.models import LDAP, NIS, ActiveDirectory
from freenasUI.freeadmin.forms import SizeField
from freenasUI.freeadmin.utils import key_order
from freenasUI.freeadmin.views import JsonResp
from freenasUI.middleware.client import (ClientException, ValidationErrors,
                                         client)
from freenasUI.middleware.exceptions import MiddlewareError
from freenasUI.middleware.form import MiddlewareModelForm
from freenasUI.middleware.notifier import notifier
from freenasUI.services.models import (iSCSITarget,
                                       iSCSITargetAuthorizedInitiator,
                                       iSCSITargetExtent, iSCSITargetGroups,
                                       iSCSITargetPortal, iSCSITargetPortalIP,
                                       iSCSITargetToExtent, services)
from freenasUI.sharing.models import (AFP_Share, CIFS_Share, NFS_Share,
                                      NFS_Share_Path)
from freenasUI.storage.forms import VolumeAutoImportForm, VolumeMixin
from freenasUI.storage.models import Disk, Scrub, Volume
from freenasUI.system import models
from freenasUI.tasks.models import SMARTTest
from ldap import LDAPError

log = logging.getLogger('system.forms')
WIZARD_PROGRESSFILE = '/tmp/.initialwizard_progress'
LEGACY_MANUAL_UPGRADE = None


def clean_path_execbit(path):
    """
    Make sure the hierarchy has the bit S_IXOTH set
    """
    current = path
    while True:
        try:
            mode = os.stat(current).st_mode
            if mode & stat.S_IXOTH == 0:
                raise forms.ValidationError(
                    _("The path '%s' requires an execute permission bit.") % (
                        current,
                    )
                )
        except OSError:
            break

        current = os.path.realpath(os.path.join(current, os.path.pardir))
        if current == '/':
            break


def clean_path_locked(mp):
    qs = Volume.objects.filter(vol_name=mp.replace('/mnt/', ''))
    if qs.exists():
        obj = qs[0]
        if not obj.is_decrypted():
            raise forms.ValidationError(
                _("Volume %s is locked by encryption.") % (
                    obj.vol_name,
                )
            )


class BootEnvAddForm(Form):

    middleware_attr_schema = 'bootenv'

    name = forms.CharField(
        label=_('Name'),
        max_length=50,
    )

    def __init__(self, *args, **kwargs):
        self._source = kwargs.pop('source', None)
        super(BootEnvAddForm, self).__init__(*args, **kwargs)

    def save(self, *args, **kwargs):
        data = {'name': self.cleaned_data.get('name')}
        if self._source:
            data['source'] = self._source
        with client as c:
            self._middleware_action = 'create'
            try:
                c.call('bootenv.create', data)
            except ValidationErrors:
                raise
            except ClientException:
                raise MiddlewareError(_(
                    'Boot Environment creation failed!'
                ))


class BootEnvRenameForm(Form):

    middleware_attr_schema = 'bootenv'

    name = forms.CharField(
        label=_('Name'),
        max_length=50,
    )

    def __init__(self, *args, **kwargs):
        self._name = kwargs.pop('name')
        super(BootEnvRenameForm, self).__init__(*args, **kwargs)

    def save(self, *args, **kwargs):
        new_name = self.cleaned_data.get('name')
        with client as c:
            self._middleware_action = 'update'
            try:
                c.call('bootenv.update', self._name, {'name': new_name})
            except ValidationErrors:
                raise
            except ClientException:
                raise MiddlewareError(_('Boot Environment rename failed!'))


class BootEnvPoolAttachForm(Form):

    attach_disk = forms.ChoiceField(
        choices=(),
        widget=forms.Select(),
        label=_('Member disk'))
    expand = forms.BooleanField(
        label=_('Use all disk space'),
        help_text=_(
            'Unchecked (default): format the new disk to the same capacity as the '
            'existing disk. Checked: use full capacity of the new disk. If the '
            'original disk in the mirror is replaced, the mirror could grow to '
            'the capacity of the new disk, requiring replacement boot devices '
            'to be as large as this new disk.'
        ),
        required=False,
        initial=False,
    )

    def __init__(self, *args, **kwargs):
        self.label = kwargs.pop('label')
        super(BootEnvPoolAttachForm, self).__init__(*args, **kwargs)
        self.fields['attach_disk'].choices = self._populate_disk_choices()
        self.fields['attach_disk'].choices.sort(
            key=lambda a: float(
                re.sub(r'^.*?([0-9]+)[^0-9]*([0-9]*).*$', r'\1.\2', a[0])
            )
        )

    def _populate_disk_choices(self):

        diskchoices = dict()

        # Grab partition list
        # NOTE: This approach may fail if device nodes are not accessible.
        disks = notifier().get_disks(unused=True)

        for disk in disks:
            devname, capacity = disks[disk]['devname'], disks[disk]['capacity']
            capacity = humanize_number_si(int(capacity))
            diskchoices[devname] = "%s (%s)" % (devname, capacity)

        choices = list(diskchoices.items())
        choices.sort(key=lambda a: float(
            re.sub(r'^.*?([0-9]+)[^0-9]*([0-9]*).*$', r'\1.\2', a[0])
        ))
        return choices

    def done(self):
        devname = self.cleaned_data['attach_disk']

        with client as c:
            try:
                c.call('boot.attach', devname, {'expand': self.cleaned_data['expand']}, job=True)
            except ClientException as e:
                self._errors['__all__'] = self.error_class([str(e)])
                return False
        return True


class BootEnvPoolReplaceForm(Form):

    replace_disk = forms.ChoiceField(
        choices=(),
        widget=forms.Select(),
        label=_('Member disk'))

    def __init__(self, *args, **kwargs):
        self.label = kwargs.pop('label')
        super(BootEnvPoolReplaceForm, self).__init__(*args, **kwargs)
        self.fields['replace_disk'].choices = self._populate_disk_choices()
        self.fields['replace_disk'].choices.sort(
            key=lambda a: float(
                re.sub(r'^.*?([0-9]+)[^0-9]*([0-9]*).*$', r'\1.\2', a[0])
            )
        )

    def _populate_disk_choices(self):

        diskchoices = dict()

        # Grab partition list
        # NOTE: This approach may fail if device nodes are not accessible.
        disks = notifier().get_disks(unused=True)

        for disk in disks:
            devname, capacity = disks[disk]['devname'], disks[disk]['capacity']
            capacity = humanize_number_si(int(capacity))
            diskchoices[devname] = "%s (%s)" % (devname, capacity)

        choices = list(diskchoices.items())
        choices.sort(key=lambda a: float(
            re.sub(r'^.*?([0-9]+)[^0-9]*([0-9]*).*$', r'\1.\2', a[0])
        ))
        return choices

    def done(self):
        devname = self.cleaned_data['replace_disk']

        with client as c:
            try:
                c.call('boot.replace', self.label, devname)
            except ClientException as e:
                self._errors['__all__'] = self.error_class([str(e)])
                return False
        return True


class CommonWizard(SessionWizardView):

    template_done = 'system/done.html'

    def done(self, form_list, **kwargs):
        response = render_to_response(self.template_done, {
            'retval': getattr(self, 'retval', None),
        })
        if not self.request.is_ajax():
            response.content = (
                b"<html><body><textarea>" +
                response.content +
                b"</textarea></boby></html>"
            )
        return response

    def process_step(self, form):
        proc = super(CommonWizard, self).process_step(form)
        """
        We execute the form done method if there is one, for each step
        """
        if hasattr(form, 'done'):
            retval = form.done(
                request=self.request,
                form_list=self.form_list,
                wizard=self)
            if self.get_step_index() == self.steps.count - 1:
                self.retval = retval
        return proc

    def render_to_response(self, context, **kwargs):
        response = super(CommonWizard, self).render_to_response(
            context,
            **kwargs)
        # This is required for the workaround dojo.io.frame for file upload
        if not self.request.is_ajax():
            return HttpResponse(
                "<html><body><textarea>" +
                response.rendered_content +
                "</textarea></boby></html>"
            )
        return response


class FileWizard(CommonWizard):

    file_storage = FileSystemStorage(location='/var/tmp/firmware')


class InitialWizard(CommonWizard):

    template_done = 'system/initialwizard_done.html'

    def get_template_names(self):
        return [
            'system/initialwizard_%s.html' % self.steps.current,
            'system/initialwizard.html',
            'system/wizard.html',
        ]

    def get_context_data(self, form, **kwargs):
        context = super(InitialWizard, self).get_context_data(form, **kwargs)
        if self.steps.last:
            context['form_list'] = list(self.get_form_list().keys())
        return context

    def done(self, form_list, **kwargs):

        events = []
        curstep = 0
        progress = {
            'step': curstep,
            'indeterminate': True,
            'percent': 0,
        }

        with open(WIZARD_PROGRESSFILE, 'wb') as f:
            f.write(pickle.dumps(progress))

        cleaned_data = self.get_all_cleaned_data()
        volume_name = cleaned_data.get('volume_name')
        volume_type = cleaned_data.get('volume_type')
        shares = cleaned_data.get('formset-shares')

        form_list = self.get_form_list()
        volume_form = form_list.get('volume')
        volume_import = form_list.get('import')
        ds_form = form_list.get('ds')
        # sys_form = form_list.get('system')

        model_objs = []
        try:
            _n = notifier()
            if volume_form or volume_import:

                curstep += 1
                progress['step'] = curstep

                with open(WIZARD_PROGRESSFILE, 'wb') as f:
                    f.write(pickle.dumps(progress))

                if volume_import:
                    volume_name, guid = cleaned_data.get(
                        'volume_id'
                    ).split('|')
                    if not _n.zfs_import(volume_name, guid):
                        raise MiddlewareError(_(
                            'Volume "%s" import failed! Check the pool '
                            'status for more details.'
                        ) % volume_name)

                    volume = Volume(vol_name=volume_name)
                    volume.save()
                    model_objs.append(volume)

                    scrub = Scrub.objects.create(scrub_volume=volume)
                    model_objs.append(scrub)

                if volume_form:
                    bysize = volume_form._get_unused_disks_by_size()

                    if volume_type == 'auto':
                        groups = volume_form._grp_autoselect(bysize)
                    else:
                        groups = volume_form._grp_predefined(bysize, volume_type)

                    with client as c:
                        c.call('pool.create', {
                            'name': volume_name,
                            'topology': groups,
                        })

                    volume = Volume.objects.get(vol_name=volume_name)
                    model_objs.append(volume)

                # Create SMART tests for every disk available
                disks = []
                for o in SMARTTest.objects.all():
                    for disk in o.smarttest_disks.all():
                        if disk.pk not in disks:
                            disks.append(disk.pk)
                qs = Disk.objects.filter(disk_expiretime=None).exclude(pk__in=disks)

                if qs.exists():
                    smarttest = SMARTTest.objects.create(
                        smarttest_hour='23',
                        smarttest_type='S',
                        smarttest_dayweek='7',
                    )
                    smarttest.smarttest_disks.add(*list(qs))
                    model_objs.append(smarttest)

            else:
                volume = Volume.objects.all()[0]
                volume_name = volume.vol_name

            curstep += 1
            progress['step'] = curstep
            progress['indeterminate'] = False
            progress['percent'] = 0
            with open(WIZARD_PROGRESSFILE, 'wb') as f:
                f.write(pickle.dumps(progress))

            services_restart = []
            for i, share in enumerate(shares):
                if not share:
                    continue

                share_name = share.get('share_name')
                share_purpose = share.get('share_purpose')
                share_allowguest = share.get('share_allowguest')
                share_timemachine = share.get('share_timemachine')
                share_iscsisize = share.get('share_iscsisize')
                share_user = share.get('share_user')
                share_usercreate = share.get('share_usercreate')
                share_userpw = share.get('share_userpw')
                share_group = share.get('share_group')
                share_groupcreate = share.get('share_groupcreate')
                share_mode = share.get('share_mode')

                dataset_name = '%s/%s' % (volume_name, share_name)
                if share_purpose != 'iscsitarget':
                    try:
                        with client as c:
                            c.call('pool.dataset.create', {
                                'name': dataset_name,
                                'type': 'FILESYSTEM',
                            })
                    except ClientException as e:
                        raise MiddlewareError(
                            _('Failed to create ZFS dataset: %s.') % e
                        )

                    if share_purpose == 'afp':
                        _n.change_dataset_share_type(dataset_name, 'mac')
                    elif share_purpose == 'cifs':
                        _n.change_dataset_share_type(dataset_name, 'windows')
                    elif share_purpose == 'nfs':
                        _n.change_dataset_share_type(dataset_name, 'unix')

                    qs = bsdGroups.objects.filter(bsdgrp_group=share_group)
                    if not qs.exists():
                        if share_groupcreate:
                            try:
                                with client as c:
                                    group = c.call('group.create', {
                                        'name': share_group,
                                    })
                            except ValidationErrors as e:
                                raise MiddlewareError(str(e))
                            else:
                                group = bsdGroups.objects.get(pk=group)
                                model_objs.append(group)
                        else:
                            group = bsdGroups.objects.all()[0]
                    else:
                        group = qs[0]

                    qs = bsdUsers.objects.filter(bsdusr_username=share_user)
                    if not qs.exists():
                        if share_usercreate:
                            try:
                                with client as c:
                                    user = c.call('user.create', {
                                        'username': share_user,
                                        'full_name': share_user,
                                        'password': share_userpw if share_userpw else '',
                                        'shell': '/bin/csh',
                                        'home': '/nonexistent',
                                        'password_disabled': False if share_userpw else True,
                                        'group': group.id,
                                    })
                            except ValidationErrors as e:
                                raise MiddlewareError(str(e))
                            else:
                                user = bsdUsers.objects.get(pk=user)
                                model_objs.append(user)

                else:
                    try:
                        with client as c:
                            c.call('pool.dataset.create', {
                                'name': dataset_name,
                                'type': 'VOLUME',
                                'sparse': True,
                                'volsize': humansize_to_bytes(share_iscsisize),
                            })
                    except ClientException as e:
                        raise MiddlewareError(
                            _('Failed to create ZFS volume: %s.') % e
                        )

                path = '/mnt/%s/%s' % (volume_name, share_name)

                sharekwargs = {}

                if 'cifs' == share_purpose:
                    if share_allowguest:
                        sharekwargs['cifs_guestok'] = True
                    model_objs.append(CIFS_Share.objects.create(
                        cifs_name=share_name,
                        cifs_path=path,
                        **sharekwargs
                    ))

                if 'afp' == share_purpose:
                    if share_timemachine:
                        sharekwargs['afp_timemachine'] = True
                        sharekwargs['afp_timemachine_quota'] = 0
                    model_objs.append(AFP_Share.objects.create(
                        afp_name=share_name,
                        afp_path=path,
                        **sharekwargs
                    ))

                if 'nfs' == share_purpose:
                    nfs_share = NFS_Share.objects.create(
                        nfs_comment=share_name,
                    )
                    model_objs.append(NFS_Share_Path.objects.create(
                        share=nfs_share,
                        path=path,
                    ))

                if 'iscsitarget' == share_purpose:

                    qs = iSCSITargetPortal.objects.all()
                    if qs.exists():
                        portal = qs[0]
                    else:
                        portal = iSCSITargetPortal.objects.create()
                        model_objs.append(portal)
                        model_objs.append(iSCSITargetPortalIP.objects.create(
                            iscsi_target_portalip_portal=portal,
                            iscsi_target_portalip_ip='0.0.0.0',
                        ))

                    qs = iSCSITargetAuthorizedInitiator.objects.all()
                    if qs.exists():
                        authini = qs[0]
                    else:
                        authini = (
                            iSCSITargetAuthorizedInitiator.objects.create()
                        )
                        model_objs.append(authini)
                    try:
                        nic = list(choices.NICChoices(
                            nolagg=True, novlan=True, exclude_configured=False)
                        )[0][0]
                        mac = subprocess.Popen(
                            "ifconfig %s ether| grep ether | "
                            "awk '{print $2}'|tr -d :" % (nic, ),
                            shell=True,
                            stdout=subprocess.PIPE,
                            encoding='utf8',
                        ).communicate()[0]
                        ltg = iSCSITargetExtent.objects.order_by('-id')
                        if ltg.count() > 0:
                            lid = ltg[0].id
                        else:
                            lid = 0
                        serial = mac.strip() + "%.2d" % lid
                    except Exception:
                        serial = "10000001"

                    iscsi_target_name = '%sTarget' % share_name
                    iscsi_target_name_idx = 1
                    while iSCSITarget.objects.filter(iscsi_target_name=iscsi_target_name).exists():
                        iscsi_target_name = '%sTarget%d' % (share_name, iscsi_target_name_idx)
                        iscsi_target_name_idx += 1

                    target = iSCSITarget.objects.create(
                        iscsi_target_name=iscsi_target_name
                    )
                    model_objs.append(target)

                    model_objs.append(iSCSITargetGroups.objects.create(
                        iscsi_target=target,
                        iscsi_target_portalgroup=portal,
                        iscsi_target_initiatorgroup=authini,
                    ))

                    iscsi_target_extent_path = 'zvol/%s/%s' % (
                        volume_name,
                        share_name,
                    )

                    extent = iSCSITargetExtent.objects.create(
                        iscsi_target_extent_name='%sExtent' % share_name,
                        iscsi_target_extent_type='ZVOL',
                        iscsi_target_extent_path=iscsi_target_extent_path,
                        iscsi_target_extent_serial=serial,
                    )
                    model_objs.append(extent)
                    with client as c:
                        target_to_extent = c.call(
                            'iscsi.targetextent.create', {
                                'target': target.id,
                                'extent': extent.id
                            }
                        )
                    model_objs.append(iSCSITargetToExtent.objects.get(pk=target_to_extent['id']))

                if share_purpose not in services_restart:
                    services.objects.filter(srv_service=share_purpose).update(
                        srv_enable=True
                    )
                    services_restart.append(share_purpose)

                progress['percent'] = int(
                    (float(i + 1) / float(len(shares))) * 100
                )
                with open(WIZARD_PROGRESSFILE, 'wb') as f:
                    f.write(pickle.dumps(progress))

            console = cleaned_data.get('sys_console')
            adv = models.Advanced.objects.order_by('-id')[0]
            advdata = dict(adv.__dict__)
            advdata['adv_consolemsg'] = console
            advdata.pop('_state', None)
            advform = AdvancedForm(
                advdata,
                instance=adv,
            )
            if advform.is_valid():
                advform.save()
                advform.done(self.request, events)

            # Set administrative email to receive alerts
            if cleaned_data.get('sys_email'):
                bsdUsers.objects.filter(bsdusr_uid=0).update(
                    bsdusr_email=cleaned_data.get('sys_email'),
                )

            email = models.Email.objects.order_by('-id')[0]
            em = EmailForm(cleaned_data, instance=email)
            if em.is_valid():
                em.save()

            try:
                settingsm = models.Settings.objects.order_by('-id')[0]
            except IndexError:
                settingsm = models.Settings.objects.create()

            settingsdata = settingsm.__dict__
            settingsdata.update({
                'stg_language': cleaned_data.get('stg_language'),
                'stg_kbdmap': cleaned_data.get('stg_kbdmap'),
                'stg_timezone': cleaned_data.get('stg_timezone'),
            })
            settingsform = SettingsForm(
                data=settingsdata,
                instance=settingsm,
            )
            if settingsform.is_valid():
                settingsform.save()
            else:
                log.warn(
                    'Active Directory data failed to validate: %r',
                    settingsform._errors,
                )
        except Exception:
            for obj in reversed(model_objs):
                if isinstance(obj, Volume):
                    obj.delete(destroy=False, cascade=False)
                else:
                    obj.delete()
            raise

        if ds_form:

            curstep += 1
            progress['step'] = curstep
            progress['indeterminate'] = True

            with open(WIZARD_PROGRESSFILE, 'wb') as f:
                f.write(pickle.dumps(progress))

            if cleaned_data.get('ds_type') == 'ad':
                try:
                    ad = ActiveDirectory.objects.all().order_by('-id')[0]
                except Exception:
                    ad = ActiveDirectory.objects.create()
                addata = ad.__dict__
                addata.update({
                    'ad_domainname': cleaned_data.get('ds_ad_domainname'),
                    'ad_bindname': cleaned_data.get('ds_ad_bindname'),
                    'ad_bindpw': cleaned_data.get('ds_ad_bindpw'),
                    'ad_enable': True,
                })
                adform = ActiveDirectoryForm(
                    data=addata,
                    instance=ad,
                )
                if adform.is_valid():
                    adform.save()
                else:
                    log.warn(
                        'Active Directory data failed to validate: %r',
                        adform._errors,
                    )
            elif cleaned_data.get('ds_type') == 'ldap':
                try:
                    ldap = LDAP.objects.all().order_by('-id')[0]
                except Exception:
                    ldap = LDAP.objects.create()
                ldapdata = ldap.__dict__
                ldapdata.update({
                    'ldap_hostname': cleaned_data.get('ds_ldap_hostname'),
                    'ldap_basedn': cleaned_data.get('ds_ldap_basedn'),
                    'ldap_binddn': cleaned_data.get('ds_ldap_binddn'),
                    'ldap_bindpw': cleaned_data.get('ds_ldap_bindpw'),
                    'ldap_enable': True,
                })
                ldapform = LDAPForm(
                    data=ldapdata,
                    instance=ldap,
                )
                if ldapform.is_valid():
                    ldapform.save()
                else:
                    log.warn(
                        'LDAP data failed to validate: %r',
                        ldapform._errors,
                    )
            elif cleaned_data.get('ds_type') == 'nis':
                try:
                    nis = NIS.objects.all().order_by('-id')[0]
                except Exception:
                    nis = NIS.objects.create()
                nisdata = nis.__dict__
                nisdata.update({
                    'nis_domain': cleaned_data.get('ds_nis_domain'),
                    'nis_servers': cleaned_data.get('ds_nis_servers'),
                    'nis_secure_mode': cleaned_data.get('ds_nis_secure_mode'),
                    'nis_manycast': cleaned_data.get('ds_nis_manycast'),
                    'nis_enable': True,
                })
                nisform = NISForm(
                    data=nisdata,
                    instance=nis,
                )
                if nisform.is_valid():
                    nisform.save()
                else:
                    log.warn(
                        'NIS data failed to validate: %r',
                        nisform._errors,
                    )

        # Change permission after joining directory service
        # since users/groups may not be local
        for i, share in enumerate(shares):
            if not share:
                continue

            share_name = share.get('share_name')
            share_purpose = share.get('share_purpose')
            share_user = share.get('share_user')
            share_group = share.get('share_group')
            share_mode = share.get('share_mode')

            if share_purpose == 'iscsitarget':
                continue

            if share_purpose == 'afp':
                share_acl = 'mac'
            elif share_purpose == 'cifs':
                share_acl = 'windows'
            else:
                share_acl = 'unix'

            _n.mp_change_permission(
                path='/mnt/%s/%s' % (volume_name, share_name),
                user=share_user,
                group=share_group,
                mode=share_mode,
                recursive=False,
                acl=share_acl,
            )

        curstep += 1
        progress['step'] = curstep
        progress['indeterminate'] = False
        progress['percent'] = 1
        with open(WIZARD_PROGRESSFILE, 'wb') as f:
            f.write(pickle.dumps(progress))

        # This must be outside transaction block to make sure the changes
        # are committed before the call of ix-fstab

        _n.reload("disk")  # Reloads collectd as well

        progress['percent'] = 50
        with open(WIZARD_PROGRESSFILE, 'wb') as f:
            f.write(pickle.dumps(progress))

        _n.start("ix-system")
        _n.restart("system_datasets")  # FIXME: may reload collectd again
        _n.reload("timeservices")

        progress['percent'] = 70
        with open(WIZARD_PROGRESSFILE, 'wb') as f:
            f.write(pickle.dumps(progress))

        _n.restart("cron")
        _n.reload("user")

        for service in services_restart:
            _n.restart(service)

        Update.CreateClone('Wizard-%s' % (
            datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        ))

        progress['percent'] = 100
        with open(WIZARD_PROGRESSFILE, 'wb') as f:
            f.write(pickle.dumps(progress))

        os.unlink(WIZARD_PROGRESSFILE)

        return JsonResp(
            self.request,
            message=__('Initial configuration succeeded.'),
            events=events,
        )


class ManualUpdateWizard(FileWizard):

    def get_template_names(self):
        return [
            'system/manualupdate_wizard_%s.html' % self.get_step_index(),
        ]

    def render_done(self, form, **kwargs):
        """
        XXX: method copied from base class so storage is not reset
        and files get removed.
        """
        final_forms = OrderedDict()
        # walk through the form list and try to validate the data again.
        for form_key in self.get_form_list():
            form_obj = self.get_form(
                step=form_key,
                data=self.storage.get_step_data(form_key),
                files=self.storage.get_step_files(form_key))
            if not form_obj.is_valid():
                return self.render_revalidation_failure(form_key,
                                                        form_obj,
                                                        **kwargs)
            final_forms[form_key] = form_obj

        # render the done view and reset the wizard before returning the
        # response. This is needed to prevent from rendering done with the
        # same data twice.
        done_response = self.done(final_forms.values(),
                                  form_dict=final_forms,
                                  **kwargs)
        # self.storage.reset()
        return done_response

    def done(self, form_list, **kwargs):
        global LEGACY_MANUAL_UPGRADE
        cleaned_data = self.get_all_cleaned_data()
        updatefile = cleaned_data.get('updatefile')

        _n = notifier()
        path = self.file_storage.path(updatefile.file.name)

        try:
            if not _n.is_freenas() and _n.failover_licensed():
                try:
                    with client as c:
                        c.call('failover.call_remote', 'notifier.create_upload_location')
                        _n.sync_file_send_v2(c, path, '/var/tmp/firmware/update.tar.xz')
                        try:
                            os.unlink(path)
                        except OSError:
                            pass
                        uuid = c.call('failover.call_remote', 'update.manual', ['/var/tmp/firmware/update.tar.xz'])
                except ClientException as e:
                    if e.errno not in (ClientException.ENOMETHOD, errno.ECONNREFUSED) and (e.trace is None or e.trace['class'] not in ('KeyError', 'ConnectionRefusedError')):
                        raise

                    # XXX: ugly hack to run the legacy upgrade in a thread
                    # so we can check progress in another view
                    class PerformLegacyUpgrade(threading.Thread):

                        def __init__(self, *args, **kwargs):
                            self.exception = None
                            self.path = kwargs.pop('path')
                            super().__init__(*args, **kwargs)

                        def run(self):
                            try:
                                _n = notifier()
                                s = _n.failover_rpc(timeout=10)
                                s.notifier('create_upload_location', None, None)
                                _n.sync_file_send(s, self.path, '/var/tmp/firmware/update.tar.xz', legacy=True)
                                s.update_manual('/var/tmp/firmware/update.tar.xz')

                                try:
                                    s.reboot()
                                except Exception:
                                    pass
                            except Exception as e:
                                self.exception = e

                    t = PerformLegacyUpgrade(path=path)
                    t.start()
                    LEGACY_MANUAL_UPGRADE = t
                    uuid = 0  # Hardcode 0 when using legacy upgrade
            else:
                with client as c:
                    uuid = c.call('update.manual', path)
                self.request.session['allow_reboot'] = True
            return HttpResponse(content=f'<html><body><textarea>{uuid}</textarea></boby></html>', status=202)
        except Exception:
            try:
                self.file_storage.delete(updatefile.name)
            except Exception:
                log.warn('Failed to delete uploaded file', exc_info=True)
            raise


class SettingsForm(MiddlewareModelForm, ModelForm):

    middleware_attr_prefix = 'stg_'
    middleware_attr_schema = 'general_settings'
    middleware_plugin = 'system.general'
    is_singletone = True
    middleware_attr_map = {
        'ui_address': 'stg_guiaddress',
        'ui_certificate': 'stg_guicertificate',
        'ui_httpsport': 'stg_guihttpsport',
        'ui_httpsredirect': 'stg_guihttpsredirect',
        'ui_port': 'stg_guiport',
        'ui_v6address': 'stg_guiv6address'
    }

    class Meta:
        fields = '__all__'
        model = models.Settings
        widgets = {
            'stg_timezone': forms.widgets.FilteringSelect(),
            'stg_language': forms.widgets.FilteringSelect(),
            'stg_kbdmap': forms.widgets.FilteringSelect(),
            'stg_guiport': forms.widgets.TextInput(),
            'stg_guihttpsport': forms.widgets.TextInput(),
        }

    def __init__(self, *args, **kwargs):
        super(SettingsForm, self).__init__(*args, **kwargs)
        self.original_instance = dict(self.instance.__dict__)

        self.fields['stg_language'].choices = settings.LANGUAGES
        self.fields['stg_language'].label = _("Language (Require UI reload)")
        self.fields['stg_guiaddress'] = forms.MultipleChoiceField(
            label=self.fields['stg_guiaddress'].label,
            required=False
        )
        ipv4_choices = list(choices.IPChoices(ipv6=False))
        ipv6_choices = list(choices.IPChoices(ipv4=False))

        self.fields['stg_guiv6address'] = forms.MultipleChoiceField(
            label=self.fields['stg_guiv6address'].label,
            required=False
        )

        for key, value, choice in [
            ('stg_guiaddress', ('0.0.0.0', '0.0.0.0'), ipv4_choices), ('stg_guiv6address', ('::', '::'), ipv6_choices)
        ]:
            self.fields[key].choices = choice if value in choice else [value] + choice

    def middleware_clean(self, update):
        keys = update.keys()
        for key in keys:
            if key.startswith('gui'):
                update['ui_' + key[3:]] = update.pop(key)

        update['sysloglevel'] = update['sysloglevel'].upper()
        for key in ('ui_address', 'ui_v6address'):
            if not update.get(key):
                update.pop(key, None)
        return update

    def done(self, request, events):
        cache.set('guiLanguage', self.instance.stg_language)

        if (
            self.original_instance['stg_guiaddress'] != self.instance.stg_guiaddress or
            self.original_instance['stg_guiport'] != self.instance.stg_guiport or
            self.original_instance['stg_guihttpsport'] != self.instance.stg_guihttpsport or
            self.original_instance['stg_guihttpsredirect'] != self.instance.stg_guihttpsredirect or
            self.original_instance['stg_guicertificate_id'] != self.instance.stg_guicertificate_id
        ):
            if "0.0.0.0" in self.instance.stg_guiaddress:
                address = request.META['HTTP_HOST'].split(':')[0]
            else:
                address = self.instance.stg_guiaddress[0]
            if not self.instance.stg_guihttpsredirect:
                protocol = 'http'
            else:
                protocol = 'https'

            newurl = "%s://%s/legacy/" % (
                protocol,
                address
            )

            if self.instance.stg_guiport and not self.instance.stg_guihttpsredirect:
                newurl += ":" + str(self.instance.stg_guiport)
            elif self.instance.stg_guihttpsport and self.instance.stg_guihttpsredirect:
                newurl += ":" + str(self.instance.stg_guihttpsport)

            if self.original_instance['stg_guihttpsredirect'] != self.instance.stg_guihttpsredirect:
                events.append("evilrestartHttpd('%s')" % newurl)
            else:
                events.append("restartHttpd('%s')" % newurl)

        if self.original_instance['stg_timezone'] != self.instance.stg_timezone:
            os.environ['TZ'] = self.instance.stg_timezone
            time.tzset()
            timezone.activate(self.instance.stg_timezone)


class NTPForm(MiddlewareModelForm, ModelForm):

    force = forms.BooleanField(
        label=_("Force"),
        required=False,
        help_text=_(
            "Continue operation if the server could not be reached/validated."
        ),
    )

    middleware_attr_prefix = "ntp_"
    middleware_attr_schema = "ntp"
    middleware_plugin = "system.ntpserver"
    is_singletone = False

    class Meta:
        fields = '__all__'
        model = models.NTPServer

    def middleware_clean(self, data):
        data['force'] = self.cleaned_data.get('force')

        return data


class AdvancedForm(MiddlewareModelForm, ModelForm):

    middleware_attr_prefix = 'adv_'
    middleware_attr_schema = 'advanced_settings'
    middleware_plugin = 'system.advanced'
    is_singletone = True

    adv_reset_sed_password = forms.BooleanField(
        label='Reset SED Password',
        required=False,
        initial=False,
        help_text=_('Click this box to reset global SED password'),
    )

    class Meta:
        exclude = ('adv_uploadcrash', )
        fields = '__all__'
        model = models.Advanced
        widgets = {
            'adv_sed_passwd': forms.widgets.PasswordInput(render_value=False),
            'adv_boot_scrub': forms.widgets.HiddenInput()
        }

    def __init__(self, *args, **kwargs):
        super(AdvancedForm, self).__init__(*args, **kwargs)
        self.fields['adv_motd'].strip = False
        self.original_instance = self.instance.__dict__

        self.fields['adv_serialport'].choices = list(choices.SERIAL_CHOICES())

        self.fields['adv_reset_sed_password'].widget.attrs['onChange'] = (
            'toggleGeneric("id_adv_reset_sed_password", ["id_adv_sed_passwd"], false);'
        )

    def clean(self):

        cdata = self.cleaned_data
        if cdata.get('adv_reset_sed_password'):
            cdata['adv_sed_passwd'] = ''
        elif not cdata.get('adv_sed_passwd'):
            cdata['adv_sed_passwd'] = self.instance.adv_sed_passwd

        return cdata

    def middleware_clean(self, data):

        data.pop('reset_sed_password', None)

        if data.get('sed_user'):
            data['sed_user'] = data['sed_user'].upper()

        return data

    def done(self, request, events):
        if self.instance.adv_consolemsg:
            events.append("_msg_start()")
        else:
            events.append("_msg_stop()")

        if self.original_instance['adv_advancedmode'] != self.instance.adv_advancedmode:
            # Invalidate cache
            request.session.pop("adv_mode", None)
        if (
            self.original_instance['adv_autotune'] != self.instance.adv_autotune and
            self.instance.adv_autotune is True
        ):
            events.append("refreshTree()")


class EmailForm(MiddlewareModelForm, ModelForm):

    middleware_attr_prefix = "em_"
    middleware_attr_schema = "mail"
    middleware_plugin = "mail"
    is_singletone = True

    em_pass1 = forms.CharField(
        label=_("Password"),
        widget=forms.PasswordInput,
        required=False,
        help_text=_('Enter the password for the SMTP server. Only ASCII valid characters are accepted.')
    )
    em_pass2 = forms.CharField(
        label=_("Password confirmation"),
        widget=forms.PasswordInput,
        help_text=_('Verify the SMTP server password.'),
        required=False
    )

    class Meta:
        model = models.Email
        exclude = ('em_pass',)

    def __init__(self, *args, **kwargs):
        super(EmailForm, self).__init__(*args, **kwargs)
        try:
            self.fields['em_pass1'].initial = self.instance.em_pass
            self.fields['em_pass2'].initial = self.instance.em_pass
        except Exception:
            pass
        self.fields['em_smtp'].widget.attrs['onChange'] = (
            'toggleGeneric("id_em_smtp", ["id_em_pass1", "id_em_pass2", '
            '"id_em_user"], true);'
        )
        ro = True

        if len(self.data) > 0:
            if self.data.get("em_smtp", None) == "on":
                ro = False
        else:
            if self.instance.em_smtp is True:
                ro = False
        if ro:
            self.fields['em_user'].widget.attrs['disabled'] = 'disabled'
            self.fields['em_pass1'].widget.attrs['disabled'] = 'disabled'
            self.fields['em_pass2'].widget.attrs['disabled'] = 'disabled'

    def clean_em_pass1(self):
        if (
            self.cleaned_data['em_smtp'] is True and
            self.cleaned_data['em_pass1'] == ""
        ):
            if self.instance.em_pass:
                return self.instance.em_pass
            raise forms.ValidationError(_("This field is required"))
        return self.cleaned_data['em_pass1']

    def clean_em_pass2(self):
        if (
            self.cleaned_data['em_smtp'] is True and
            self.cleaned_data.get('em_pass2', "") == ""
        ):
            if self.instance.em_pass:
                return self.instance.em_pass
            raise forms.ValidationError(_("This field is required"))
        pass1 = self.cleaned_data.get("em_pass1", "")
        pass2 = self.cleaned_data.get("em_pass2", "")
        if pass1 != pass2:
            raise forms.ValidationError(
                _("The two password fields didn't match.")
            )
        return pass2

    def clean(self):
        if 'em_pass1' in self.cleaned_data:
            self.cleaned_data['em_pass'] = self.cleaned_data.pop('em_pass1')
            if 'em_pass2' in self.cleaned_data:
                del self.cleaned_data['em_pass2']
        return self.cleaned_data

    def middleware_clean(self, update):
        update['security'] = update['security'].upper()
        return update


class ManualUpdateTemporaryLocationForm(Form):
    mountpoint = forms.ChoiceField(
        label=_("Location to temporarily store update file"),
        help_text=_(
            "The update file is temporarily stored here "
            "before being applied."),
        choices=(),
        widget=forms.Select(attrs={'class': 'required'}),
    )

    def clean_mountpoint(self):
        mp = self.cleaned_data.get("mountpoint")
        if mp.startswith('/'):
            clean_path_execbit(mp)
        clean_path_locked(mp)
        return mp

    def __init__(self, *args, **kwargs):
        super(ManualUpdateTemporaryLocationForm, self).__init__(*args, **kwargs)
        self.fields['mountpoint'].choices = [
            (x.vol_path, x.vol_path)
            for x in Volume.objects.all()
        ]
        self.fields['mountpoint'].choices.append(
            (':temp:', _('Memory device'))
        )

    def done(self, *args, **kwargs):
        mp = str(self.cleaned_data["mountpoint"])
        if mp == ":temp:":
            notifier().create_upload_location()
        else:
            notifier().change_upload_location(mp)


class ManualUpdateUploadForm(Form):
    updatefile = FileField(label=_("Update file to be installed"), required=True)


class ConfigUploadForm(Form):
    config = FileField(label=_("New config to be installed"))


class ConfigSaveForm(Form):
    secret = forms.BooleanField(
        label=_('Export Password Secret Seed'),
        initial=False,
        required=False,
    )


"""
TODO: Move to a unittest .py file.

invalid_sysctls = [
    'a.0',
    'a.b',
    'a..b',
    'a._.b',
    'a.b._.c',
    '0',
    '0.a',
    'a-b',
    'a',
]

valid_sysctls = [
    'ab.0',
    'ab.b',
    'smbios.system.version',
    'dev.emu10kx.0.multichannel_recording',
    'hw.bce.tso0',
    'kern.sched.preempt_thresh',
    'net.inet.tcp.tso',
]

assert len(filter(SYSCTL_VARNAME_FORMAT_RE.match, invalid_sysctls)) == 0
assert len(
    filter(SYSCTL_VARNAME_FORMAT_RE.match, valid_sysctls)) == len(valid_sysctls
)
"""

# NOTE:
# - setenv in the kernel is more permissive than this, but we want to reduce
#   user footshooting.
# - this doesn't reject all benign input; it just rejects input that would
#   break system boots.
# XXX: note that I'm explicitly rejecting input for root sysctl nodes.
SYSCTL_TUNABLE_VARNAME_FORMAT = """Sysctl variable names must:<br />
1. Start with a letter.<br />
2. Contain at least one period.<br />
3. End with a letter or number.<br />
4. Can contain a combination of alphanumeric characters, numbers and/or underscores.
"""
SYSCTL_VARNAME_FORMAT_RE = \
    re.compile(r'[a-z][a-z0-9_]+\.([a-z0-9_]+\.)*[a-z0-9_]+', re.I)

LOADER_VARNAME_FORMAT_RE = \
    re.compile(r'[a-z][a-z0-9_]+\.*([a-z0-9_]+\.)*[a-z0-9_]+', re.I)


class TunableForm(MiddlewareModelForm, ModelForm):

    class Meta:
        fields = '__all__'
        model = models.Tunable

    middleware_attr_prefix = "tun_"
    middleware_attr_schema = "tunable"
    middleware_plugin = "tunable"
    is_singletone = False

    def middleware_clean(self, data):
        data['type'] = data['type'].upper()
        return data


class AlertServiceSettingsWidget(forms.Widget):
    def __init__(self, allow_inherit):
        self.allow_inherit = allow_inherit

        super().__init__()

    def render(self, name, value, attrs=None, renderer=None):
        value = value or {}

        html = []
        html.append('<table>')

        with client as c:
            policies = c.call('alert.list_policies')
            sources = c.call('alert.list_sources')

        for source in sources:
            html.append('<tr>')

            html.append(f'<td>{source["title"]}</td>')

            html.append('<td>')
            html.append(f'<select dojoType="dijit.form.Select" id="id_{name}_{source["name"]}" '
                        f'name="{name}[{source["name"]}]">')
            options = [(policy, policy.title()) for policy in policies]
            if self.allow_inherit:
                options = [("", "Inherit")] + options
            for k, v in options:
                selected = ""
                if value.get(source["name"], "") == k:
                    selected = "selected=\"selected\""
                html.append(f'<option value="{k}" {selected}>{v}</option>')
            html.append('</select>')
            html.append('</td>')

            html.append('</tr>')

        html.append('</table>')

        html.append(f'<input type="hidden" name="{name}" value="{{}}" data-dojo-type="dijit.form.TextBox" />')

        return mark_safe("".join(html))

    def value_from_datadict(self, data, files, name):
        """
        Given a dictionary of data and this widget's name, return the value
        of this widget or None if it's not provided.
        """
        r = fr"{name}\[(.+)\]"
        return json.dumps({re.match(r, k).group(1): v for k, v in data.items() if re.match(r, k) and v})


class AlertDefaultSettingsForm(MiddlewareModelForm, ModelForm):

    middleware_attr_prefix = ""
    middleware_attr_schema = "alert_default_settings"
    middleware_plugin = "alertdefaultsettings"
    is_singletone = True

    settings = forms.CharField(
        widget=AlertServiceSettingsWidget(allow_inherit=False),
    )

    class Meta:
        fields = '__all__'
        model = models.AlertDefaultSettings


class AlertServiceForm(MiddlewareModelForm, ModelForm):

    middleware_attr_prefix = ""
    middleware_attr_schema = "alert_service"
    middleware_plugin = "alertservice"
    is_singletone = False

    middleware_exclude_fields = ["send_test_alert"]

    type = forms.ChoiceField(
        choices=(),
    )

    # Common fields between all API
    username = forms.CharField(
        max_length=255,
        label=_("Username"),
        help_text=_("The username to use with this service"),
        required=False,
    )
    password = forms.CharField(
        max_length=255,
        label=_("Password"),
        help_text=_("Password"),
        required=False,
    )
    cluster_name = forms.CharField(
        max_length=255,
        label=_("Cluster name"),
        help_text=_("The name of the cluster"),
        required=False,
    )
    url = forms.CharField(
        max_length=255,
        label=_("Webhook URL"),
        help_text=_("The incoming webhook URL asssociated with this service"),
        required=False,
    )

    # Influxdb
    host = forms.CharField(
        max_length=255,
        label=_("Host"),
        help_text=_("Influxdb Host"),
        required=False,
    )
    database = forms.CharField(
        max_length=255,
        label=_("Database"),
        help_text=_("Influxdb database name"),
        required=False,
    )
    series_name = forms.CharField(
        max_length=255,
        label=_("Series"),
        help_text=_("Influxdb series name for the points"),
        required=False,
    )

    # Slack
    channel = forms.CharField(
        max_length=255,
        label=_("Channel"),
        help_text=_("The channel to post notifications to. This overides the default channel in the Incoming Webhook settings."),
        required=False,
    )
    icon_url = forms.CharField(
        max_length=255,
        label=_("Icon URL"),
        help_text=_("URL of a custom image for notification icons. This overrides the default if set in the Incoming Webhook settings."),
        required=False,
    )
    detailed = forms.BooleanField(
        label=_("Detailed"),
        help_text=_("Enable pretty Slack notifications"),
        initial=False,
        required=False,
    )

    # Mattermost
    team = forms.CharField(
        max_length=255,
        label=_("Team"),
        help_text=_("The mattermost team"),
        required=False,
    )

    # PagerDuty
    service_key = forms.CharField(
        max_length=255,
        label=_("Service key"),
        help_text=_("Service key to access PagerDuty"),
        required=False,
    )
    client_name = forms.CharField(
        max_length=255,
        label=_("Client name"),
        help_text=_("The monitoring client name"),
        required=False,
    )

    # HipChat
    hfrom = forms.CharField(
        max_length=20,
        label=_("From"),
        help_text=_("The name to send notification"),
        required=False,
    )
    base_url = forms.CharField(
        max_length=255,
        initial="https://api.hipchat.com/v2/",
        label=_("URL"),
        help_text=_("HipChat base url"),
        required=False,
    )
    room_id = forms.CharField(
        max_length=50,
        label=_("Room"),
        help_text=_("The room to post to"),
        required=False,
    )
    auth_token = forms.CharField(
        max_length=255,
        label=_("Token"),
        help_text=_("Authentication token"),
        required=False,
    )

    # OpsGenie
    api_key = forms.CharField(
        max_length=255,
        label=_("API Key"),
        help_text=_("API Key"),
        required=False,
    )

    # AWS SNS
    region = forms.CharField(
        max_length=255,
        label=_("Region"),
        help_text=_("AWS Region"),
        required=False,
    )
    topic_arn = forms.CharField(
        max_length=255,
        label=_("ARN"),
        help_text=_("Topic ARN to publish to"),
        required=False,
    )
    aws_access_key_id = forms.CharField(
        max_length=255,
        label=_("Key Id"),
        help_text=_("AWS Access Key Id"),
        required=False,
    )
    aws_secret_access_key = forms.CharField(
        max_length=255,
        label=_("Secret Key"),
        help_text=_("AWS Secret Access Key"),
        required=False,
    )

    # VictorOps
    routing_key = forms.CharField(
        max_length=255,
        label=_("Routing Key"),
        help_text=_("Routing Key"),
        required=False,
    )

    # Mail
    email = forms.EmailField(
        label=_('E-mail address'),
        help_text=_('Leave empty for system global e-mail address.'),
        required=False,
    )

    settings = forms.CharField(
        widget=AlertServiceSettingsWidget(allow_inherit=True),
    )

    send_test_alert = forms.CharField(
        required=False,
        widget=forms.widgets.HiddenInput(),
    )

    types_fields = {
        'AWSSNS': ['region', 'topic_arn', 'aws_access_key_id', 'aws_secret_access_key'],
        'HipChat': ['hfrom', 'cluster_name', 'base_url', 'room_id', 'auth_token'],
        'InfluxDB': ['host', 'username', 'password', 'database', 'series_name'],
        'Mattermost': ['cluster_name', 'url', 'username', 'password', 'team', 'channel'],
        'Mail': ['email'],
        'OpsGenie': ['cluster_name', 'api_key'],
        'PagerDuty': ['service_key', 'client_name'],
        'Slack': ['cluster_name', 'url', 'channel', 'username', 'icon_url', 'detailed'],
        'SNMPTrap': [],
        'VictorOps': ['api_key', 'routing_key'],
    }

    class Meta:
        fields = '__all__'
        model = models.AlertService

    def __init__(self, *args, **kwargs):
        super(AlertServiceForm, self).__init__(*args, **kwargs)
        with client as c:
            self.fields['type'].choices = [(t["name"], t["title"]) for t in c.call('alertservice.list_types')]
        self.fields['type'].widget.attrs['onChange'] = (
            "alertServiceTypeToggle();"
        )
        key_order(self, len(self.fields) - 2, 'enabled', instance=True)
        key_order(self, len(self.fields) - 1, 'settings', instance=True)

        if self.instance.id:
            for k in self.types_fields.get(self.instance.type, []):
                self.fields[k].initial = self.instance.attributes.get(k)

    def save(self):
        if self.cleaned_data.get("send_test_alert") == "1":
            data = self.middleware_prepare()

            self._middleware_action = "test"

            with client as c:
                if c.call(f"{self.middleware_plugin}.test", data):
                    msg = "Test alert sent successfully"
                else:
                    msg = "Test alert sending failed"

                raise ValidationErrors([["__all__", msg, errno.EINVAL]])

        return super().save()

    def middleware_clean(self, data):
        data["attributes"] = {k: data[k] for k in self.types_fields[data["type"]]}
        for k in sum(self.types_fields.values(), []):
            data.pop(k, None)
        return data

    def delete(self):
        with client as c:
            c.call("alert_service.delete", self.instance.id)


class SystemDatasetForm(MiddlewareModelForm, ModelForm):

    middleware_attr_prefix = "sys_"
    middleware_attr_schema = "sysdataset_update"
    middleware_plugin = "systemdataset"
    middleware_job = True
    is_singletone = True

    sys_pool = forms.ChoiceField(
        label=_("System dataset pool"),
        required=False
    )

    class Meta:
        fields = '__all__'
        model = models.SystemDataset

    def __init__(self, *args, **kwargs):
        super(SystemDatasetForm, self).__init__(*args, **kwargs)
        pool_choices = [('', ''), ('freenas-boot', 'freenas-boot')]
        for v in Volume.objects.all():
            if v.is_decrypted():
                pool_choices.append((v.vol_name, v.vol_name))

        self.fields['sys_pool'].choices = pool_choices
        self.fields['sys_pool'].widget.attrs['onChange'] = (
            "systemDatasetMigration();"
        )

    def middleware_clean(self, update):
        update['syslog'] = update.pop('syslog_usedataset')
        return update


class ReportingForm(MiddlewareModelForm, ModelForm):

    middleware_attr_prefix = ""
    middleware_attr_schema = "reporting"
    middleware_plugin = "reporting"
    is_singletone = True

    confirm_rrd_destroy = forms.BooleanField(
        label=_("Confirm reporting database will be destroyed"),
        required=False,
    )

    class Meta:
        fields = '__all__'
        model = models.Reporting

    def __init__(self, *args, **kwargs):
        super(ReportingForm, self).__init__(*args, **kwargs)

        self.fields["graph_age"].widget.attrs['onchange'] = "confirmRrdDestroyShow();"
        self.fields["graph_points"].widget.attrs['onchange'] = "confirmRrdDestroyShow();"


class InitialWizardDSForm(Form):

    ds_type = forms.ChoiceField(
        label=_('Directory Service'),
        choices=(
            ('ad', _('Active Directory')),
            ('ldap', _('LDAP')),
            ('nis', _('NIS')),
        ),
        initial='ad',
    )
    ds_ad_domainname = forms.CharField(
        label=_('Domain Name (DNS/Realm-Name)'),
        required=False,
    )
    ds_ad_bindname = forms.CharField(
        label=_('Domain Account Name'),
        required=False,
    )
    ds_ad_bindpw = forms.CharField(
        label=_('Domain Account Password'),
        required=False,
        widget=forms.widgets.PasswordInput(),
    )
    ds_ldap_hostname = forms.CharField(
        label=_('Hostname'),
        required=False,
    )
    ds_ldap_basedn = forms.CharField(
        label=('Base DN'),
        required=False,
    )
    ds_ldap_binddn = forms.CharField(
        label=('Bind DN'),
        required=False,
    )
    ds_ldap_bindpw = forms.CharField(
        label=('Base Password'),
        required=False,
        widget=forms.widgets.PasswordInput(),
    )

    def __init__(self, *args, **kwargs):
        super(InitialWizardDSForm, self).__init__(*args, **kwargs)

        for fname, field in list(NISForm().fields.items()):
            if fname.find('enable') == -1:
                self.fields['ds_%s' % fname] = field
                field.required = False

        pertype = defaultdict(list)
        for fname in list(self.fields.keys()):
            if fname.startswith('ds_ad_'):
                pertype['ad'].append('id_ds-%s' % fname)
            elif fname.startswith('ds_ldap_'):
                pertype['ldap'].append('id_ds-%s' % fname)
            elif fname.startswith('ds_nis_'):
                pertype['nis'].append('id_ds-%s' % fname)

        self.jsChange = 'genericSelectFields(\'%s\', \'%s\');' % (
            'id_ds-ds_type',
            escapejs(json.dumps(pertype)),
        )
        self.fields['ds_type'].widget.attrs['onChange'] = (
            self.jsChange
        )

    @classmethod
    def show_condition(cls, wizard):
        ad = ActiveDirectory.objects.all().filter(ad_enable=True).exists()
        ldap = LDAP.objects.all().filter(ldap_enable=True).exists()
        nis = NIS.objects.all().filter(nis_enable=True).exists()
        return not(ad or ldap or nis)

    def clean(self):
        cdata = self.cleaned_data

        if cdata.get('ds_type') == 'ad':
            domain = cdata.get('ds_ad_domainname')
            bindname = cdata.get('ds_ad_bindname')
            bindpw = cdata.get('ds_ad_bindpw')
            binddn = '%s@%s' % (bindname, domain)

            if not (domain and bindname and bindpw):
                if domain or bindname or bindpw:
                    if not domain:
                        self._errors['ds_ad_domainname'] = self.error_class([
                            _('This field is required.'),
                        ])

                    if not bindname:
                        self._errors['ds_ad_bindname'] = self.error_class([
                            _('This field is required.'),
                        ])

                    if not bindpw:
                        self._errors['ds_ad_bindpw'] = self.error_class([
                            _('This field is required.'),
                        ])
                else:
                    cdata.pop('ds_type', None)
            else:

                try:
                    FreeNAS_ActiveDirectory.validate_credentials(
                        domain, binddn=binddn, bindpw=bindpw
                    )
                except LDAPError as e:
                    # LDAPError is dumb, it returns a list with one element for goodness knows what reason
                    if not hasattr(e, '__iter__'):
                        raise forms.ValidationError("{0}".format(e))
                    e = e[0]
                    error = []
                    desc = e.get('desc')
                    info = e.get('info')
                    if desc:
                        error.append(desc)
                    if info:
                        error.append(info)

                    if error:
                        error = ', '.join(error)
                    else:
                        error = str(e)

                    raise forms.ValidationError("{0}".format(error))
                except Exception as e:
                    raise forms.ValidationError("{0}".format(e))

        elif cdata.get('ds_type') == 'ldap':
            hostname = cdata.get('ds_ldap_hostname')
            binddn = cdata.get('ds_ldap_binddn')
            bindpw = cdata.get('ds_ldap_bindpw')

            if not (hostname and binddn and bindpw):
                if hostname or binddn or bindpw:
                    if not hostname:
                        self._errors['ds_ldap_hostname'] = self.error_class([
                            _('This field is required.'),
                        ])

                    if not binddn:
                        self._errors['ds_ldap_binddn'] = self.error_class([
                            _('This field is required.'),
                        ])

                    if not bindpw:
                        self._errors['ds_ldap_bindpw'] = self.error_class([
                            _('This field is required.'),
                        ])
                else:
                    cdata.pop('ds_type', None)
            else:
                try:
                    FreeNAS_LDAP.validate_credentials(hostname, binddn=binddn, bindpw=bindpw)
                except LDAPError as e:
                    # LDAPError is dumb, it returns a list with one element for goodness knows what reason
                    if not hasattr(e, '__iter__'):
                        raise forms.ValidationError("{0}".format(e))
                    e = e[0]
                    error = []
                    desc = e.get('desc')
                    info = e.get('info')
                    if desc:
                        error.append(desc)
                    if info:
                        error.append(info)

                    if error:
                        error = ', '.join(error)
                    else:
                        error = str(e)

                    raise forms.ValidationError("{0}".format(error))
                except Exception as e:
                    raise forms.ValidationError("{0}".format(str(e)))

        elif cdata.get('ds_type') == 'nis':
            values = []
            empty = True
            for fname in list(self.fields.keys()):
                if fname.startswith('ds_nis_'):
                    value = cdata.get(fname)
                    values.append((fname, value))
                    if empty and value:
                        empty = False

            if not empty:
                for fname, value in values:
                    if not value and NISForm.base_fields[
                        fname.replace('ds_', '')
                    ].required:
                        self._errors[fname] = self.error_class([
                            _('This field is required.'),
                        ])

        return cdata

    def done(self, request=None, **kwargs):
        dsdata = self.cleaned_data
        # Save Directory Service (DS) in the session so we can retrieve DS
        # users and groups for the Ownership screen
        request.session['wizard_ds'] = dsdata


class InitialWizardShareForm(Form):

    share_name = forms.CharField(
        label=_('Share Name'),
        max_length=80,
    )
    share_purpose = forms.ChoiceField(
        label=_('Purpose'),
        choices=(
            ('cifs', _('Windows (SMB)')),
            ('afp', _('Apple (AFP)')),
            ('nfs', _('Unix (NFS)')),
            ('iscsitarget', _('Block Storage (iSCSI)')),
        ),
    )
    share_allowguest = forms.BooleanField(
        label=_('Allow Guest'),
        required=False,
    )
    share_timemachine = forms.BooleanField(
        label=_('Time Machine'),
        required=False,
    )
    share_iscsisize = forms.CharField(
        label=_('iSCSI Size'),
        max_length=255,
        required=False,
    )
    share_user = forms.CharField(
        max_length=100,
        required=False,
    )
    share_group = forms.CharField(
        max_length=100,
        required=False,
    )
    share_usercreate = forms.BooleanField(
        required=False,
    )
    share_userpw = forms.CharField(
        required=False,
        max_length=100,
    )
    share_groupcreate = forms.BooleanField(
        required=False,
    )
    share_mode = forms.CharField(
        max_length=3,
        required=False,
    )

    def clean_share_name(self):
        share_name = self.cleaned_data.get('share_name')
        qs = Volume.objects.all()
        if qs.exists():
            volume_name = qs[0].vol_name
            path = '/mnt/%s/%s' % (volume_name, share_name)
            if os.path.exists(path):
                raise forms.ValidationError(
                    _('Share path %s already exists.') % path
                )
        return share_name


class SharesBaseFormSet(BaseFormSet):

    RE_FIELDS = re.compile(r'^shares-(\d+)-share_(.+)$')

    def data_to_store(self):
        """
        Returns an array suitable to use in the dojo memory store.
        """
        keys = defaultdict(dict)
        for key, val in list(self.data.items()):
            reg = self.RE_FIELDS.search(key)
            if not reg:
                continue
            idx, name = reg.groups()
            keys[idx][name] = val

        return json.dumps(list(keys.values()))

    def errors_json(self):
        return json.dumps(self.errors)


InitialWizardShareFormSet = formset_factory(
    InitialWizardShareForm,
    formset=SharesBaseFormSet,
)


class InitialWizardVolumeForm(VolumeMixin, Form):

    volume_name = forms.CharField(
        label=_('Volume Name'),
        max_length=200,
    )
    volume_type = forms.ChoiceField(
        label=_('Type'),
        choices=(),
        widget=forms.RadioSelect,
        initial='auto',
    )

    def __init__(self, *args, **kwargs):
        super(InitialWizardVolumeForm, self).__init__(*args, **kwargs)
        self.fields['volume_type'].choices = (
            (
                'auto',
                _('Automatic (Reasonable defaults using the available drives)')
            ),
            (
                'raid10',
                _('Virtualization (RAID 10: Moderate Redundancy, Maximum Performance, Minimum Capacity)')
            ),
            (
                'raidz2',
                _('Backups (RAID Z2: Moderate Redundancy, Moderate Performance, Moderate Capacity)')
            ),
            (
                'raidz1',
                _('Media (RAID Z1: Minimum Redundancy, Moderate Performance, Moderate Capacity)')
            ),
            (
                'stripe',
                _('Logs (RAID 0: No Redundancy, Maximum Performance, Maximum Capacity)')
            ),
        )

        self.types_avail = self._types_avail(self._get_unused_disks_by_size())

    @classmethod
    def show_condition(cls, wizard):
        imported = wizard.get_cleaned_data_for_step('import')
        if imported:
            return False
        has_disks = (
            len(cls._types_avail(cls._get_unused_disks_by_size())) > 0
        )
        volume_exists = Volume.objects.all().exists()
        return has_disks or (not has_disks and not volume_exists)

    @staticmethod
    def _get_unused_disks():
        return notifier().get_disks(unused=True)

    @classmethod
    def _get_unused_disks_by_size(cls):
        disks = cls._get_unused_disks()
        bysize = defaultdict(list)
        for disk, attrs in list(disks.items()):
            size = int(attrs['capacity'])
            # Some disks might have a few sectors of difference.
            # We still want to group them together.
            # This is not ideal but good enough for now.
            size = size - (size % 10000)
            bysize[size].append(disk)
        return bysize

    @staticmethod
    def _higher_disks_group(bysize):
        higher = (None, 0)
        for size, _disks in list(bysize.items()):
            if len(_disks) > higher[1]:
                higher = (size, len(_disks))
        return higher

    @classmethod
    def _types_avail(cls, disks):
        types = []
        ndisks = cls._higher_disks_group(disks)[1]
        if ndisks >= 4:
            types.extend(['raid10', 'raidz2'])
        if ndisks >= 3:
            types.append('raidz1')
        if ndisks > 0:
            types.extend(['auto', 'stripe'])
        return types

    @staticmethod
    def _grp_type(num):
        check = OrderedDict((
            ('MIRROR', lambda y: y == 2),
            (
                'RAIDZ1',
                lambda y: False if y < 3 else math.log(y - 1, 2) % 1 == 0
            ),
            (
                'RAIDZ2',
                lambda y: False if y < 4 else math.log(y - 2, 2) % 1 == 0
            ),
            (
                'RAIDZ3',
                lambda y: False if y < 5 else math.log(y - 3, 2) % 1 == 0
            ),
            ('STRIPE', lambda y: True),
        ))
        for name, func in list(check.items()):
            if func(num):
                return name
        return 'STRIPE'

    @classmethod
    def _grp_autoselect(cls, disks):

        higher = cls._higher_disks_group(disks)
        groups = defaultdict(list)

        for size, devs in [(higher[0], disks[higher[0]])]:
            num = len(devs)
            if num in (4, 8):
                mod = 0
                perrow = int(num / 2)
                rows = 2
                vdevtype = cls._grp_type(int(num / 2))
            elif num < 12:
                mod = 0
                perrow = num
                rows = 1
                vdevtype = cls._grp_type(num)
            elif num < 18:
                mod = num % 2
                rows = 2
                perrow = int((num - mod) / 2)
                vdevtype = cls._grp_type(perrow)
            elif num >= 18:
                div9 = int(num / 9)
                div10 = int(num / 10)
                mod9 = num % 9
                mod10 = num % 10

                if mod9 >= 0.75 * div9 and mod10 >= 0.75 * div10:
                    perrow = 9
                    rows = div9
                    mod = mod9
                else:
                    perrow = 10
                    rows = div10
                    mod = mod10

                vdevtype = cls._grp_type(perrow)
            else:
                perrow = num
                rows = 1
                vdevtype = 'stripe'
                mod = 0

            for i in range(rows):
                groups['data'].append({
                    'type': vdevtype,
                    'disks': devs[i * perrow:perrow * (i + 1)],
                })
            if mod > 0:
                groups['spares'] += devs[-mod:]
        return groups

    @classmethod
    def _grp_predefined(cls, disks, grptype):

        higher = cls._higher_disks_group(disks)

        maindisks = disks[higher[0]]

        groups = defaultdict(list)

        if grptype == 'raid10':
            for i in range(int(len(maindisks) / 2)):
                groups['data'].append({
                    'type': 'MIRROR',
                    'disks': maindisks[i * 2:2 * (i + 1)],
                })
        elif grptype.startswith('raidz'):
            if grptype == 'raidz':
                optimalrow = 9
                grptype = 'raidz1'
            elif grptype == 'raidz2':
                optimalrow = 10
            else:
                optimalrow = 11
            div = int(len(maindisks) / optimalrow)
            mod = len(maindisks) % optimalrow
            if mod >= 2:
                div += 1
            perrow = int(len(maindisks) / (div if div else 1))
            for i in range(div):
                groups['data'].append({
                    'type': grptype.upper(),
                    'disks': maindisks[i * perrow:perrow * (i + 1)],
                })
        else:
            groups['data'].append({
                'type': grptype.upper(),
                'disks': maindisks,
            })

        return groups

    def _get_disk_size(self, disk, bysize):
        size = None
        for _size, disks in list(bysize.items()):
            if disk in disks:
                size = _size
                break
        return size

    def _groups_to_disks_size(self, bysize, groups, swapsize):
        size = 0
        disks = []
        for group in list(groups['data']):
            lower = None
            for disk in group['disks']:
                _size = self._get_disk_size(disk, bysize)
                if _size and (not lower or lower > _size):
                    lower = _size - swapsize
            if not lower:
                continue

            if group['type'] == 'MIRROR':
                size += lower
            elif group['type'] == 'RAIDZ1':
                size += lower * (len(group['disks']) - 1)
            elif group['type'] == 'RAIDZ2':
                size += lower * (len(group['disks']) - 2)
            elif group['type'] == 'STRIPE':
                size += lower * len(group['disks'])
            disks.extend(group['disks'])
        return disks, humanize_size(size)

    def choices(self):
        swapsize = models.Advanced.objects.order_by('-id')[0].adv_swapondrive
        swapsize *= 1024 * 1024 * 1024
        types = defaultdict(dict)
        bysize = self._get_unused_disks_by_size()
        for _type, descr in self.fields['volume_type'].choices:
            if _type == 'auto':
                groups = self._grp_autoselect(bysize)
            else:
                groups = self._grp_predefined(bysize, _type)
            types[_type] = self._groups_to_disks_size(bysize, groups, swapsize)
        return json.dumps(types)

    def clean(self):
        volume_type = self.cleaned_data.get('volume_type')
        if not volume_type:
            self._errors['volume_type'] = self.error_class([
                _('No available disks have been found.'),
            ])
        return self.cleaned_data


class InitialWizardVolumeImportForm(VolumeAutoImportForm):

    @classmethod
    def show_condition(cls, wizard):
        volume = wizard.get_cleaned_data_for_step('volume')
        if volume and volume.get('volume_name'):
            return False
        if Volume.objects.all().exists():
            return False
        return len(cls._volume_choices()) > 0


class InitialWizardSettingsForm(Form):

    stg_language = forms.ChoiceField(
        choices=settings.LANGUAGES,
        label=_('Language'),
        required=False,
    )
    stg_kbdmap = forms.ChoiceField(
        choices=[('', '------')] + list(choices.KBDMAP_CHOICES()),
        label=_('Console Keyboard Map'),
        required=False,
    )
    stg_timezone = forms.ChoiceField(
        choices=choices.TimeZoneChoices(),
        label=_('Timezone'),
        required=False,
    )

    def __init__(self, *args, **kwargs):
        super(InitialWizardSettingsForm, self).__init__(*args, **kwargs)
        try:
            settingsm = models.Settings.objects.order_by('-id')[0]
        except IndexError:
            settingsm = models.Settings.objects.create()

        self.fields['stg_language'].initial = settingsm.stg_language
        self.fields['stg_kbdmap'].initial = settingsm.stg_kbdmap
        self.fields['stg_timezone'].initial = settingsm.stg_timezone

    def done(self, request=None, **kwargs):
        # Save selected language to be used in the wizard
        request.session['wizard_lang'] = self.cleaned_data.get('stg_language')


class InitialWizardSystemForm(Form):

    sys_console = forms.BooleanField(
        label=_('Console messages'),
        help_text=_('Show console messages in the footer.'),
        required=False,
    )
    sys_email = forms.EmailField(
        label=_('Root E-mail'),
        help_text=_('Administrative email address used for alerts.'),
        required=False,
    )

    def __init__(self, *args, **kwargs):
        super(InitialWizardSystemForm, self).__init__(*args, **kwargs)
        self._instance = models.Email.objects.order_by('-id')[0]
        for fname, field in list(EmailForm(instance=self._instance).fields.items()):
            self.fields[fname] = field
            field.initial = getattr(self._instance, fname, None)
            field.required = False

        with client as c:
            root_user = c.call(
                'user.query',
                [('username', '=', 'root')],
                {'get': True}
            )
            self.fields['sys_email'].initial = root_user['email']

        try:
            adv = models.Advanced.objects.order_by('-id')[0]
        except IndexError:
            adv = models.Advanced.objects.create()
        self.fields['sys_console'].initial = adv.adv_consolemsg
        self.fields['em_smtp'].widget.attrs['onChange'] = (
            'toggleGeneric("id_system-em_smtp", ["id_system-em_pass1", '
            '"id_system-em_pass2", "id_system-em_user"], true);'
        )

    def clean(self):
        em = EmailForm(self.cleaned_data, instance=self._instance)
        if self.cleaned_data.get('em_fromemail') and not em.is_valid():
            for fname, errors in list(em._errors.items()):
                self._errors[fname] = errors
        return self.cleaned_data


class InitialWizardConfirmForm(Form):
    pass


class UpdateForm(ModelForm):

    curtrain = forms.CharField(
        label=_('Current Train'),
        widget=forms.TextInput(attrs={'readonly': True, 'disabled': True}),
        required=False,
    )

    class Meta:
        fields = '__all__'
        model = models.Update

    def __init__(self, *args, **kwargs):
        super(UpdateForm, self).__init__(*args, **kwargs)
        self._conf = Configuration.Configuration()
        self._conf.LoadTrainsConfig()
        self.fields['curtrain'].initial = self._conf.CurrentTrain()


class CertificateAuthorityEditForm(MiddlewareModelForm, ModelForm):

    middleware_attr_prefix = 'cert_'
    middleware_attr_schema = 'certificate_authority'
    middleware_plugin = 'certificateauthority'
    is_singletone = False

    cert_name = forms.CharField(
        label=models.CertificateAuthority._meta.get_field('cert_name').verbose_name,
        required=True,
        help_text=models.CertificateAuthority._meta.get_field('cert_name').help_text
    )
    cert_certificate = forms.CharField(
        label=models.CertificateAuthority._meta.get_field('cert_certificate').verbose_name,
        widget=forms.Textarea(),
        help_text=models.CertificateAuthority._meta.get_field('cert_certificate').help_text
    )
    cert_privatekey = forms.CharField(
        label=models.CertificateAuthority._meta.get_field('cert_privatekey').verbose_name,
        widget=forms.Textarea(),
        help_text=models.CertificateAuthority._meta.get_field('cert_privatekey').help_text
    )

    def __init__(self, *args, **kwargs):
        super(CertificateAuthorityEditForm, self).__init__(*args, **kwargs)

        self.fields['cert_certificate'].widget.attrs['readonly'] = True
        self.fields['cert_privatekey'].widget.attrs['readonly'] = True

    def middleware_clean(self, data):
        return {'name': data['name']}

    class Meta:
        fields = [
            'cert_name',
            'cert_certificate',
            'cert_privatekey'
        ]
        model = models.CertificateAuthority


class CertificateAuthorityImportForm(MiddlewareModelForm, ModelForm):

    middleware_attr_prefix = 'cert_'
    middleware_attr_schema = 'certificate_authority'
    middleware_plugin = 'certificateauthority'
    is_singletone = False

    cert_name = forms.CharField(
        label=models.CertificateAuthority._meta.get_field('cert_name').verbose_name,
        required=True,
        help_text=models.CertificateAuthority._meta.get_field('cert_name').help_text
    )
    cert_certificate = forms.CharField(
        label=models.CertificateAuthority._meta.get_field('cert_certificate').verbose_name,
        widget=forms.Textarea(),
        required=True,
        help_text=models.CertificateAuthority._meta.get_field('cert_certificate').help_text,
    )
    cert_passphrase = forms.CharField(
        label=_("Passphrase"),
        required=False,
        help_text=_("Passphrase for encrypted private keys"),
        widget=forms.PasswordInput(render_value=True),
    )
    cert_passphrase2 = forms.CharField(
        label=_("Confirm Passphrase"),
        required=False,
        widget=forms.PasswordInput(render_value=True),
    )

    def clean_cert_passphrase2(self):
        cdata = self.cleaned_data
        passphrase = cdata.get('cert_passphrase')
        passphrase2 = cdata.get('cert_passphrase2')

        if passphrase and passphrase != passphrase2:
            raise forms.ValidationError(_(
                'Passphrase confirmation does not match.'
            ))
        return passphrase

    def middleware_clean(self, data):
        data['create_type'] = 'CA_CREATE_IMPORTED'
        data.pop('passphrase2', None)
        return data

    class Meta:
        fields = [
            'cert_name',
            'cert_certificate',
            'cert_privatekey',
            'cert_passphrase',
            'cert_passphrase2',
        ]
        model = models.CertificateAuthority


class CertificateAuthorityCreateInternalForm(MiddlewareModelForm, ModelForm):

    middleware_attr_prefix = 'cert_'
    middleware_attr_schema = 'certificate_authority'
    middleware_plugin = 'certificateauthority'
    is_singletone = False

    cert_name = forms.CharField(
        label=models.CertificateAuthority._meta.get_field('cert_name').verbose_name,
        required=True,
        help_text=models.CertificateAuthority._meta.get_field('cert_name').help_text
    )
    cert_key_type = forms.ChoiceField(
        label=_('Key Type'),
        help_text=_(
            'See https://crypto.stackexchange.com/questions/1190/why-is-elliptic-curve-cryptography-not-'
            'widely-used-compared-to-rsa for more information about key types.'
        ),
        required=True,
        choices=()
    )
    cert_ec_curve = forms.ChoiceField(
        label=_('EC Key Curve'),
        help_text=_(
            'Brainpool* curves can be more secure, while secp* curves can be faster. See https://tls.mbed.org/'
            'kb/cryptography/elliptic-curve-performance-nist-vs-brainpool for more information.'
        ),
        required=False,
        widget=forms.Select({'disabled': True}),
        choices=()
    )
    cert_key_length = forms.ChoiceField(
        label=_('Key Length'),
        required=False,
        choices=choices.CERT_KEY_LENGTH_CHOICES,
        initial=2048
    )
    cert_digest_algorithm = forms.ChoiceField(
        label=_('Digest Algorithm'),
        required=True,
        choices=choices.CERT_DIGEST_ALGORITHM_CHOICES,
        initial='SHA256'
    )
    cert_lifetime = forms.IntegerField(
        label=_('Lifetime'),
        required=True,
        initial=3650
    )
    cert_country = forms.ChoiceField(
        label=_('Country'),
        required=True,
        choices=choices.COUNTRY_CHOICES(),
        initial='US',
        help_text=_('Country Name (2 letter code)')
    )
    cert_state = forms.CharField(
        label=_('State'),
        required=True,
        help_text=_('State or Province Name (full name)')
    )
    cert_city = forms.CharField(
        label=_('Locality'),
        required=True,
        help_text=_('Locality Name (eg, city)'),
    )
    cert_organization = forms.CharField(
        label=_('Organization'),
        required=True,
        help_text=_('Organization Name (eg, company)')
    )
    cert_organizational_unit = forms.CharField(
        label=_('Organizational Unit'),
        required=False,
        help_text=_('Organizational unit of the entity')
    )
    cert_email = forms.CharField(
        label=_('Email Address'),
        required=True,
        help_text=_('Email Address'),
    )
    cert_common = forms.CharField(
        label=_('Common Name'),
        required=True,
        help_text=_('Common Name (eg, FQDN of FreeNAS server or service)')
    )
    cert_san = forms.CharField(
        widget=forms.Textarea,
        label=_('Subject Alternate Names'),
        required=False,
        help_text=_('Multi-domain support. Enter additional space separated domains')
    )

    def __init__(self, *args, **kwargs):
        super(CertificateAuthorityCreateInternalForm, self).__init__(*args, **kwargs)
        with client as c:
            self.fields['cert_key_type'].choices = list(map(lambda v: (v, v), c.call('certificate.key_type_choices')))
            self.fields['cert_ec_curve'].choices = list(map(lambda v: (v, v), c.call('certificate.ec_curve_choices')))

        self.fields['cert_key_type'].widget.attrs['onChange'] = (
            'javascript:Cert_EC_key();'
        )

    def middleware_clean(self, data):
        data['san'] = data['san'].split()
        data['create_type'] = 'CA_CREATE_INTERNAL'

        if data['key_type'] != 'EC':
            data.pop('ec_curve')
            data['key_length'] = int(data['key_length'])
        else:
            data.pop('key_length')

        return data

    class Meta:
        fields = [
            'cert_name',
            'cert_key_type',
            'cert_ec_curve',
            'cert_key_length',
            'cert_digest_algorithm',
            'cert_lifetime',
            'cert_country',
            'cert_state',
            'cert_city',
            'cert_organization',
            'cert_email',
            'cert_common',
            'cert_san',
        ]
        model = models.CertificateAuthority


class CertificateAuthorityCreateIntermediateForm(MiddlewareModelForm, ModelForm):

    middleware_attr_prefix = 'cert_'
    middleware_attr_schema = 'certificate_authority'
    middleware_plugin = 'certificateauthority'
    is_singletone = False

    cert_name = forms.CharField(
        label=models.CertificateAuthority._meta.get_field('cert_name').verbose_name,
        required=True,
        help_text=models.CertificateAuthority._meta.get_field('cert_name').help_text
    )
    cert_key_type = forms.ChoiceField(
        label=_('Key Type'),
        help_text=_(
            'See https://crypto.stackexchange.com/questions/1190/why-is-elliptic-curve-cryptography-not-'
            'widely-used-compared-to-rsa for more information about key types.'
        ),
        required=True,
        choices=()
    )
    cert_ec_curve = forms.ChoiceField(
        label=_('EC Key Curve'),
        help_text=_(
            'Brainpool* curves can be more secure, while secp* curves can be faster. See https://tls.mbed.org/'
            'kb/cryptography/elliptic-curve-performance-nist-vs-brainpool for more information.'
        ),
        required=False,
        widget=forms.Select({'disabled': True}),
        choices=()
    )
    cert_key_length = forms.ChoiceField(
        label=_('Key Length'),
        required=False,
        choices=choices.CERT_KEY_LENGTH_CHOICES,
        initial=2048
    )
    cert_digest_algorithm = forms.ChoiceField(
        label=_('Digest Algorithm'),
        required=True,
        choices=choices.CERT_DIGEST_ALGORITHM_CHOICES,
        initial='SHA256'
    )
    cert_lifetime = forms.IntegerField(
        label=_('Lifetime'),
        required=True,
        initial=3650
    )
    cert_country = forms.ChoiceField(
        label=_('Country'),
        required=True,
        choices=choices.COUNTRY_CHOICES(),
        initial='US',
        help_text=_('Country Name (2 letter code)')
    )
    cert_state = forms.CharField(
        label=_('State'),
        required=True,
        help_text=_('State or Province Name (full name)')
    )
    cert_city = forms.CharField(
        label=_('Locality'),
        required=True,
        help_text=_('Locality Name (eg, city)'),
    )
    cert_organization = forms.CharField(
        label=_('Organization'),
        required=True,
        help_text=_('Organization Name (eg, company)')
    )
    cert_organizational_unit = forms.CharField(
        label=_('Organizational Unit'),
        required=False,
        help_text=_('Organizational unit of the entity')
    )
    cert_email = forms.CharField(
        label=_('Email Address'),
        required=True,
        help_text=_('Email Address'),
    )
    cert_common = forms.CharField(
        label=_('Common Name'),
        required=True,
        help_text=_('Common Name (eg, FQDN of FreeNAS server or service)')
    )
    cert_san = forms.CharField(
        widget=forms.Textarea,
        label=_('Subject Alternate Names'),
        required=False,
        help_text=_('Multi-domain support. Enter additional space separated domains')
    )

    def __init__(self, *args, **kwargs):
        super(CertificateAuthorityCreateIntermediateForm, self).__init__(*args, **kwargs)

        with client as c:
            self.fields['cert_key_type'].choices = list(map(lambda v: (v, v), c.call('certificate.key_type_choices')))
            self.fields['cert_ec_curve'].choices = list(map(lambda v: (v, v), c.call('certificate.ec_curve_choices')))

        self.fields['cert_signedby'].required = True
        self.fields['cert_signedby'].queryset = (
            models.CertificateAuthority.objects.exclude(
                Q(cert_certificate__isnull=True) |
                Q(cert_privatekey__isnull=True) |
                Q(cert_certificate__exact='') |
                Q(cert_privatekey__exact='')
            )
        )
        self.fields['cert_signedby'].widget.attrs["onChange"] = (
            "javascript:CA_autopopulate();"
        )
        self.fields['cert_key_type'].widget.attrs['onChange'] = (
            'javascript:Cert_EC_key();'
        )

    def middleware_clean(self, data):
        data['san'] = data['san'].split()
        data['create_type'] = 'CA_CREATE_INTERMEDIATE'

        if data['key_type'] != 'EC':
            data.pop('ec_curve')
            data['key_length'] = int(data['key_length'])
        else:
            data.pop('key_length')

        return data

    class Meta:
        fields = [
            'cert_signedby',
            'cert_name',
            'cert_key_type',
            'cert_ec_curve',
            'cert_key_length',
            'cert_digest_algorithm',
            'cert_lifetime',
            'cert_country',
            'cert_state',
            'cert_city',
            'cert_organization',
            'cert_email',
            'cert_common',
            'cert_san',
        ]
        model = models.CertificateAuthority


class CertificateAuthoritySignCSRForm(MiddlewareModelForm, ModelForm):

    middleware_attr_prefix = 'cert_'
    middleware_attr_schema = 'certificate_authority'
    middleware_plugin = 'certificateauthority'
    is_singletone = False
    middleware_attr_map = {
        'cert_CSRs': 'csr_cert_id'
    }

    cert_CSRs = forms.ModelChoiceField(
        queryset=models.Certificate.objects.filter(cert_CSR__isnull=False),
        label=(_("CSRs"))
    )

    def middleware_clean(self, data):
        data['csr_cert_id'] = data.pop('CSRs')
        data['create_type'] = 'CA_SIGN_CSR'
        return data

    class Meta:
        fields = [
            'cert_CSRs',
            'cert_name',
        ]
        model = models.Certificate


class ACMEDNSAuthenticatorForm(MiddlewareModelForm, ModelForm):

    middleware_plugin = 'acme.dns.authenticator'
    middleware_attr_schema = 'dns_authenticator'
    middleware_attr_prefix = ''
    is_singletone = False

    authenticator = forms.ChoiceField(
        choices=(),
    )
    attributes = forms.CharField(
        widget=forms.widgets.HiddenInput,
    )
    credentials_schemas = forms.CharField(
        widget=forms.widgets.HiddenInput
    )

    class Meta:
        fields = [
            'name',
            'authenticator',
        ]
        model = models.ACMEDNSAuthenticator

    def __init__(self, *args, **kwargs):
        super(ACMEDNSAuthenticatorForm, self).__init__(*args, **kwargs)
        self.fields['authenticator'].widget.attrs['onChange'] = (
            'credentialsProvider("id_authenticator", "dns-authenticators-attribute");'
        )
        with client as c:
            schemas = c.call('acme.dns.authenticator.authenticator_schemas')

        schemas = [
            {
                'name': data['key'],
                'title': data['key'].replace('_', ' ').capitalize(),
                'credentials_schema': [
                    {
                        'property': s['title'],
                        'schema': {
                            '_required_': s['_required_'], 'type': s['type'], 'title': s['title'].replace('_', ' ')
                        }
                    }
                    for s in data['schema']
                ]
            }
            for data in schemas
        ]

        self.fields['authenticator'].choices = [
            (schema['name'], schema['title'])
            for schema in schemas
        ]
        self.fields['attributes'].initial = json.dumps(self.instance.attributes if self.instance else {})
        self.fields['credentials_schemas'].initial = json.dumps({
            schema['name']: schema['credentials_schema']
            for schema in schemas
        })

    def middleware_clean(self, data):
        data['attributes'] = json.loads(self.cleaned_data.get('attributes'))
        data.pop('credentials_schemas', None)
        if self.instance.id:
            data.pop('authenticator')
        return data


class CertificateACMEForm(MiddlewareModelForm, ModelForm):

    middleware_plugin = 'certificate'
    middleware_attr_prefix = 'cert_'
    middleware_attr_schema = 'certificate'
    is_singletone = False
    middleware_job = True

    cert_tos = forms.BooleanField(
        required=False,
        label=_('Terms Of Service'),
        initial=False,
        help_text=_('Please accept terms of service for the given ACME Server')
    )
    cert_renew_days = forms.IntegerField(
        required=False,
        initial=10,
        label=models.Certificate._meta.get_field('cert_renew_days').verbose_name,
        help_text=models.Certificate._meta.get_field('cert_renew_days').help_text
    )
    cert_name = forms.CharField(
        label=models.Certificate._meta.get_field('cert_name').verbose_name,
        required=True,
        help_text=models.Certificate._meta.get_field('cert_name').help_text
    )
    cert_acme_directory_uri = forms.ChoiceField(
        label=_('ACME Server Directory URI'),
        required=True,
        help_text=_('Please specify URI of ACME Server Directory')
    )

    class Meta:
        fields = [
            'cert_name',
            'cert_tos',
            'cert_renew_days',
            'cert_acme_directory_uri'
        ]
        model = models.Certificate

    def __init__(self, *args, **kwargs):
        self.csr_id = kwargs.pop('csr_id')
        self.middleware_job_wait = kwargs.pop('middleware_job_wait', False)

        super(CertificateACMEForm, self).__init__(*args, **kwargs)

        with client as c:
            popular_acme_choices = c.call('certificate.acme_server_choices')
            self.csr_domains = c.call('certificate.get_domain_names', self.csr_id)

        self.fields['cert_acme_directory_uri'].widget = forms.widgets.ComboBox()
        self.fields['cert_acme_directory_uri'].choices = [(v, v) for v in popular_acme_choices]

        for n, domain in enumerate(self.csr_domains):
            self.fields[f'domain_{n}'] = forms.ModelChoiceField(
                queryset=models.ACMEDNSAuthenticator.objects.all(),
                label=(_(f'Authenticator for {domain} ')),
                required=True,
                help_text=_(f'Specify Authenticator to be used for {domain} domain')
            )

    def clean_cert_tos(self):
        if not self.cleaned_data.get('cert_tos'):
            raise forms.ValidationError(_(
                'Please accept Terms of Service for the ACME Server'
            ))
        else:
            return True

    def middleware_clean(self, data):
        data['csr_id'] = self.csr_id
        data['dns_mapping'] = {}
        for n, domain_f in enumerate(self.csr_domains):
            data['dns_mapping'][domain_f] = self.cleaned_data.get(f'domain_{n}').id
        data['create_type'] = 'CERTIFICATE_CREATE_ACME'
        return data


class CertificateEditForm(MiddlewareModelForm, ModelForm):

    middleware_plugin = 'certificate'
    middleware_attr_prefix = 'cert_'
    middleware_attr_schema = 'certificate'
    is_singletone = False
    middleware_job = True

    cert_name = forms.CharField(
        label=models.Certificate._meta.get_field('cert_name').verbose_name,
        required=True,
        help_text=models.Certificate._meta.get_field('cert_name').help_text
    )
    cert_certificate = forms.CharField(
        label=models.Certificate._meta.get_field('cert_certificate').verbose_name,
        widget=forms.Textarea(),
        help_text=models.Certificate._meta.get_field('cert_certificate').help_text
    )
    cert_privatekey = forms.CharField(
        label=models.Certificate._meta.get_field('cert_privatekey').verbose_name,
        widget=forms.Textarea(),
        help_text=models.Certificate._meta.get_field('cert_privatekey').help_text
    )

    def __init__(self, *args, **kwargs):
        self.middleware_job_wait = kwargs.pop('middleware_job_wait', False)
        super(CertificateEditForm, self).__init__(*args, **kwargs)

        self.fields['cert_certificate'].widget.attrs['readonly'] = True
        self.fields['cert_privatekey'].widget.attrs['readonly'] = True

    def middleware_clean(self, data):
        return {'name': data['name']}

    class Meta:
        fields = [
            'cert_name',
            'cert_certificate',
            'cert_privatekey'
        ]
        model = models.Certificate


class CertificateCSREditForm(MiddlewareModelForm, ModelForm):

    middleware_plugin = 'certificate'
    middleware_attr_prefix = 'cert_'
    middleware_attr_schema = 'certificate'
    is_singletone = False
    middleware_job = True

    cert_name = forms.CharField(
        label=models.Certificate._meta.get_field('cert_name').verbose_name,
        required=True,
        help_text=models.Certificate._meta.get_field('cert_name').help_text
    )
    cert_CSR = forms.CharField(
        label=models.Certificate._meta.get_field('cert_CSR').verbose_name,
        widget=forms.Textarea(),
        help_text=models.Certificate._meta.get_field('cert_CSR').help_text
    )
    cert_privatekey = forms.CharField(
        label=models.Certificate._meta.get_field('cert_privatekey').verbose_name,
        widget=forms.Textarea(),
        help_text=models.Certificate._meta.get_field('cert_privatekey').help_text
    )

    def __init__(self, *args, **kwargs):
        self.middleware_job_wait = kwargs.pop('middleware_job_wait', False)
        super(CertificateCSREditForm, self).__init__(*args, **kwargs)

        self.fields['cert_name'].widget.attrs['readonly'] = False
        self.fields['cert_CSR'].widget.attrs['readonly'] = True
        self.fields['cert_privatekey'].widget.attrs['readonly'] = True

    def middleware_clean(self, data):
        data.pop('CSR', None)
        return data

    class Meta:
        fields = [
            'cert_name',
            'cert_CSR',
            'cert_privatekey'
        ]
        model = models.Certificate


class CertificateCSRImportForm(MiddlewareModelForm, ModelForm):

    middleware_plugin = 'certificate'
    middleware_attr_prefix = 'cert_'
    middleware_attr_schema = 'certificate'
    is_singletone = False
    middleware_job = True

    class Meta:
        fields = [
            'cert_name',
            'cert_csr',
            'cert_privatekey',
            'cert_passphrase',
            'cert_passphrase2'
        ]
        model = models.Certificate

    cert_name = forms.CharField(
        label=models.Certificate._meta.get_field('cert_name').verbose_name,
        required=True,
        help_text=models.Certificate._meta.get_field('cert_name').help_text
    )
    cert_csr = forms.CharField(
        label=models.Certificate._meta.get_field('cert_CSR').verbose_name,
        widget=forms.Textarea(),
        required=True,
        help_text=_(
            'Cut and paste the contents of your certificate signing request here'
        )
    )
    cert_privatekey = forms.CharField(
        label=models.Certificate._meta.get_field('cert_privatekey').verbose_name,
        widget=forms.Textarea(),
        required=True,
        help_text=models.Certificate._meta.get_field('cert_privatekey').help_text
    )
    cert_passphrase = forms.CharField(
        label=_("Passphrase"),
        required=False,
        help_text=_("Passphrase for encrypted private keys"),
        widget=forms.PasswordInput(render_value=True),
    )
    cert_passphrase2 = forms.CharField(
        label=_("Confirm Passphrase"),
        required=False,
        widget=forms.PasswordInput(render_value=True),
    )

    def __init__(self, *args, **kwargs):
        self.middleware_job_wait = kwargs.pop('middleware_job_wait', False)
        super(CertificateCSRImportForm, self).__init__(*args, **kwargs)

    def clean_cert_passphrase2(self):
        cdata = self.cleaned_data
        passphrase = cdata.get('cert_passphrase')
        passphrase2 = cdata.get('cert_passphrase2')

        if passphrase and passphrase != passphrase2:
            raise forms.ValidationError(_(
                'Passphrase confirmation does not match.'
            ))
        return passphrase

    def middleware_clean(self, data):
        data['create_type'] = 'CERTIFICATE_CREATE_IMPORTED_CSR'
        data.pop('passphrase2', None)
        data['CSR'] = data.pop('csr')
        return data


class CertificateImportForm(MiddlewareModelForm, ModelForm):

    middleware_plugin = 'certificate'
    middleware_attr_prefix = 'cert_'
    middleware_attr_schema = 'certificate'
    is_singletone = False
    middleware_job = True

    class Meta:
        fields = [
            'cert_name',
            'cert_csr',
            'cert_csr_id',
            'cert_certificate',
            'cert_privatekey',
            'cert_passphrase',
            'cert_passphrase2'
        ]
        model = models.Certificate

    cert_csr = forms.BooleanField(
        required=False,
        initial=False,
        label='CSR exists on FreeNAS',
        help_text=_(
            'Check this box if importing a certificate for which a CSR '
            'exists on the FreeNAS system'
        )
    )

    cert_csr_id = forms.ModelChoiceField(
        queryset=models.Certificate.objects.filter(cert_type=0x20),
        label=(_("CSRs")),
        required=False
    )

    cert_name = forms.CharField(
        label=models.Certificate._meta.get_field('cert_name').verbose_name,
        required=True,
        help_text=models.Certificate._meta.get_field('cert_name').help_text
    )
    cert_certificate = forms.CharField(
        label=models.Certificate._meta.get_field('cert_certificate').verbose_name,
        widget=forms.Textarea(),
        required=True,
        help_text=_(
            "Cut and paste the contents of your certificate here.<br>"
            "Order in which you should paste this: <br>"
            "The Primary Certificate.<br>The Intermediate CA's Certificate(s) (optional)."
            "<br>The Root CA Certificate (optional)"),
    )
    cert_privatekey = forms.CharField(
        label=models.Certificate._meta.get_field('cert_privatekey').verbose_name,
        widget=forms.Textarea(),
        required=False,
        help_text=models.Certificate._meta.get_field('cert_privatekey').help_text
    )
    cert_passphrase = forms.CharField(
        label=_("Passphrase"),
        required=False,
        help_text=_("Passphrase for encrypted private keys"),
        widget=forms.PasswordInput(render_value=True),
    )
    cert_passphrase2 = forms.CharField(
        label=_("Confirm Passphrase"),
        required=False,
        widget=forms.PasswordInput(render_value=True),
    )

    def __init__(self, *args, **kwargs):
        self.middleware_job_wait = kwargs.pop('middleware_job_wait', False)
        super(CertificateImportForm, self).__init__(*args, **kwargs)

        self.fields['cert_csr'].widget.attrs['onChange'] = (
            'toggleGeneric("id_cert_csr", ["id_cert_passphrase",'
            ' "id_cert_passphrase2", "id_cert_privatekey"], false); '
            'toggleGeneric("id_cert_csr", ["id_cert_csr_id"], true);'
        )

    def clean_cert_passphrase2(self):
        cdata = self.cleaned_data
        passphrase = cdata.get('cert_passphrase')
        passphrase2 = cdata.get('cert_passphrase2')

        if passphrase and passphrase != passphrase2:
            raise forms.ValidationError(_(
                'Passphrase confirmation does not match.'
            ))
        return passphrase

    def middleware_clean(self, data):
        data['create_type'] = 'CERTIFICATE_CREATE_IMPORTED'
        data.pop('passphrase2', None)
        if data.pop('csr', False):
            data.pop('passphrase', None)
        else:
            data.pop('csr_id', None)

        return data


class CertificateCreateInternalForm(MiddlewareModelForm, ModelForm):

    middleware_plugin = 'certificate'
    middleware_attr_prefix = 'cert_'
    middleware_attr_schema = 'certificate'
    is_singletone = False
    middleware_job = True

    cert_name = forms.CharField(
        label=models.Certificate._meta.get_field('cert_name').verbose_name,
        required=True,
        help_text=models.Certificate._meta.get_field('cert_name').help_text
    )
    cert_key_type = forms.ChoiceField(
        label=_('Key Type'),
        help_text=_(
            'See https://crypto.stackexchange.com/questions/1190/why-is-elliptic-curve-cryptography-not-'
            'widely-used-compared-to-rsa for more information about key types.'
        ),
        required=True,
        choices=()
    )
    cert_ec_curve = forms.ChoiceField(
        label=_('EC Key Curve'),
        help_text=_(
            'Brainpool* curves can be more secure, while secp* curves can be faster. See https://tls.mbed.org/'
            'kb/cryptography/elliptic-curve-performance-nist-vs-brainpool for more information.'
        ),
        required=False,
        widget=forms.Select({'disabled': True}),
        choices=()
    )
    cert_key_length = forms.ChoiceField(
        label=_('Key Length'),
        required=False,
        choices=choices.CERT_KEY_LENGTH_CHOICES,
        initial=2048
    )
    cert_digest_algorithm = forms.ChoiceField(
        label=_('Digest Algorithm'),
        required=True,
        choices=choices.CERT_DIGEST_ALGORITHM_CHOICES,
        initial='SHA256'
    )
    cert_lifetime = forms.IntegerField(
        label=_('Lifetime'),
        required=True,
        initial=3650
    )
    cert_country = forms.ChoiceField(
        label=_('Country'),
        required=True,
        choices=choices.COUNTRY_CHOICES(),
        initial='US',
        help_text=_('Country Name (2 letter code)')
    )
    cert_state = forms.CharField(
        label=_('State'),
        required=True,
        help_text=_('State or Province Name (full name)')
    )
    cert_city = forms.CharField(
        label=_('Locality'),
        required=True,
        help_text=_('Locality Name (eg, city)'),
    )
    cert_organization = forms.CharField(
        label=_('Organization'),
        required=True,
        help_text=_('Organization Name (eg, company)')
    )
    cert_organizational_unit = forms.CharField(
        label=_('Organizational Unit'),
        required=False,
        help_text=_('Organizational unit of the entity')
    )
    cert_email = forms.CharField(
        label=_('Email Address'),
        required=True,
        help_text=_('Email Address'),
    )
    cert_common = forms.CharField(
        label=_('Common Name'),
        required=True,
        help_text=_('Common Name (eg, FQDN of FreeNAS server or service)')
    )
    cert_san = forms.CharField(
        widget=forms.Textarea,
        label=_('Subject Alternate Names'),
        required=False,
        help_text=_('Multi-domain support. Enter additional space separated domains')
    )

    def __init__(self, *args, **kwargs):
        self.middleware_job_wait = kwargs.pop('middleware_job_wait', False)
        super(CertificateCreateInternalForm, self).__init__(*args, **kwargs)

        with client as c:
            self.fields['cert_key_type'].choices = list(map(lambda v: (v, v), c.call('certificate.key_type_choices')))
            self.fields['cert_ec_curve'].choices = list(map(lambda v: (v, v), c.call('certificate.ec_curve_choices')))

        self.fields['cert_signedby'].required = True
        self.fields['cert_signedby'].queryset = (
            models.CertificateAuthority.objects.exclude(
                Q(cert_certificate__isnull=True) |
                Q(cert_privatekey__isnull=True) |
                Q(cert_certificate__exact='') |
                Q(cert_privatekey__exact='')
            )
        )
        self.fields['cert_signedby'].widget.attrs["onChange"] = (
            "javascript:CA_autopopulate();"
        )
        self.fields['cert_key_type'].widget.attrs['onChange'] = (
            'javascript:Cert_EC_key();'
        )

    def middleware_clean(self, data):
        data['san'] = data['san'].split()
        data['create_type'] = 'CERTIFICATE_CREATE_INTERNAL'
        data['signedby'] = self.instance.cert_signedby.pk

        if data['key_type'] != 'EC':
            data.pop('ec_curve')
            data['key_length'] = int(data['key_length'])
        else:
            data.pop('key_length')

        return data

    class Meta:
        fields = [
            'cert_signedby',
            'cert_name',
            'cert_key_type',
            'cert_ec_curve',
            'cert_key_length',
            'cert_digest_algorithm',
            'cert_lifetime',
            'cert_country',
            'cert_state',
            'cert_city',
            'cert_organization',
            'cert_organizational_unit',
            'cert_email',
            'cert_common',
            'cert_san'
        ]
        model = models.Certificate


class CertificateCreateCSRForm(MiddlewareModelForm, ModelForm):

    middleware_plugin = 'certificate'
    middleware_attr_prefix = 'cert_'
    middleware_attr_schema = 'certificate'
    is_singletone = False
    middleware_job = True

    cert_name = forms.CharField(
        label=models.Certificate._meta.get_field('cert_name').verbose_name,
        required=True,
        help_text=models.Certificate._meta.get_field('cert_name').help_text
    )
    cert_key_type = forms.ChoiceField(
        label=_('Key Type'),
        help_text=_(
            'See https://crypto.stackexchange.com/questions/1190/why-is-elliptic-curve-cryptography-not-'
            'widely-used-compared-to-rsa for more information about key types.'
        ),
        required=True,
        choices=()
    )
    cert_ec_curve = forms.ChoiceField(
        label=_('EC Key Curve'),
        help_text=_(
            'Brainpool* curves can be more secure, while secp* curves can be faster. See https://tls.mbed.org/'
            'kb/cryptography/elliptic-curve-performance-nist-vs-brainpool for more information.'
        ),
        required=False,
        widget=forms.Select({'disabled': True}),
        choices=()
    )
    cert_key_length = forms.ChoiceField(
        label=_('Key Length'),
        required=False,
        choices=choices.CERT_KEY_LENGTH_CHOICES,
        initial=2048
    )
    cert_digest_algorithm = forms.ChoiceField(
        label=_('Digest Algorithm'),
        required=True,
        choices=choices.CERT_DIGEST_ALGORITHM_CHOICES,
        initial='SHA256'
    )
    cert_country = forms.ChoiceField(
        label=_('Country'),
        required=True,
        choices=choices.COUNTRY_CHOICES(),
        initial='US',
        help_text=_('Country Name (2 letter code)')
    )
    cert_state = forms.CharField(
        label=_('State'),
        required=True,
        help_text=_('State or Province Name (full name)')
    )
    cert_city = forms.CharField(
        label=_('Locality'),
        required=True,
        help_text=_('Locality Name (eg, city)'),
    )
    cert_organization = forms.CharField(
        label=_('Organization'),
        required=True,
        help_text=_('Organization Name (eg, company)')
    )
    cert_organizational_unit = forms.CharField(
        label=_('Organizational Unit'),
        required=False,
        help_text=_('Organizational unit of the entity')
    )
    cert_email = forms.CharField(
        label=_('Email Address'),
        required=True,
        help_text=_('Email Address'),
    )
    cert_common = forms.CharField(
        label=_('Common Name'),
        required=True,
        help_text=_('Common Name (eg, FQDN of FreeNAS server or service)')
    )
    cert_san = forms.CharField(
        widget=forms.Textarea,
        label=_('Subject Alternate Names'),
        required=False,
        help_text=_('Multi-domain support. Enter additional space separated domains')
    )

    def __init__(self, *args, **kwargs):
        self.middleware_job_wait = kwargs.pop('middleware_job_wait', False)
        super(CertificateCreateCSRForm, self).__init__(*args, **kwargs)

        with client as c:
            self.fields['cert_key_type'].choices = list(map(lambda v: (v, v), c.call('certificate.key_type_choices')))
            self.fields['cert_ec_curve'].choices = list(map(lambda v: (v, v), c.call('certificate.ec_curve_choices')))

        self.fields['cert_key_type'].widget.attrs['onChange'] = (
            'javascript:Cert_EC_key();'
        )

    def middleware_clean(self, data):
        data['san'] = data['san'].split()
        data['create_type'] = 'CERTIFICATE_CREATE_CSR'

        if data['key_type'] != 'EC':
            data.pop('ec_curve')
            data['key_length'] = int(data['key_length'])
        else:
            data.pop('key_length')

        return data

    class Meta:
        fields = [
            'cert_name',
            'cert_key_type',
            'cert_ec_curve',
            'cert_key_length',
            'cert_digest_algorithm',
            'cert_country',
            'cert_state',
            'cert_city',
            'cert_organization',
            'cert_organizational_unit',
            'cert_email',
            'cert_common',
            'cert_san'
        ]
        model = models.Certificate


class CloudCredentialsForm(ModelForm):

    provider = forms.ChoiceField(
        choices=(),
    )
    attributes = forms.CharField(
        widget=forms.widgets.HiddenInput,
    )
    credentials_schemas = forms.CharField(
        widget=forms.widgets.HiddenInput,
    )

    verify = forms.CharField(
        required=False,
        widget=forms.widgets.HiddenInput(),
    )

    class Meta:
        fields = [
            'name',
            'provider',
        ]
        model = models.CloudCredentials

    def __init__(self, *args, **kwargs):
        super(CloudCredentialsForm, self).__init__(*args, **kwargs)
        self.fields['provider'].widget.attrs['onChange'] = (
            'credentialsProvider("id_provider", "cloud-credentials-attribute");'
        )
        with client as c:
            providers = c.call("cloudsync.providers")
        self.fields["provider"].choices = [
            (provider["name"], provider["title"])
            for provider in providers
        ]
        self.fields["attributes"].initial = json.dumps(self.instance.attributes if self.instance else {})
        self.fields["credentials_schemas"].initial = json.dumps({
            provider["name"]: provider["credentials_schema"]
            for provider in providers
        })

    def save(self, *args, **kwargs):
        with client as c:
            data = {
                'name': self.cleaned_data.get('name'),
                'provider': self.cleaned_data.get('provider'),
                'attributes': json.loads(self.cleaned_data.get('attributes')),
            }

            if self.cleaned_data.get("verify") == "1":
                result = c.call("cloudsync.credentials.verify", {"provider": data["provider"],
                                                                 "attributes": data["attributes"]})
                if result["valid"]:
                    msg = "Credentials are valid"
                else:
                    msg = f"Credentials are invalid: {result['error']}"

                raise ValidationErrors([["__all__", msg, errno.EINVAL]])

            if self.instance.id:
                c.call('cloudsync.credentials.update', self.instance.id, data)
            else:
                self.instance = models.CloudCredentials.objects.get(
                    pk=c.call('cloudsync.credentials.create', data)['id']
                )

        return self.instance

    def delete(self, *args, **kwargs):
        with client as c:
            c.call('cloudsync.credentials.delete', self.instance.id)


class BackupForm(Form):
    def __init__(self, *args, **kwargs):
        super(BackupForm, self).__init__(*args, **kwargs)

    backup_hostname = forms.CharField(
        label=_("Hostname or IP address"),
        required=True)

    backup_username = forms.CharField(
        label=_("User name"),
        required=True)

    backup_password = forms.CharField(
        label=_("Password"),
        required=False,
        widget=forms.widgets.PasswordInput(),
    )

    backup_password2 = forms.CharField(
        label=_("Confirm Password"),
        required=False,
        widget=forms.widgets.PasswordInput(),
    )

    backup_directory = forms.CharField(
        label=_("Remote directory"),
        required=True)

    backup_data = forms.BooleanField(
        label=_("Backup data"),
        required=False)

    backup_compression = forms.BooleanField(
        label=_("Compress backup"),
        required=False)

    backup_auth_keys = forms.BooleanField(
        label=_("Use key authentication"),
        required=False)

    def clean_backup_password2(self):
        pwd = self.cleaned_data.get('backup_password')
        pwd2 = self.cleaned_data.get('backup_password2')
        if pwd and pwd != pwd2:
            raise forms.ValidationError(
                _("The two password fields didn't match.")
            )
        return pwd2


class SupportForm(ModelForm):

    class Meta:
        model = models.Support
        fields = '__all__'
        widgets = {
            'enabled': forms.widgets.CheckboxInput(),
        }

    def __init__(self, *args, **kwargs):
        super(SupportForm, self).__init__(*args, **kwargs)
        self.fields['enabled'].widget.attrs['onChange'] = (
            'javascript:toggleGeneric("id_enabled", ["id_name", "id_title", '
            '"id_email", "id_phone", "id_secondary_name", "id_secondary_title", '
            '"id_secondary_email", "id_secondary_phone"], true);'
        )

        # If proactive support is not available disable all fields
        available = self.instance.is_available(support=self.instance)[0]
        if not available:
            self.fields['enabled'].label += ' (Silver/Gold support only)'
        if (self.instance.id and not self.instance.enabled) or not available:
            for name, field in self.fields.items():
                if available and name == 'enabled':
                    continue
                field.widget.attrs['disabled'] = 'disabled'

    def clean_enabled(self):
        return self.cleaned_data.get('enabled') in ('True', '1')

    def clean(self):
        data = self.cleaned_data
        for name in self.fields.keys():
            if name == 'enabled':
                continue
            if data.get('enabled') and not data.get(name):
                self._errors[name] = self.error_class([_(
                    'This field is required.'
                )])
        return data


class SSHKeyPairKeychainCredentialForm(MiddlewareModelForm, ModelForm):
    middleware_attr_map = {f"attributes.{k}": k for k in ["private_key", "public_key"]}
    middleware_attr_prefix = ""
    middleware_attr_schema = "keychain_credential"
    middleware_plugin = "keychaincredential"
    is_singletone = False

    def __init__(self, *args, **kwargs):
        kwargs.setdefault("initial", {})
        if "instance" in kwargs:
            kwargs["initial"].update(kwargs["instance"].attributes or {})
        super().__init__(*args, **kwargs)

    def middleware_prepare(self):
        attributes = super().middleware_prepare()
        return {
            "name": attributes.pop("name"),
            "type": "SSH_KEY_PAIR",
            "attributes": attributes,
        }

    name = forms.CharField(
        label=_("Name"),
    )
    private_key = forms.CharField(
        widget=forms.Textarea(),
        required=True,
    )
    public_key = forms.CharField(
        widget=forms.Textarea(),
        required=False,
    )

    class Meta:
        exclude = [
            "type",
            "attributes",
        ]
        model = models.SSHKeyPairKeychainCredential


class SSHCredentialsKeychainCredentialForm(MiddlewareModelForm, ModelForm):
    middleware_attr_map = {f"attributes.{k}": k for k in ["private_key", "public_key"]}
    middleware_attr_prefix = ""
    middleware_attr_schema = "keychain_credential"
    middleware_plugin = "keychaincredential"
    middleware_exclude_fields = ["type", "url", "token"]
    is_singletone = False

    class Meta:
        exclude = [
            "type",
            "attributes",
        ]
        model = models.SSHCredentialsKeychainCredential

    def __init__(self, *args, **kwargs):
        if "instance" in kwargs and kwargs["instance"].id:
            kwargs.setdefault("initial", {})
            kwargs["initial"].update(kwargs["instance"].attributes or {})

        super().__init__(*args, **kwargs)

        if "instance" in kwargs and kwargs["instance"].id:
            del self.fields['type']
            del self.fields['url']
            del self.fields['token']
        else:
            self.fields['type'].widget.attrs['onChange'] = "sshCredentialsTypeToggle();"

    def middleware_prepare(self):
        attributes = super().middleware_prepare()
        return {
            "name": attributes.pop("name"),
            "type": "SSH_CREDENTIALS",
            "attributes": attributes,
        }

    def save(self):
        if self.cleaned_data.get("type") == "SEMIAUTOMATIC":
            data = self.middleware_prepare()
            data["attributes"]["name"] = data["name"]
            data = data["attributes"]
            data.pop("host")
            data.pop("port")
            data.pop("remote_host_key")
            data["url"] = self.cleaned_data.get("url")
            data["token"] = self.cleaned_data.get("token")

            self.middleware_attr_schema = "keychain"
            self._middleware_action = "remote_ssh_semiautomatic_setup"

            with client as c:
                try:
                    result = c.call(f"keychaincredential.remote_ssh_semiautomatic_setup", data)
                except ClientException as e:
                    raise ValidationErrors([["__all__", str(e), errno.EINVAL]])
                else:
                    self.instance = self._meta.model.objects.get(pk=result["id"])
                    return self.instance

        return super().save()

    name = forms.CharField(
        label=_("Name"),
    )
    type = forms.ChoiceField(
        label=("Setup method"),
        choices=[
            ("MANUAL", "Manual"),
            ("SEMIAUTOMATIC", "Semi-automatic (FreeNAS only)"),
        ],
    )
    url = forms.CharField(
        required=False,
        label=_("FreeNAS URL"),
        help_text=_(
            "E.g. http://192.168.0.180"
        ),
    )
    token = forms.CharField(
        required=False,
        label=_("Auth Token"),
        help_text=_(
            "On the remote host go to Storage -> Replication Tasks, click the "
            "Temporary Auth Token button and paste the resulting value in to "
            "this field."
        ),
    )
    host = forms.CharField(
        required=False,
        label=_("Host"),
    )
    port = forms.CharField(
        required=False,
        label=_("Port"),
        initial=22,
    )
    username = forms.CharField(
        label=_("Username"),
        initial="root",
        required=True,
    )
    private_key = forms.ModelChoiceField(
        label=_("Private Key"),
        queryset=models.SSHKeyPairKeychainCredential.objects.all(),
    )
    remote_host_key = forms.CharField(
        required=False,
        widget=forms.Textarea(),
    )
    cipher = forms.ChoiceField(
        choices=(
            ("STANDARD", "Standard"),
            ("FAST", "Fast"),
            ("DISABLED", "Disabled"),
        ),
    )
    connect_timeout = forms.IntegerField(
        label=_("Connect Timeout"),
        initial=10,
    )

    def clean_port(self):
        value = self.cleaned_data.get('port')
        if value:
            try:
                return int(value)
            except ValueError:
                raise forms.ValidationError("Not a valid integer")
        else:
            return None
