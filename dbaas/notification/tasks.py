# -*- coding: utf-8 -*-
from __future__ import absolute_import

from django.conf import settings

from system.models import Configuration
from celery.utils.log import get_task_logger
from celery.exceptions import SoftTimeLimitExceeded
from dbaas.celery import app
from django.db import transaction

from util import call_script
from util.decorators import only_one
from util import email_notifications
from util.providers import make_infra, destroy_infra
from util import full_stack
from .util import get_clone_args
from .models import TaskHistory
from drivers import factory_for
from django.db.models import Sum, Count
from physical.models import Plan

from physical.models import DatabaseInfra
from physical.models import Instance
from logical.models import Database
from account.models import Team
from simple_audit.models import AuditRequest
from system.models import Configuration
from util.laas import register_database_laas

LOG = get_task_logger(__name__)


def get_history_for_task_id(task_id):
    
    try:
        return TaskHistory.objects.get(task_id=task_id)
    except Exception, e:
        LOG.error("could not find history for task id %s" % task_id)
        return None


def rollback_database(dest_database):
    dest_database.is_in_quarantine = True
    dest_database.save()
    dest_database.delete()


@app.task(bind=True)
def create_database(self, name, plan, environment, team, project, description, user=None):
    AuditRequest.new_request("create_database", self.request.args[-1], "localhost")
    try:
        task_history = TaskHistory.register(request=self.request, user=user)
        LOG.info("id: %s | task: %s | kwargs: %s | args: %s" % (
            self.request.id, self.request.task, self.request.kwargs, str(self.request.args)))

        task_history.update_details(persist=True, details="Loading Process...")

        result = make_infra(plan=plan, environment=environment, name=name, task=task_history)

        if result['created']==False:

            if 'exceptions' in result:
                error = "\n".join(": ".join(err) for err in result['exceptions']['error_codes'])
                traceback = "\nException Traceback\n".join(result['exceptions']['traceback'])
                error = "{}\n{}\n{}".format(error, traceback, error)
            else:
                error = "There is not any infra-structure to allocate this database."

            task_history.update_status_for(TaskHistory.STATUS_ERROR, details=error)
            return


        database = Database.provision(name, result['databaseinfra'])
        database.team = team
        database.project = project
        database.description = description
        database.save()
        
        task_history.update_dbid(db=database)
        
        if Configuration.get_by_name_as_int('laas_integration') == 1:
            register_database_laas(database)
        
        task_history.update_status_for(TaskHistory.STATUS_SUCCESS, details='Database created successfully')
        
        return
        
    except Exception, e:
        traceback = full_stack()
        LOG.error("Ops... something went wrong: %s" % e)
        LOG.error(traceback)
        
        if 'database' in locals() and database.id:
            task_history.update_status_for(TaskHistory.STATUS_WARNING, details=traceback)
        else:
            if 'result' in locals() and result['created']:
                destroy_infra(databaseinfra = result['databaseinfra'], task=task_history)
            task_history.update_status_for(TaskHistory.STATUS_ERROR, details=traceback)
        return
        
    finally:
        AuditRequest.cleanup_request()



@app.task(bind=True)
def destroy_database(self, database, user=None):
    # register History
    AuditRequest.new_request("destroy_database", self.request.args[-1], "localhost")
    try:
        task_history = TaskHistory.register(request=self.request, user=user)
        LOG.info("id: %s | task: %s | kwargs: %s | args: %s" % (
            self.request.id, self.request.task, self.request.kwargs, str(self.request.args)))

        task_history.update_details(persist=True, details="Loading Process...")

        databaseinfra = database.databaseinfra
        database.delete()

        destroy_infra(databaseinfra=databaseinfra, task=task_history)

        task_history.update_status_for(TaskHistory.STATUS_SUCCESS, details='Database destroyed successfully')
        return
    finally:
        AuditRequest.cleanup_request()


