# -*- coding: utf-8 -*-
import logging
from base import BaseStep
from dbaas_cloudstack.provider import CloudStackProvider
from dbaas_credentials.models import CredentialType
from util import get_credentials_for
from dbaas_cloudstack.models import HostAttr, DatabaseInfraAttr


LOG = logging.getLogger(__name__)


class CreateSecondaryIp(BaseStep):

    def __unicode__(self):
        return "Provisioning secondary ips..."

    def do(self, workflow_dict):

        try:
            if not 'hosts' in workflow_dict:
                return False

            if len(workflow_dict['hosts']) ==1:
                return True

            cs_credentials = get_credentials_for(
                environment=workflow_dict['environment'],
                credential_type=CredentialType.CLOUDSTACK)

            cs_provider = CloudStackProvider(credentials=cs_credentials)

            workflow_dict['databaseinfraattr'] = []

            for host in workflow_dict['hosts']:
                LOG.info("Creating Secondary ips...")

                host_attr = HostAttr.objects.get(host= host)

                reserved_ip = cs_provider.reserve_ip(
                    project_id=cs_credentials.project,
                    vm_id=host_attr.vm_id)

                if not reserved_ip:
                    return False

                total = DatabaseInfraAttr.objects.filter(
                    databaseinfra=workflow_dict['databaseinfra']).count()

                databaseinfraattr = DatabaseInfraAttr()

                databaseinfraattr.ip = reserved_ip['secondary_ip']

                if total == 0:
                    databaseinfraattr.is_write = True

                    LOG.info("Updating databaseinfra endpoint...")

                    databaseinfra = workflow_dict['databaseinfra']
                    databaseinfra.endpoint = databaseinfraattr.ip + ":%i" % 3306
                    databaseinfra.save()

                    workflow_dict['databaseinfra'] = databaseinfra

                else:
                    databaseinfraattr.is_write = False

                databaseinfraattr.cs_ip_id = reserved_ip['cs_ip_id']
                databaseinfraattr.databaseinfra = workflow_dict[
                    'databaseinfra']
                databaseinfraattr.save()

                workflow_dict['databaseinfraattr'].append(databaseinfraattr)


            return True
        except Exception as e:
            print e
            return False

    def undo(self, workflow_dict):
        LOG.info("Running undo...")
        try:
            if not 'databaseinfra' in workflow_dict and not 'hosts' in workflow_dict:
                LOG.info(
                    "We could not find a databaseinfra inside the workflow_dict")
                return False

            if len(workflow_dict['hosts']) ==1:
                return True

            databaseinfraattr = DatabaseInfraAttr.objects.filter(
                databaseinfra=workflow_dict['databaseinfra'])

            cs_credentials = get_credentials_for(
                environment=workflow_dict['environment'],
                credential_type=CredentialType.CLOUDSTACK)

            cs_provider = CloudStackProvider(credentials=cs_credentials)

            for infra_attr in databaseinfraattr:
                LOG.info("Removing secondary_ip for %s" % infra_attr.cs_ip_id)
                if not cs_provider.remove_secondary_ips(infra_attr.cs_ip_id):
                    return False

                LOG.info("Secondary ip deleted!")

                infra_attr.delete()
                LOG.info("Databaseinfraattr deleted!")

            return True
        except Exception as e:
            print e
            return False