# -*- coding: utf-8 -*-
from physical.models import Instance, Environment
from logical.models import Database
from account.models import Team
from dbaas_laas.provider import LaaSProvider
import json
import logging

LOG = logging.getLogger(__name__)

def get_users_for_team(team):
    users = []
    for user in team.users.all():
        users.append({
            "email" : user.email,
            "full_name" : user.get_full_name(),
            "username" : user.username
        })
    return users
    
def get_hosts_for_database(database):
    instances = Instance.objects.filter(databaseinfra=database.databaseinfra)
    hosts = []
    for instance in instances:
        if '.' in instance.hostname.hostname:
            hosts.append(instance.hostname.hostname.split('.')[0])
        else:
            hosts.append(hostname = instance.hostname.hostname)
    return hosts  

def get_group_name(database):
    return "DBaaS_%s_%s" % (database.databaseinfra.name, database.engine_type)
    
def register_database_laas(database):
    workspace_json = {}
    workspace_json["team"] = {
        "name" : database.team.name,
        "users" : get_users_for_team(database.team),
    }
    
    from util.providers import MYSQL, MONGODB, REDIS, get_engine
    engine = get_engine(database.engine_type)
    if engine == MYSQL:
        app = ["mysqld", "mysql-slow"]
    elif engine == MONGODB:
        app = ["mongod.27017"]
    elif engine == REDIS:
        app = ["redis"]
    else:
        app =[]

    hosts = get_hosts_for_database(database)

    groups = [{
        "filter_name" : "host:(%s) AND app:(%s)" % (" OR ".join(hosts), " OR ".join(app)),
        "name" : get_group_name(database)
    }]

    workspace_json["workspace"] = {
        "description" : "%s Workspace" % database.team.name,
        "name" : database.team.name,
        "groups" : groups
    }
    
    workspace_json = json.dumps(workspace_json)
    LOG.info("Register workspace on LaaS. Workspace info: %s" % (workspace_json))
    try:
        LaaSProvider.update_laas_workspace(environment=database.environment, laas_workspace=workspace_json)
    except Exception, e:
        LOG.error("Ops... something went wrong: %s" % e)    

def register_all_databases_laas():
    for database in Database.objects.all():
        register_database_laas(database)

def register_team_laas(team):
    for environment in Environment.objects.all():
        if team.databases_in_use_for(environment=environment):
            team_json = {}
            team_json["team"] = {
                "name" : team.name,
                "users" : get_users_for_team(team),
            }
            team_json = json.dumps(team_json)
            LOG.info("Register team on LaaS. Team info: %s" % (team_json))
            try:
                LaaSProvider.update_laas_team(environment=environment, laas_team=team_json)
            except Exception, e:
                LOG.error("Ops... something went wrong: %s" % e)

def register_all_teams_laas():
    for team in Team.objects.all():
        register_team_laas(team)