@app.task(bind=True)
def clone_database(self, origin_database, clone_name, plan, environment, user=None):
    AuditRequest.new_request("clone_database", self.request.kwargs["user"], "localhost")
    try:
        LOG.info("id: %s | task: %s | kwargs: %s | args: %s" % (
            self.request.id, self.request.task, self.request.kwargs, str(self.request.args)))

        task_history = TaskHistory.register(request=self.request, user=user)

        LOG.info("origin_database: %s" % origin_database)

        dest_database = Database.objects.get(pk=origin_database.pk)
        dest_database.name = clone_name
        dest_database.pk = None

        task_history.update_details(persist=True, details="Loading Process...")
        result = make_infra(plan=plan, environment=environment, name=clone_name,
                            task=task_history)

        if result['created']==False:

            if 'exceptions' in result:
                error = "\n\n".join(": ".join(err) for err in result['exceptions']['error_codes'])
                traceback = "\n\nException Traceback\n".join(result['exceptions']['traceback'])
                error = "{}\n{}".format(error, traceback)
            else:
                error = "There is not any infra-structure to allocate this database."

            task_history.update_status_for(TaskHistory.STATUS_ERROR, details=error)
            return

        dest_database.databaseinfra = result['databaseinfra']
        dest_database.save()
        LOG.info("dest_database: %s" % dest_database)

        if Configuration.get_by_name_as_int('laas_integration') == 1:
            register_database_laas(dest_database)


        args = get_clone_args(origin_database, dest_database)
        script_name = factory_for(origin_database.databaseinfra).clone()
        return_code, output = call_script(script_name, working_dir=settings.SCRIPTS_PATH, args=args, split_lines=False)
        LOG.info("%s - return code: %s" % (self.request.id, return_code))

        if return_code != 0:
            task_history.update_status_for(TaskHistory.STATUS_ERROR, details=output + "\nTransaction rollback")
            LOG.error("task id %s - error occurred. Transaction rollback" % self.request.id)
            rollback_database(dest_database)
            if 'result' in locals() and result['created']:
                        destroy_infra(databaseinfra = result['databaseinfra'], task=task_history)
        else:
            task_history.update_dbid(db=dest_database)
            task_history.update_status_for(TaskHistory.STATUS_SUCCESS, details=output)
        return
    except SoftTimeLimitExceeded:
        LOG.error("task id %s - timeout exceeded" % self.request.id)
        task_history.update_status_for(TaskHistory.STATUS_ERROR, details="timeout exceeded")
        rollback_database(dest_database)
        if 'result' in locals() and result['created']:
                    destroy_infra(databaseinfra = result['databaseinfra'], task=task_history)
                    return
    except Exception, e:
        traceback = full_stack()
        LOG.error("Ops... something went wrong: %s" % e)
        LOG.error(traceback)

        rollback_database(dest_database)
        if 'result' in locals() and result['created']:
            destroy_infra(databaseinfra = result['databaseinfra'], task=task_history)

        task_history.update_status_for(TaskHistory.STATUS_ERROR, details=traceback)

        return

    finally:
        AuditRequest.cleanup_request()



@app.task
@only_one(key="db_infra_notification_key", timeout=20)
def databaseinfra_notification(self, user=None):
    task_history = TaskHistory.register(request=self.request, user=user)
    threshold_infra_notification = Configuration.get_by_name_as_int("threshold_infra_notification", default=0)
    if threshold_infra_notification <= 0:
        LOG.warning("database infra notification is disabled")
        return

    # Sum capacity per databseinfra with parameter plan, environment and engine
    infras = DatabaseInfra.objects.values('plan__name', 'environment__name', 'engine__engine_type__name',
                                          'plan__provider').annotate(capacity=Sum('capacity'))
    for infra in infras:
        # total database created in databaseinfra per plan, environment and engine
        used = DatabaseInfra.objects.filter(plan__name=infra['plan__name'],
                                            environment__name=infra['environment__name'],
                                            engine__engine_type__name=infra['engine__engine_type__name']).aggregate(
            used=Count('databases'))
        # calculate the percentage
        percent = int(used['used'] * 100 / infra['capacity'])
        if percent >= threshold_infra_notification and infra['plan__provider'] != Plan.CLOUDSTACK:
            LOG.info('Plan %s in environment %s with %s%% occupied' % (
                infra['plan__name'], infra['environment__name'], percent))
            LOG.info("Sending database infra notification...")
            context = {}
            context['plan'] = infra['plan__name']
            context['environment'] = infra['environment__name']
            context['used'] = used['used']
            context['capacity'] = infra['capacity']
            context['percent'] = percent
            email_notifications.databaseinfra_ending(context=context)

        task_history.update_status_for(TaskHistory.STATUS_SUCCESS,
                                       details='Databaseinfra Notification successfully sent to dbaas admins!')
    return


