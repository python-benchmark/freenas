# -*- coding: utf-8 -*-
# Generated by Django 1.10.8 on 2018-12-10 11:18
from __future__ import unicode_literals

from django.db import migrations, models
import freenasUI.freeadmin.models.fields


class Migration(migrations.Migration):

    dependencies = [
        ('tasks', '0009_auto_20181211_0237'),
    ]

    operations = [
        migrations.AddField(
            model_name='cloudsync',
            name='transfers',
            field=models.IntegerField(null=True),
        ),
    ]
