# +
# Copyright 2016 ZFStor
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
from django.conf.urls import url

from .views import (
    add, add_progress, start, stop, power_off, restart, home, clone, vnc_web, download_progress,
)

urlpatterns = [
    url(r'^$', home, name="vm_home"),
    url(r'^add/$', add, name="vm_add"),
    url(r'^add/progress/$', add_progress, name="vm_add_progress"),
    url(r'^start/(?P<id>\d+)/$', start, name="vm_start"),
    url(r'^stop/(?P<id>\d+)/$', stop, name="vm_stop"),
    url(r'^poweroff/(?P<id>\d+)/$', power_off, name="vm_poweroff"),
    url(r'^restart/(?P<id>\d+)/$', restart, name="vm_restart"),
    url(r'^clone/(?P<id>\d+)/$', clone, name="vm_clone"),
    url(r'^vncweb/(?P<id>\d+)/$', vnc_web, name="vm_vncweb"),
    url(r'^verify_progress/$', download_progress, name="vm_download_progress"),
]