def database_notification_for_team(team=None):
    """
    Notifies teams of database usage.
    if threshold_database_notification <= 0, the notification is disabled.
    """
    from logical.models import Database

    LOG.info("sending database notification for team %s" % team)
    threshold_database_notification = Configuration.get_by_name_as_int("threshold_database_notification", default=0)
    # if threshold_database_notification
    if threshold_database_notification <= 0:
        LOG.warning("database notification is disabled")
        return

    databases = Database.objects.filter(team=team)
    msgs = []
    for database in databases:
        used = database.used_size_in_mb
        capacity = database.total_size_in_mb
        try:
            percent_usage = (used / capacity) * 100
        except ZeroDivisionError:
            #database has no total size
            percent_usage = 0.0
        msg = "database %s => usage: %.2f | threshold: %.2f" % (
            database, percent_usage, threshold_database_notification)
        LOG.info(msg)
        msgs.append(msg)

        if not team.email:
            msgs.append("team %s has no email set and therefore no database usage notification will been sent" % team)
        else:
            if percent_usage >= threshold_database_notification:
                LOG.info("Sending database notification...")
                context = {}
                context['database'] = database.name
                context['team'] = team
                context['measure_unity'] = "MB"
                context['used'] = used
                context['capacity'] = capacity
                context['percent'] = "%.2f" % percent_usage
                context['environment'] = database.environment.name
                email_notifications.database_usage(context=context)

    return msgs


@app.task(bind=True)
@only_one(key="db_notification_key", timeout=20)
def database_notification(self):
    """
    Create tasks for database notification by team
    if threshold_database_notification <= 0, the notification is disabled.
    """
    # get all teams and for each one create a new task
    LOG.info("retrieving all teams and sendind database notification")
    teams = Team.objects.all()
    msgs = {}

    for team in teams:
        ###############################################
        # create task
        ###############################################

        msgs[team] = database_notification_for_team(team=team)
        ###############################################

    try:
        LOG.info("Messages: ")
        LOG.info(msgs)

        task_history = TaskHistory.register(request=self.request, user=None)
        task_history.update_status_for(TaskHistory.STATUS_SUCCESS, details="\n".join(
            str(key) + ': ' + ', '.join(value) for key, value in msgs.items()))
    except Exception, e:
        task_history.update_status_for(TaskHistory.STATUS_ERROR, details=e)

    return

@app.task(bind=True)
@only_one(key="get_databases_status", timeout=50)
def update_database_status(self):
    LOG.info("Retrieving all databases")
    try:
        task_history = TaskHistory.register(request=self.request, user=None)
        databases = Database.objects.all()
        msgs = []
        for database in databases:
            if database.database_status.is_alive:
                database.status = Database.ALIVE
            else:
                database.status = Database.DEAD

            database.save()
            msg = "\nUpdating status for database: {}, status: {}".format(database, database.status)
            msgs.append(msg)
            LOG.info(msg)
        
        task_history.update_status_for(TaskHistory.STATUS_SUCCESS, details="\n".join(
            value for value in msgs))
    except Exception, e:
        task_history.update_status_for(TaskHistory.STATUS_ERROR, details=e)

    return

@app.task(bind=True)
@only_one(key="get_databases_used_size", timeout=50)
def update_database_used_size(self):
    LOG.info("Retrieving all databases")
    try:
        task_history = TaskHistory.register(request=self.request, user=None)
        databases = Database.objects.all()
        msgs = []
        for database in databases:
            if database.database_status:
                database.used_size_in_bytes = float(database.database_status.used_size_in_bytes)
            else:
                database.used_size_in_bytes = 0.0

            database.save()
            msg = "\nUpdating used size in bytes for database: {}, used size: {}".format(
                database, database.used_size_in_bytes)
            msgs.append(msg)
            LOG.info(msg)
        
        task_history.update_status_for(TaskHistory.STATUS_SUCCESS, details="\n".join(
            value for value in msgs))
    except Exception, e:
        task_history.update_status_for(TaskHistory.STATUS_ERROR, details=e)

    return

@app.task(bind=True)
@only_one(key="get_instances_status", timeout=50)
def update_instances_status(self):
    LOG.info("Retrieving all databaseinfras")
    task_history = TaskHistory.register(request=self.request, user=None)

    try:
        infras = DatabaseInfra.objects.all()
        msgs = []
        for databaseinfra in infras:
            LOG.info("Retrieving all instances for {}".format(databaseinfra))

            for instance in Instance.objects.filter(databaseinfra=databaseinfra, is_arbiter=False):
                if instance.check_status():
                    instance.status = Instance.ALIVE
                else:
                    instance.status = Instance.DEAD

                instance.save()

                msg = "\nUpdating instance status, instance: {}, status: {}".format(
                instance, instance.status)
                msgs.append(msg)
                LOG.info(msg)

        task_history.update_status_for(TaskHistory.STATUS_SUCCESS, details="\n".join(
            value for value in msgs))
    except Exception, e:
        task_history.update_status_for(TaskHistory.STATUS_ERROR, details=e)

    return



