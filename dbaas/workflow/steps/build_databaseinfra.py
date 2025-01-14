# -*- coding: utf-8 -*-
from base import BaseStep
from util import gen_infra_names, make_db_random_password
from util.providers import get_engine_credentials
from physical.models import DatabaseInfra
from ..exceptions.error_codes import DBAAS_0002
from util import full_stack
import logging

LOG = logging.getLogger(__name__)


class BuildDatabaseInfra(BaseStep):
    def __unicode__(self):
        return "Initializing databaseinfra..."

    def do(self, workflow_dict):
        try:
            workflow_dict['names'] = gen_infra_names(
                name=workflow_dict['name'], qt=workflow_dict['qt'])


            databaseinfra = DatabaseInfra()
            databaseinfra.name = workflow_dict['names']['infra']
            if workflow_dict['enginecod'] == workflow_dict['REDIS']:
                databaseinfra.user = ''
                databaseinfra.password = make_db_random_password()
            else:
                credentials = get_engine_credentials(engine=str(workflow_dict['plan'].engine_type),
                                                    environment=workflow_dict['environment'])
                databaseinfra.user = credentials.user
                databaseinfra.password = credentials.password
            databaseinfra.engine = workflow_dict[
                'plan'].engine_type.engines.all()[0]
            databaseinfra.plan = workflow_dict['plan']
            databaseinfra.environment = workflow_dict['environment']
            databaseinfra.capacity = 1
            databaseinfra.per_database_size_mbytes = workflow_dict['plan'].max_db_size
            databaseinfra.save()

            LOG.info("DatabaseInfra created!")
            workflow_dict['databaseinfra'] = databaseinfra

            return True
        except Exception, e:

            traceback = full_stack()

            workflow_dict['exceptions']['error_codes'].append(DBAAS_0002)
            workflow_dict['exceptions']['traceback'].append(traceback)

            return False

    def undo(self, workflow_dict):
        try:

            if 'databaseinfra' in workflow_dict:
                LOG.info("Destroying databaseinfra...")
                workflow_dict['databaseinfra'].delete()
                return True

        except Exception, e:
            traceback = full_stack()

            workflow_dict['exceptions']['error_codes'].append(DBAAS_0002)
            workflow_dict['exceptions']['traceback'].append(traceback)

            return False
