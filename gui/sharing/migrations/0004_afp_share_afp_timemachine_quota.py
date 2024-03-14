# -*- coding: utf-8 -*-
# Generated by Django 1.10.7 on 2017-09-08 06:44
from __future__ import unicode_literals

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('sharing', '0003_cifs_auxsmbconf_change_default'),
    ]

    operations = [
        migrations.AddField(
            model_name='afp_share',
            name='afp_timemachine_quota',
            field=models.IntegerField(default=0, help_text='Quota for each Time Machine backup on this share.', verbose_name='Time Machine Quota'),
        ),
    ]