####################
#DBAAS_ACL_API_TASKS#
####################

try:
    from dbaas_aclapi.tasks import tasks
    from dbaas_aclapi import models
except ImportError, e:
    LOG.warn("DBaaS AclApi not installed")



@app.task(bind= True)
def bind_address_on_database(self, database, acl_environment, acl_vlan, action="permit", user=None):
    if not user:
        user =  self.request.args[-1]

    LOG.info("User: {}, action: {}".format(user, action))

    task_history = TaskHistory.register(request=self.request, user=user)
    LOG.info("id: %s | task: %s | kwargs: %s | args: %s" % (self.request.id, self.request.task, self.request.kwargs, str(self.request.args)))

    task_history.update_details(persist=True, details="Loading Process...")


    try:
        if action == "permit":
            bind_status = models.CREATING
        else:
            bind_status = models.DESTROYING

        LOG.info("Params database: {}, acl_environment: {}, acl_vlan: {}, action: {}, bind_status: {}".format(database, acl_environment,
            acl_vlan, action, bind_status))

        job = tasks.bind_unbind_address_on_database(database= database, acl_environment= acl_environment,
            acl_vlan=acl_vlan, action="permit", bind_status= bind_status)

        if not job:
            raise Exception, "Error when executing the Bind"

        task_history.update_status_for(TaskHistory.STATUS_SUCCESS, details='Bind created successfully')

        if bind_status == models.CREATING:
            bind_status = models.CREATED
        else:
            bind_status = models.ERROR

        LOG.debug("Bind Status: {}".format(bind_status))


        monitor_acl_job.delay(database, job, acl_environment+'/'+acl_vlan, bind_status, user=user)
        return

    except Exception,e:
        LOG.info("DatabaseBind ERROR: {}".format(e))
        task_history.update_status_for(TaskHistory.STATUS_ERROR, details='Bind could not be created')
        return

    finally:
        AuditRequest.cleanup_request()


@app.task(bind= True)
def monitor_acl_job(self,database, job_id, bind_address, bind_status=models.CREATED , user=None):
    if not user:
        user =  self.request.args[-1]
    AuditRequest.new_request("create_database",user, "localhost")

    task_history = TaskHistory.register(request=self.request, user=user)
    LOG.info("id: %s | task: %s | kwargs: %s | args: %s" % (self.request.id, self.request.task, self.request.kwargs, str(self.request.args)))

    task_history.update_details(persist=True, details="Loading Process...")
    try:

        LOG.debug("database: {}, job_id: {}, bind_address: {}, bind_status: {}, user: {}".format(database, job_id, bind_address, bind_status, user))

        status = tasks.monitor_acl_job(database, job_id, bind_address,)

        LOG.debug("Job status return: {}".format(status))
        if status:
            from dbaas_aclapi.util import update_bind_status
            LOG.info("Updating Bind Status")
            update_bind_status(database, bind_address, bind_status)

            task_history.update_status_for(TaskHistory.STATUS_SUCCESS, details='Bind created successfully')
            return
        else:
            raise Exception, "Error when monitoring the Bind Process"


    except Exception, e:
        LOG.info("DatabaseBindMonitoring ERROR: {}".format(e))
        task_history.update_status_for(TaskHistory.STATUS_ERROR, details='Bind could not be granted')
        return

    finally:
        AuditRequest.cleanup_request()

@app.task(bind=True)
def resize_database(self, database, cloudstackpack, user=None):

    AuditRequest.new_request("resize_database", user, "localhost")

    try:
        task_history = TaskHistory.register(request=self.request, user=user)

        from util.providers import resize_database

        result = resize_database(database = database, cloudstackpack = cloudstackpack, task = task_history)

        if result['created']==False:

            if 'exceptions' in result:
                error = "\n".join(": ".join(err) for err in result['exceptions']['error_codes'])
                traceback = "\nException Traceback\n".join(result['exceptions']['traceback'])
                error = "{}\n{}\n{}".format(error, traceback, error)
            else:
                error = "Something went wrong."

            task_history.update_status_for(TaskHistory.STATUS_ERROR, details=error)
        else:
            task_history.update_status_for(TaskHistory.STATUS_SUCCESS, details='Resize successfully done.')

    except Exception, e:
        error = "Resize Database ERROR: {}".format(e)
        LOG.error(error)
        task_history.update_status_for(TaskHistory.STATUS_ERROR, details=error)

    finally:
        AuditRequest.cleanup_request()

