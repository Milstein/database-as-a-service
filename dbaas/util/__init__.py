# -*- coding: utf-8 -*-
from __future__ import absolute_import, unicode_literals
from time import sleep
import paramiko
import socket
import re
from slugify import slugify as slugify_function
from django.contrib.auth.models import User
from django.http import HttpResponse
import json
import logging
import subprocess
import signal
import os
import traceback, sys

LOG = logging.getLogger(__name__)

# See http://docs.python.org/2/library/subprocess.html#popen-constructor if you
# have questions about this variable
DEFAULT_OUTPUT_BUFFER_SIZE = 16384
PROCESS_TIMEOUT=4*60*60 # 4 horas


class AlarmException(Exception):
    pass

def alarm_handler(signum, frame):
    raise AlarmException

def slugify(string):
    return slugify_function(string, separator="_")

def make_db_random_password():
    return User.objects.make_random_password()

def as_json(f):
    def wrapper(request, *args, **kw):
        output = f(request, *args, **kw)
        if isinstance(output, HttpResponse):
            return output
        elif isinstance(output, basestring):
            return HttpResponse(output, content_type="text/plain")
        output = json.dumps(output, indent=4)
        return HttpResponse(output, content_type="application/json")
    return wrapper


def call_script(script_name, working_dir=None, split_lines=True, args=[], envs={}):

    args_copy = []
    for arg in args:
        if arg.startswith("PASSWORD"):
            args_copy.append("xxx")
        else:
            args_copy.append(arg)

    if not working_dir:
        raise RuntimeError("Working dir is null")

    logging_cmdline = "%s %s" % (
        " ".join([ "%s=%s" % (k, "xxx" if k.endswith("_PASSWORD") else v) for (k,v) in envs.items()]),
        " ".join([script_name] + args_copy),
    )
    return_code = None
    output = []
    try:
        LOG.debug('Running on path %s command: %s', working_dir, logging_cmdline)

        envs_with_path = {'PATH': os.getenv("PATH")}

        if envs:
            envs_with_path.update(envs)

        # For future, if scripts have lot of output can be better
        # create a temporary file for stdout. Scripts with lot of output and subprocess.PIPE
        # can lock because this method not consume stdout without script finish execute.
        process = subprocess.Popen(
            [working_dir + script_name] + args,
            bufsize=DEFAULT_OUTPUT_BUFFER_SIZE,
            stdin=None,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, # stderr and stdout are the same
            close_fds=True,
            cwd=working_dir,
            env=envs_with_path,
            universal_newlines=True)

        signal.signal(signal.SIGALRM, alarm_handler)
        signal.alarm(PROCESS_TIMEOUT)
        try:
            process.wait()
            signal.alarm(0) # Disable the alarm
        except AlarmException:
            LOG.error("Timeout %s exceeded for process id %s" % (PROCESS_TIMEOUT, process.pid))
            process.kill()

        output = process.stdout.read()
        return_code = process.returncode

        # if return_code != 0:
        #     raise RuntimeError("Error executing %s, exit code = %d: '%s'" % (script_name, return_code, output))

        if split_lines:
            return return_code, [s.strip() for s in output.splitlines()]
        else:
            return return_code, output
    except:
        # if any error happen, log cmdline to error
        LOG.error("Error running cmdline (exit code %s): %s", return_code, logging_cmdline, exc_info=True)
        if not return_code:
            return_code = 1

        return return_code, output


def check_nslookup(dns_to_check, dns_server, retries= 90, wait= 10):
    try:
        LOG.info("Cheking dns...")
        for attempt in range(0, retries):
            LOG.info("Cheking dns... attempt number %s..." % str(attempt + 1))

            result = subprocess.Popen("nslookup %s %s" % (dns_to_check, dns_server), stdout=subprocess.PIPE, shell=True)
            (output, err) = result.communicate()
            indexes = [i for i, x in enumerate(output.split('\n')) if re.match(r'\W*' + "Address" + r'\W*', x)]

            LOG.info("Nslookup output: %s" % output)

            if len(indexes)==2:
                LOG.info("%s is available!" % dns_to_check)
                return True
            sleep(wait)

        return False
    except Exception, e:
        LOG.warn("We caught an exception %s" % e)
        return None



