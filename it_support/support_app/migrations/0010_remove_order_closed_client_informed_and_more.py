# Generated by Django 4.1.7 on 2023-02-18 10:47

from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('support_app', '0009_order_all_contractors_informed_and_more'),
    ]

    operations = [
        migrations.RemoveField(
            model_name='order',
            name='closed_client_informed',
        ),
        migrations.RemoveField(
            model_name='order',
            name='in_work_client_informed',
        ),
    ]
