# -*- coding: utf-8 -*-
from django.utils.module_loading import import_by_path
import logging
import time
from exceptions.error_codes import DBAAS_0001
from util import full_stack

LOG = logging.getLogger(__name__)


def start_workflow(workflow_dict, task=None):
    try:
        if not 'steps' in workflow_dict:
            return False
        workflow_dict['step_counter'] = 0

        workflow_dict['msgs'] = []
        workflow_dict['status'] = 0
        workflow_dict['total_steps'] = len(workflow_dict['steps'])
        workflow_dict['exceptions'] = {}
        workflow_dict['exceptions']['traceback'] = []
        workflow_dict['exceptions']['error_codes'] = []

        for step in workflow_dict['steps']:
            workflow_dict['step_counter'] += 1

            my_class = import_by_path(step)
            my_instance = my_class()

            time_now = str(time.strftime("%m/%d/%Y %H:%M:%S"))

            msg = "\n%s - Step %i of %i - %s" % (
                time_now, workflow_dict['step_counter'], workflow_dict['total_steps'], str(my_instance))

            LOG.info(msg)

            if task:
                workflow_dict['msgs'].append(msg)
                task.update_details(persist=True, details=msg)

            if my_instance.do(workflow_dict) != True:
                workflow_dict['status'] = 0
                raise Exception("We caught an error while executing the steps...")

            workflow_dict['status'] = 1
            if task:
                task.update_details(persist=True, details="DONE!")

        workflow_dict['created'] = True

        return True

    except Exception, e:

        if not workflow_dict['exceptions']['error_codes'] or not workflow_dict['exceptions']['traceback']:
            traceback = full_stack()
            workflow_dict['exceptions']['error_codes'].append(DBAAS_0001)
            workflow_dict['exceptions']['traceback'].append(traceback)

        LOG.warn("\n".join( ": ".join(error) for error in workflow_dict['exceptions']['error_codes']))
        LOG.warn("\nException Traceback\n".join(workflow_dict['exceptions']['traceback']))


        workflow_dict['steps'] = workflow_dict[
                                     'steps'][:workflow_dict['step_counter']]
        stop_workflow(workflow_dict, task)

        workflow_dict['created'] = False

        return False


def stop_workflow(workflow_dict, task=None):
    LOG.info("Running undo...")

    if not 'exceptions' in workflow_dict:
        workflow_dict['exceptions'] = {}
        workflow_dict['exceptions']['traceback'] = []
        workflow_dict['exceptions']['error_codes'] = []
    
    workflow_dict['total_steps'] = len(workflow_dict['steps'])
    if 'step_counter' not in workflow_dict:
        workflow_dict['step_counter'] = len(workflow_dict['steps'])
    workflow_dict['msgs'] = []

    try:

        for step in workflow_dict['steps'][::-1]:

            my_class = import_by_path(step)
            my_instance = my_class()

            LOG.info("Rollback Step %i %s " % (workflow_dict['step_counter'], str(my_instance)))

            time_now = str(time.strftime("%m/%d/%Y %H:%M:%S"))

            msg = "\n%s - Rollback Step %i of %i - %s" % (
                time_now, workflow_dict['step_counter'], workflow_dict['total_steps'], str(my_instance))

            LOG.info(msg)

            workflow_dict['step_counter'] -= 1
            
            if task:
                workflow_dict['msgs'].append(msg)
                task.update_details(persist=True, details=msg)

            my_instance.undo(workflow_dict)

            if task:
                task.update_details(persist=True, details="DONE!")
                
        return True
    except Exception, e:

        if not workflow_dict['exceptions']['error_codes'] or not workflow_dict['exceptions']['traceback']:
            traceback = full_stack()
            workflow_dict['exceptions']['error_codes'].append(DBAAS_0001)
            workflow_dict['exceptions']['traceback'].append(traceback)

        LOG.warn("\n".join( ": ".join(error) for error in workflow_dict['exceptions']['error_codes']))
        LOG.warn("\nException Traceback\n".join(workflow_dict['exceptions']['traceback']))

        return False