def exec_remote_command(server, username, password, command, output={}):

    try:
        #LOG.info("Executing command [%s] on remote server %s" % (command, server))
        client = paramiko.SSHClient()
        client.load_system_host_keys()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        client.connect(server, username= username, password= password)

        stdin, stdout, stderr = client.exec_command(command)
        log_stdout = stdout.readlines()
        log_stderr = stderr.readlines()
        exit_status = stdout.channel.recv_exit_status()
        LOG.info("Comand return code: %s, stdout: %s, stderr %s" % (exit_status, log_stdout, log_stderr))
        output['stdout'] = log_stdout
        output['stderr'] = log_stderr
        return exit_status
    except (paramiko.ssh_exception.SSHException) as e:
        LOG.warning("We caught an exception: %s ." % (e))
        return None


def check_ssh(server, username, password, retries=6, wait=30, interval=40):
    username = username
    password = password
    ssh = paramiko.SSHClient()
    ssh.load_system_host_keys()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())

    LOG.info("Waiting %s seconds to check %s ssh connection..." % (wait, server))
    sleep(wait)

    for attempt in range(retries):
        try:

            LOG.info("Login attempt number %i on %s " % (attempt+1, server))

            ssh.connect( server, port=22, username=username,
                                 password=password, timeout= None, allow_agent= True,
                                 look_for_keys= True, compress= False)
            return True

        except (paramiko.ssh_exception.BadHostKeyException,
                    paramiko.ssh_exception.AuthenticationException,
                    paramiko.ssh_exception.SSHException,
                    socket.error) as e:

            if attempt == retries-1:
                LOG.error("Maximum number of login attempts : %s ." % (e))
                return False

            LOG.warning("We caught an exception: %s ." % (e))
            LOG.info("Wating %i seconds to try again..." % ( interval))
            sleep(interval)


def gen_infra_names(name, qt):
    import time
    import re

    stamp = str(time.time()).replace(".","")

    name = re.compile("[^\w']|_").sub("", name.lower())
    name = name[:10]

    names = {"infra": name + stamp, "vms":[]}

    for x in range(qt):
        names['vms'].append(name + "-0%i-" % (x+1)+ stamp)

    return names


def get_credentials_for(environment, credential_type):
    from dbaas_credentials.models import Credential
    return Credential.objects.filter(integration_type__type= credential_type, environments= environment)[0]


def build_dict(**kwargs):
    my_dict = {}
    for name, value in kwargs.items():
        my_dict[name] = value
    return my_dict

def full_stack():
    exc = sys.exc_info()[0]
    stack = traceback.extract_stack()[:-1]  # last one would be full_stack()
    if not exc is None:  # i.e. if an exception is present
        del stack[-1]       # remove call of full_stack, the printed exception
                            # will contain the caught exception caller instead
    trc = 'Traceback (most recent call last):\n'
    stackstr = trc + ''.join(traceback.format_list(stack))
    if not exc is None:
         stackstr += '  ' + traceback.format_exc().lstrip(trc)
    return stackstr

def dict_to_string(dict):
    ''.join('{}: {}'.format(key, val) for key, val in sorted(dict.items()))



def retry(ExceptionToCheck, tries=10, delay=3, backoff=2):
    import time
    def deco_retry(f):
        def f_retry(*args, **kwargs):
            mtries, mdelay = tries, delay
            while mtries > 0:
                try:
                    return f(*args, **kwargs)
                except ExceptionToCheck, e:
                    print "%s, Retrying in %d seconds..." % (str(e), mdelay)
                    time.sleep(mdelay)
                    mtries -= 1
                    mdelay *= backoff
                    lastException = e
            raise lastException
        return f_retry # true decorator
    return deco_retry

def build_context_script(contextdict, script):
    from django.template import Context, Template
    import re
    regex = re.compile(r'[\r]')
    script = regex.sub('', str(script))
    context = Context(contextdict)
    template = Template(script)
    return template.render(context)