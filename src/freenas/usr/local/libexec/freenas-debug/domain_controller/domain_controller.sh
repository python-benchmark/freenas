#!/bin/sh
#+
# Copyright 2015 iXsystems, Inc.
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


domain_controller_opt() { echo D; }
domain_controller_help() { echo "Dump Domain Controller Configuration"; }
domain_controller_directory() { echo "DomainController"; }
domain_controller_func()
{
	local realm
	local domain
	local role
	local dns_backend
	local dns_forwarder
	local forest_level
	local krb_realm
	local kdc
	local admin_server
	local kpasswd_server
	local onoff
	local enabled="DISABLED"


	#
	#	Check if the Domain Controller is set to start on boot. 
	#
	onoff=$(${FREENAS_SQLITE_CMD} ${FREENAS_CONFIG} "
	SELECT
		srv_enable
	FROM
		services_services
	WHERE
		srv_service =  'domaincontroller'
	ORDER BY
		-id

	LIMIT 1
	")

	enabled="not start on boot."
	if [ "${onoff}" = "1" ]
	then
		enabled="start on boot."
	fi

	section_header "Domain Controller Boot Status"
	echo "Domain Controller will ${enabled}"
	section_footer

	#
	#	If Domain Controller isn't set to start on boot, exit this script.
	#	We can not afford to run the wbinfo commands at the end of this script
	#	on TrueNAS HA systems because it can potentially take an incredible amount of time
	#	if the customers AD environment is quite large. It also hangs the freenas-debug process
	#	from finishing in a timely manner.
	#	
	#	For now, we will exit if it isn't set to start on boot.
	#
	if [ "${onoff}" = "0" ]
	then
		exit 0
	fi

	#
	#	Dump Domain Controller configuration
	#
	local IFS="|"
	read realm domain role dns_backend dns_forwarder forest_level \
		krb_realm kdc admin_server kpasswd_server <<-__DC__
	$(${FREENAS_SQLITE_CMD} ${FREENAS_CONFIG} "
	SELECT
		sd.dc_realm,
		sd.dc_domain,
		sd.dc_role,
		sd.dc_dns_backend,
		sd.dc_dns_forwarder,
		sd.dc_forest_level,
		dk.krb_realm,
		dk.krb_kdc,
		dk.krb_admin_server,
		dk.krb_kpasswd_server
	FROM
		services_domaincontroller as sd

	INNER JOIN
		directoryservice_kerberosrealm as dk
	ON
		(sd.dc_kerberos_realm_id = dk.id)

	ORDER BY
		-sd.id

	LIMIT 1
	")
__DC__

	IFS="
"

	section_header "Domain Controller Settings"
	cat<<-__EOF__
	Realm:                   ${realm}
	Domain:                  ${domain}
	Role:                    ${role}
	DNS Backend:             ${dns_backend}
	DNS Forwarder:           ${dns_forwarder}
	Forst Level:             ${forest_level}
	Kerberos Realm:          ${realm}
	Kerberos KDC:            ${kdc}
	Kerberos Admin Server:   ${admin_server}
	Kerberos Kpasswd Server: ${kpasswd_server}
__EOF__
	section_footer

	#
	#	Dump kerberos configuration
	#
	section_header "${PATH_KRB5_CONFIG}"
	sc "${PATH_KRB5_CONFIG}" 2>/dev/null
	section_footer

	#
	#	Dump nsswitch.conf
	#
	section_header "${PATH_NS_CONF}"
	sc "${PATH_NS_CONF}"
	section_footer

	#
	#	Dump samba configuration
	#
	section_header "${SAMBA_CONF}"
	sc "${SAMBA_CONF}"
	section_footer

	#
	#	List kerberos tickets
	#
	section_header "Kerberos Tickets - 'klist'"
	klist
	section_footer

	#
	#	Dump Domain Controller SSSD configuration
	#
	section_header "${SSSD_CONF}"
	sc "${SSSD_CONF}" | grep -iv ldap_default_authtok
	section_footer

	#
	#	Dump generated DC config file
	#
	#section_header "${AD_CONFIG_FILE}"
	#sc "${AD_CONFIG_FILE}"
	#section_footer

	#
	#	Try to generate a DC config file
	#
	#section_header "adtool get config_file"
	#adtool get config_file
	#section_footer

	#
	#	Dump Domain Controller domain info
	#
	section_header "Domain Controller Domain Info - 'net ads info'"
	net ads info
	section_footer

	#
	#	Dump wbinfo information
	#
	section_header "Active Directory Trust Secret - 'wbinfo -t'"
	wbinfo -t
	section_footer
	section_header "Active Directory NETLOGON connection - 'wbinfo -P'"
	wbinfo -P
	section_footer
	section_header "Active Directory trusted domains - 'wbinfo -m'"
	wbinfo -m
	section_footer
	section_header "Active Directory all domains - 'wbinfo --all-domains'"
	wbinfo --all-domains
	section_footer
	section_header "Active Directory own domain - 'wbinfo --own-domain'"
	wbinfo --own-domain
	section_footer
	section_header "Active Directory online status - 'wbinfo --online-status'"
	wbinfo --online-status
	section_footer
	section_header "Active Directory domain info - 'wbinfo --domain-info=$(wbinfo --own-domain)'"
	wbinfo --domain-info="$(wbinfo --own-domain)"
	section_footer
	section_header "Active Directory DC name - 'wbinfo --dsgetdcname=$(wbinfo --own-domain)'"
	wbinfo --dsgetdcname="$(wbinfo --own-domain)"
	section_footer
	section_header "Active Directory DC info - 'wbinfo --dc-info=$(wbinfo --own-domain)'"
	wbinfo --dc-info="$(wbinfo --own-domain)"
	section_footer

	#
	#	Dump Active Directory users and groups
	#
	#
	#	We limit the output here because the point of running
	#	wbinfo and getent is not, necessarily, to find a
	#	specific user or group. It's to simply see if we
	#	are even enumerating users and groups
	#
	section_header "Active Directory Users - 'wbinfo -u'"
	wbinfo -u | -head 50
	section_header "Active Directory Groups - 'wbinfo -g'"
	wbinfo -g | -head 50
	section_header "Local Users database- 'getent passwd'"
	getent passwd | -head 50
	section_header "Local Groups database- 'getent group'"
	getent group | -head 50
	section_footer
}
