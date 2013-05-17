import logging
import socket
import os
import sys
import time
import platform
try:
    import simplejson as json
except ImportError:
    import json
import httplib

from octopus.core.framework.mainloopapplication import MainLoopApplication
from octopus.core.communication.requestmanager import RequestManager
from octopus.core.enums import command as COMMAND
from octopus.core.enums import rendernode
from octopus.worker import settings
from octopus.worker.model.command import Command
from octopus.worker.process import spawnCommandWatcher

LOGGER = logging.getLogger("worker")
COMPUTER_NAME_TEMPLATE = "%s:%d"


class Worker(MainLoopApplication):

    class CommandWatcher(object):
        def __init__(self):
            self.id = None
            self.processId = None
            self.startTime = None
            self.processObj = None
            self.timeOut = None
            self.commandId = None
            self.command = None
            self.modified = True
            self.finished = False

    @property
    def modifiedCommandWatchers(self):
        return (watcher for watcher in self.commandWatchers.values() if watcher.modified)

    @property
    def finishedCommandWatchers(self):
        return (watcher for watcher in self.commandWatchers.values() if watcher.finished and not watcher.modified)

    def __init__(self, framework):
        super(Worker, self).__init__(self)
        LOGGER.info("Starting worker on %s:%d.", settings.ADDRESS, settings.PORT)
        self.framework = framework
        self.data = None
        self.requestManager = RequestManager(settings.DISPATCHER_ADDRESS,
                                             settings.DISPATCHER_PORT)
        self.commandWatchers = {}
        self.commands = {}
        self.port = settings.PORT
        self.computerName = COMPUTER_NAME_TEMPLATE % (settings.ADDRESS,
                                                      settings.PORT)
        self.lastSysInfosMessageTime = 0
        self.sysInfosMessagePeriod = 6
        self.httpconn = httplib.HTTPConnection(settings.DISPATCHER_ADDRESS, settings.DISPATCHER_PORT)
        self.PID_DIR = os.path.dirname(settings.PIDFILE)
        if not os.path.isdir(self.PID_DIR):
            LOGGER.warning("Worker pid directory does not exist, creating...")
            try:
                os.makedirs(self.PID_DIR, 0777)
                LOGGER.info("Worker pid directory created.")
            except OSError:
                LOGGER.error("Failed to create pid directory.")
                sys.exit(1)
        elif not os.access(self.PID_DIR, os.R_OK | os.W_OK):
            LOGGER.error("Missing read or write access on %s", self.PID_DIR)
            sys.exit(1)
        self.status = rendernode.RN_BOOTING
        self.updateSys = False
        self.isPaused = False
        self.toberestarted = False
        self.speed = 1.0
        self.cpuName = ""
        self.distrib = ""
        self.mikdistrib = ""
        self.openglversion = ""

    def prepare(self):
        for name in (name for name in dir(settings) if name.isupper()):
            LOGGER.info("settings.%s = %r", name, getattr(settings, name))
        self.registerWorker()

    def getNbCores(self):
        import multiprocessing
        return multiprocessing.cpu_count()

    def getTotalMemory(self):
        memTotal = 1024
        if os.path.isfile('/proc/meminfo'):
            try:
                # get total memory
                f = open('/proc/meminfo', 'r')
                for line in f.readlines():
                    if line.split()[0] == 'MemTotal:':
                        memTotal = line.split()[1]
                        f.close()
                        break
            except:
                pass
        return int(memTotal) / 1024

    def getCpuInfo(self):
        if os.path.isfile('/proc/cpuinfo'):
            try:
                # get cpu speed
                f = open('/proc/cpuinfo', 'r')
                for line in f.readlines():
                    if 'model name' in line:
                        self.cpuName = line.split(':')[1].strip()
                    elif 'MHz' in line:
                        speedStr = line.split(':')[1].strip()
                        self.speed = "%.1f" % (float(speedStr) / 1000)
                        break
                f.close()
            except:
                pass

    def getDistribName(self):
        if os.path.isfile('/etc/mik-release'):
            try:
                f = open('/etc/mik-release', 'r')
                for line in f.readlines():
                    if 'MIK-VERSION' in line or 'MIK-RELEASE' in line:
                        self.mikdistrib = line.split()[1]
                    elif 'openSUSE' in line:
                        if '=' in line:
                            self.distrib = line.split('=')[1].strip()
                        else:
                            self.distrib = line
                        break
                f.close()
            except:
                pass

    def getOpenglVersion(self):
        import subprocess
        import re
        p = subprocess.Popen("glxinfo", stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        output, errors = p.communicate()
        outputList = output.split("\n")
        for line in outputList:
            if "OpenGL version string" in line:
                LOGGER.info("found : %s" % line)
                oglpattern = re.compile("(\d.\d.\d)")
                res = oglpattern.search(line)
                self.openglversion = res.group()
                break

    def updateSysInfos(self, ticket):
        self.updateSys = True

    def fetchSysInfos(self):
        infos = {}
        if self.updateSys:
            self.getCpuInfo()
            self.getDistribName()
            self.getOpenglVersion()
            infos['cores'] = self.getNbCores()
            infos['ram'] = self.getTotalMemory()
            self.updateSys = False
            # system info values:
            infos['caracteristics'] = {"os": platform.system().lower(),
                                        "softs": [],
                                        "cpuname": self.cpuName,
                                        "distribname": self.distrib,
                                        "mikdistrib": self.mikdistrib,
                                        "openglversion": self.openglversion}
        infos['name'] = self.computerName
        infos['port'] = self.port
        infos['status'] = self.status
        infos['pools'] = []
        infos['speed'] = float(self.speed)
        return infos

    def setPerformanceIndex(self, ticket, performance):
        LOGGER.warning("set perf idx")
        dct = json.dumps({'performance': performance})
        headers = {}
        headers['content-length'] = len(dct)

        LOGGER.warning(dct)

        try:
            self.requestManager.put("/rendernodes/%s/sysinfos" % self.computerName, dct, headers)
        except RequestManager.RequestError, err:
            if err.status == 404:
                # the dispatcher doesn't know the worker
                # it may have been launched before the dispatcher itself
                # and not be mentioned in the tree.description file
                self.registerWorker()
            else:
                raise
        except httplib.BadStatusLine:
            LOGGER.exception('Sending sys infos has failed with a BadStatusLine error')

    def registerWorker(self):
        '''Register the worker in the dispatcher.'''
        self.updateSys = True
        infos = self.fetchSysInfos()
        dct = json.dumps(infos)
        # FIXME if a command is currently running on this worker, notify the dispatcher
        if len(self.commands.items()):
            dct['commands'] = self.commands.items()
        headers = {}
        headers['content-length'] = len(dct)

        while True:
            try:
                LOGGER.info("Boot process... registering worker")
                url = "/rendernodes/%s/" % self.computerName
                resp = self.requestManager.post(url, dct, headers)
            except RequestManager.RequestError, e:
                if e.status != 409:
                    msg = "Dispatcher (%s:%s) is not reachable. We'll retry..."
                    msg %= (settings.DISPATCHER_ADDRESS, settings.DISPATCHER_PORT)
                    LOGGER.exception(msg)
                else:
                    LOGGER.info("Boot process... worker already registered")
                    break
            else:
                if resp == 'ERROR':
                    LOGGER.warning('Worker registration failed.')
                else:
                    LOGGER.info("Boot process... worker registered")
                    break
            # try to register to dispatcher every 10 seconds
            time.sleep(10)

        # once the worker is registered, ensure the RN status is correct according to the killfile presence
        if os.path.isfile(settings.KILLFILE):
            self.pauseWorker(True, False)
        else:
            self.pauseWorker(False, False)

        self.sendSysInfosMessage()

    def buildUpdateDict(self, command):
        dct = {}
        if command.completion is not None:
            dct["completion"] = command.completion
        if command.status is not None:
            dct["status"] = command.status
        if command.validatorMessage is not None:
            dct["validatorMessage"] = command.validatorMessage
        if command.errorInfos is not None:
            dct["errorInfos"] = command.errorInfos
        dct['message'] = command.message
        dct['id'] = command.id
        return dct

    def updateCommandWatcher(self, commandWatcher):
        while True:
            url = "/rendernodes/%s/commands/%d/" % (self.computerName, commandWatcher.commandId)
            body = json.dumps(self.buildUpdateDict(commandWatcher.command))
            headers = {'Content-Length': len(body)}
            try:
                self.httpconn.request('PUT', url, body, headers)
                response = self.httpconn.getresponse()
            except httplib.HTTPException:
                LOGGER.exception('"PUT %s" failed', url)
            except socket.error:
                LOGGER.exception('"PUT %s" failed', url)
            else:
                if response.status == 200:
                    response = response.read()
                    commandWatcher.modified = False
                elif response.status == 404:
                    LOGGER.warning('removing stale command %d', commandWatcher.commandId)
                    response = response.read()
                    self.removeCommandWatcher(commandWatcher)
                else:
                    data = response.read()
                    print "unexpected status %d: %s %s" % (response.status, response.reason, data)
                return
            finally:
                self.httpconn.close()
            LOGGER.warning('Update of command %d failed.', commandWatcher.commandId)

    def pauseWorker(self, paused, killproc):
        while True:
            url = "/rendernodes/%s/paused/" % (self.computerName)
            dct = {}
            dct['paused'] = paused
            dct['killproc'] = killproc
            body = json.dumps(dct)
            headers = {'Content-Length': len(body)}
            try:
                self.httpconn.request('PUT', url, body, headers)
                response = self.httpconn.getresponse()
            except httplib.HTTPException:
                LOGGER.exception('"PUT %s" failed', url)
            except socket.error:
                LOGGER.exception('"PUT %s" failed', url)
            else:
                if response.status == 200:
                    if paused:
                        self.status = rendernode.RN_PAUSED
                        self.isPaused = True
                        LOGGER.info("Worker has been put in paused mode")
                    else:
                        self.status = rendernode.RN_IDLE
                        self.isPaused = False
                        LOGGER.info("Worker awakes from paused mode")
                return
            finally:
                self.httpconn.close()

    def killCommandWatchers(self):
        for commandWatcher in self.commandWatchers.values():
            LOGGER.warning("Aborting command %d", commandWatcher.commandId)
            commandWatcher.processObj.kill()
            commandWatcher.finished = True

    def mainLoop(self):
        # try:
        now = time.time()

        # check if the killfile is present
        #
        if os.path.isfile(settings.KILLFILE):
            if not self.isPaused:
                with open(settings.KILLFILE, 'r') as f:
                    data = f.read()
                if len(data) != 0:
                    data = int(data)
                LOGGER.warning("Killfile detected, pausing worker")
                # kill cmd watchers, if the flag in the killfile is set to -1
                killproc = False
                if data == -1:
                    LOGGER.warning("Flag -1 detected in killfile, killing render")
                    killproc = True
                    self.killCommandWatchers()
                if data == -2:
                    LOGGER.warning("Flag -2 detected in killfile, schedule restart")
                    self.toberestarted = True
                if data == -3:
                    LOGGER.warning("Flag -3 detected in killfile, killing render and schedule restart")
                    killproc = True
                    self.toberestarted = True
                    self.killCommandWatchers()
                self.pauseWorker(True, killproc)
        else:
            self.toberestarted = False
            # if no killfile present and worker is paused, unpause it
            if self.isPaused:
                self.pauseWorker(False, False)

        # if the worker is paused and marked to be restarted, create restartfile
        if self.isPaused and self.toberestarted:
            LOGGER.warning("Restarting...")
            rf = open("/tmp/render/restartfile", 'w')
            rf.close()

        # Waits for any child process, non-blocking (this is necessary to clean up finished process properly)
        #
        try:
            pid, stat = os.waitpid(-1, os.WNOHANG)
            if pid:
                LOGGER.warning("Cleaned process %s" % str(pid))
        except OSError:
            pass

        # Send updates for every modified command watcher.
        #
        for commandWatcher in self.modifiedCommandWatchers:
            self.updateCommandWatcher(commandWatcher)

        # Attempt to remove finished command watchers
        #
        for commandWatcher in self.finishedCommandWatchers:
            LOGGER.info("Removing command watcher %d (status=%r, finished=%r, modified=%r)", commandWatcher.command.id, commandWatcher.command.status, commandWatcher.finished, commandWatcher.modified)
            self.removeCommandWatcher(commandWatcher)

        # Kill watchers that timeout and remove dead command watchers
        # that are not flagged as modified.
        #
        for commandWatcher in self.commandWatchers.values():
            # add the test on running state because a non running command can not timeout (Olivier Derpierre 17/11/10)
            if commandWatcher.timeOut and commandWatcher.command.status == COMMAND.CMD_RUNNING:
                responding = (now - commandWatcher.startTime) <= commandWatcher.timeOut
                if not responding:
                    # time out has been reached
                    LOGGER.warning("Timeout on command %d", commandWatcher.commandId)
                    commandWatcher.processObj.kill()
                    commandWatcher.finished = True
                    self.updateCompletionAndStatus(commandWatcher.commandId, None, COMMAND.CMD_CANCELED, None)

        # time resync
        now = time.time()
        if (now - self.lastSysInfosMessageTime) > self.sysInfosMessagePeriod:
            self.sendSysInfosMessage()
            self.lastSysInfosMessageTime = now

        self.httpconn.close()

        # let's be CPU friendly
        time.sleep(0.05)
        # except:
        #     LOGGER.error("A problem occured : " + repr(sys.exc_info()))

    def sendSysInfosMessage(self):
        # only send sysinfos message if the worker is not paused
        LOGGER.info("status is %d" % self.status)
        if self.status is not rendernode.RN_PAUSED:
            # we don't need to send the whole dict of sysinfos
            #infos = self.fetchSysInfos()
            infos = {}
            infos['status'] = self.status
            dct = json.dumps(infos)
            headers = {}
            headers['content-length'] = len(dct)

            try:
                self.requestManager.put("/rendernodes/%s/sysinfos" % self.computerName, dct, headers)
            except RequestManager.RequestError, err:
                if err.status == 404:
                    # the dispatcher doesn't know the worker
                    # it may have been launched before the dispatcher itself
                    # and not be mentioned in the tree.description file
                    self.registerWorker()
                else:
                    raise
            except httplib.BadStatusLine:
                LOGGER.exception('Sending sys infos has failed with a BadStatusLine error')

    def connect(self):
        return httplib.HTTPConnection(settings.DISPATCHER_ADDRESS, settings.DISPATCHER_PORT)

    def removeCommandWatcher(self, commandWatcher):
        print "\nREMOVING COMMAND WATCHER %d\n" % commandWatcher.command.id
        LOGGER.info('Removing command watcher for command %d', commandWatcher.commandId)
        del self.commandWatchers[commandWatcher.commandId]
        del self.commands[commandWatcher.commandId]
        try:
            os.remove(commandWatcher.processObj.pidfile)
            self.status = rendernode.RN_IDLE
        except OSError, e:
            from errno import ENOENT
            err, msg = e.args
            LOGGER.exception(msg)
            if err != ENOENT:
                raise

    def updateCompletionAndStatus(self, commandId, completion, status, message):
        try:
            commandWatcher = self.commandWatchers[commandId]
        except KeyError:
            LOGGER.warning("attempt to update completion and status of unregistered  command %d", commandId)
        else:
            commandWatcher.modified = True
            if commandWatcher.command.status == COMMAND.CMD_CANCELED:
                return
            if completion is not None:
                commandWatcher.command.completion = completion
            if message is not None:
                commandWatcher.command.message = message
            if status is not None:
                commandWatcher.command.status = status
                if COMMAND.isFinalStatus(status):
                    commandWatcher.finished = True

    def addCommandApply(self, ticket, commandId, runner, arguments, validationExpression, taskName, relativePathToLogDir, environment):
        if not self.isPaused:
            newCommand = Command(commandId, runner, arguments, validationExpression, taskName, relativePathToLogDir, environment=environment)
            self.commands[commandId] = newCommand
            self.addCommandWatcher(newCommand)
            LOGGER.info("Added command %d {runner: %s, arguments: %s}", commandId, runner, repr(arguments))

    ##
    #
    # @param id the integer value identifying the command
    # @todo add a ticket parameter
    # @todo find a clean way to stop the processes so that they \
    #       can call their after-execution scripts
    #
    def stopCommandApply(self, ticket, commandId):
        commandWatcher = self.commandWatchers[commandId]
        commandWatcher.processObj.kill()
        self.updateCompletionAndStatus(commandId, 0, COMMAND.CMD_CANCELED, "killed")
        LOGGER.info("Stopped command %r", commandId)

    def updateCommandApply(self, ticket, commandId, status, completion, message):
        self.updateCompletionAndStatus(commandId, completion, status, message)
        LOGGER.info("Updated command id=%r status=%r completion=%r message=%r" % (commandId, status, completion, message))

    def updateCommandValidationApply(self, ticket, commandId, validatorMessage, errorInfos):
        try:
            commandWatcher = self.commandWatchers[commandId]
        except KeyError:
            ticket.status = ticket.ERROR
            ticket.message = "No such command watcher."
        else:
            commandWatcher.command.validatorMessage = validatorMessage
            commandWatcher.command.errorInfos = errorInfos
            LOGGER.info("Updated validation info id=%r validatorMessage=%r errorInfos=%r" % (commandId, validatorMessage, errorInfos))

    def addCommandWatcher(self, command):
        newCommandWatcher = self.CommandWatcher()
        newCommandWatcher.commandId = command.id

        commandId = command.id

        from octopus.commandwatcher import commandwatcher
        logdir = os.path.join(settings.LOGDIR, command.relativePathToLogDir)
        outputFile = os.path.join(logdir, '%d.log' % (commandId))
        commandWatcherLogFile = outputFile

        scriptFile = commandwatcher.__file__

        workerPort = self.framework.webService.port
        pythonExecutable = sys.executable

        pidFile = os.path.join(self.PID_DIR, "cw%s.pid" % newCommandWatcher.commandId)

        # create the logdir if it does not exist
        if not os.path.exists(logdir):
            try:
                os.makedirs(logdir, 0777)
            except OSError, e:
                import errno
                err = e.args[0]
                if err != errno.EEXIST:
                    raise
        logFile = file(outputFile, "w")

        d = os.path.dirname(pidFile)
        if not os.path.exists(d):
            try:
                os.makedirs(d, 0777)
            except OSError, e:
                err = e.args[0]
                if err != errno.EEXIST:
                    raise

        args = [
            pythonExecutable,
            "-u",
            scriptFile,
            commandWatcherLogFile,
            str(workerPort),
            str(command.id),
            command.runner,
            command.validationExpression,
        ]
        args.extend(('%s=%s' % (str(name), str(value)) for (name, value) in command.arguments.items()))

        watcherProcess = spawnCommandWatcher(pidFile, logFile, args, command.environment)
        newCommandWatcher.processObj = watcherProcess
        newCommandWatcher.startTime = time.time()
        newCommandWatcher.timeOut = None
        newCommandWatcher.command = command
        newCommandWatcher.processId = watcherProcess.pid

        self.commandWatchers[command.id] = newCommandWatcher

        LOGGER.info("Started command %d", command.id)
