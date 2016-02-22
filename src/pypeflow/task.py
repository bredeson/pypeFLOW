
# @author Jason Chin
#
# Copyright (C) 2010 by Jason Chin 
# Copyright (C) 2011 by Jason Chin, Pacific Biosciences
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in
# all copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN
# THE SOFTWARE.

"""

PypeTask: This module provides the PypeTask class and the decorators that can convert 
a regular python funtion into a PypeTask instance. 

"""

import pprint
import inspect
import hashlib
import logging
import copy
import sys

PYTHONVERSION = sys.version_info[:2]
if PYTHONVERSION  < (3,0):
    range = xrange

if PYTHONVERSION == (2,5):
    import simplejson as json
else:
    import json

import os
import shlex

from common import PypeError, PypeObject, pypeNS, runShellCmd, Graph, URIRef, Literal
from data import FileNotExistError, PypeSplittableLocalFile, makePypeLocalFile

logger = logging.getLogger(__name__)

class TaskFunctionError(PypeError):
    pass

# These must be strings.
TaskInitialized = "TaskInitialized"
TaskDone = "done"
TaskFail = "fail"
# TODO(CD): Make user-code compare by variable name.

class PypeTaskBase(PypeObject):
    """
    Represent a PypeTask. Subclass it to for different kind of
    task.
    """

    supportedURLScheme = ["task"]

    def __init__(self, URL, *argv, **kwargv):

        """
        Constructor of a PypeTask.
        """

        PypeObject.__init__(self, URL, **kwargv)

        self._argv = argv
        self._kwargv = kwargv
        self._taskFun = kwargv['_taskFun']
        self._referenceMD5 = None
        self._status = TaskInitialized
        self._queue = None
        self.shutdown_event = None
        

        for defaultAttr in ["inputDataObjs", "outputDataObjs", "parameters", "mutableDataObjs"]:
            if defaultAttr not in self.__dict__:
                self.__dict__[defaultAttr] = {}

        # "input" and "output" short cut
        if "inputs" in kwargv:
            self.inputDataObjs.update(kwargv["inputs"])
            del kwargv["inputs"]

        if "outputs" in kwargv:
            self.outputDataObjs.update(kwargv["outputs"])
            del kwargv["outputs"]

        if "mutables" in kwargv:
            self.mutableDataObjs.update(kwargv["mutables"])
            del kwargv["mutables"]

        #the keys in inputDataObjs/outputDataObjs/parameters will become a task attribute 
        for defaultAttr in ["inputDataObjs", "outputDataObjs", "mutableDataObjs", "parameters"]:
            vars(self).update(self.__dict__[defaultAttr]) 

        if "chunk_id" in kwargv:
            self.chunk_id = kwargv["chunk_id"]

        self._codeMD5digest = kwargv.get("_codeMD5digest", "")
        self._paramMD5digest = kwargv.get("_paramMD5digest", "")
        self._compareFunctions = kwargv.get("_compareFunctions", [ timeStampCompare ])

        for o in self.outputDataObjs.values():
            if o.readOnly is True:
                raise PypeError, "Cannot assign read only data object %s for task %s" % (o.URL, self.URL) 
        
    @property
    def status(self):
        return self._status
        
    def setInputs( self, inputDataObjs ):
        self.inputDataObjs = inputDataObjs
        vars(self).update( inputDataObjs )
        
    def setOutputs( self, outputDataObjs ):
        self.outputDataObjs = outputDataObjs
        vars(self).update( outputDataObjs )
        
    def setReferenceMD5(self, md5Str):
        self._referenceMD5 = md5Str

    def _getRunFlag(self):
        """Determine whether the PypeTask should be run. It can be overridden in
        subclass to allow more flexible rules.
        """
        runFlag = False
        if self._referenceMD5 is not None and self._referenceMD5 != self._codeMD5digest:
            self._referenceMD5 = self._codeMD5digest
            # Code has changed.
            return True
        return any( [ f(self.inputDataObjs, self.outputDataObjs, self.parameters) for f in self._compareFunctions] )

    def isSatisfied(self):
        """Compare dependencies. (Kinda expensive.)
        Note: Do not call this while the task is actually running!
        """
        return not self._getRunFlag()

    def getStatus(self):
        """
        Note: Do not call this while the task is actually running!
        """
        return self._status

    def setStatus(self, status):
        """
        Note: Do not call this while the task is actually running!
        """
        assert status in (TaskInitialized, TaskDone, TaskFail)
        self._status = status

    def _runTask(self, *argv, **kwargv):
        """ 
        The method to run the decorated function _taskFun(). It is called through run() of
        the PypeTask object and it should never be called directly

        TODO: the arg porcessing is still a mess, need to find a better way to do this 
        """
        if PYTHONVERSION == (2,5): #TODO(CD): Does this even work anymore?
            (args, varargs, varkw, defaults)  = inspect.getargspec(self._taskFun)
            #print(args, varargs, varkw, defaults)
        else:
            argspec = inspect.getargspec(self._taskFun)
            (args, varargs, varkw, defaults) = argspec.args, argspec.varargs, argspec.keywords, argspec.defaults

        if varkw != None:
            return self._taskFun(self, *argv, **kwargv)
        elif varargs != None:
            return self._taskFun(self, *argv)
        elif len(args) != 0:
            nkwarg = {}
            if defaults != None:
                defaultArg = args[-len(defaults):]
                for a in defaultArg:
                    nkwarg[a] = kwargv[a]
                return self._taskFun(self, *argv, **nkwarg)
            else:
                return self._taskFun(self)
        else:
            return self._taskFun(self)

    @property
    def _RDFGraph(self):
        graph = Graph()
        for k,v in self.__dict__.iteritems():
            if k == "URL": continue
            if k[0] == "_": continue
            if k in ["inputDataObjs", "outputDataObjs", "mutableDataObjs", "parameters"]:
                if k == "inputDataObjs":
                    for ft, f in v.iteritems():
                        graph.add( (URIRef(self.URL), pypeNS["prereq"], URIRef(f.URL) ) )
                elif k == "outputDataObjs":
                    for ft, f in v.iteritems():
                        graph.add( (URIRef(f.URL), pypeNS["prereq"], URIRef(self.URL) ) )
                elif k == "mutableDataObjs":
                    for ft, f in v.iteritems():
                        graph.add( (URIRef(self.URL), pypeNS["hasMutable"], URIRef(f.URL)   ) )
                elif k == "parameters":
                    graph.add( (URIRef(self.URL), pypeNS["hasParameters"], Literal(json.dumps(v)) ) )
            
                continue

            if k in self.inputDataObjs:
                graph.add( ( URIRef(self.URL), pypeNS["inputDataObject"], URIRef(v.URL) ) )
                continue

            if k in self.outputDataObjs:
                graph.add( ( URIRef(self.URL), pypeNS["outputDataObject"], URIRef(v.URL) ) )
                continue

            if k in self.mutableDataObjs:
                graph.add( ( URIRef(self.URL), pypeNS["mutableDataObject"], URIRef(v.URL) ) )
                continue

            if hasattr(v, "URL"):
                graph.add( ( URIRef(self.URL), pypeNS[k], URIRef(v.URL) ) )

            graph.add(  ( URIRef(self.URL), pypeNS["codeMD5digest"], Literal(self._codeMD5digest) ) )
            graph.add(  ( URIRef(self.URL), pypeNS["parameterMD5digest"], Literal(self._paramMD5digest) ) )

        return graph

    def __call__(self, *argv, **kwargv):
        """Trap all exceptions, set fail flag, log, and re-raise.
        If you need to do more, then over-ride this method.
        """
        try:
            return self.run(*argv, **kwargv)
        except: # and re-raise
            logger.exception('PypeTaskBase failed unexpectedly:\n%r' %self)
            self._status = TaskFail
            raise

    @staticmethod
    def syncDirectories(fns):
        # need the following loop to force the stupid Islon to update the metadata in the directory
        # otherwise, the file would be appearing as non-existence... sigh, this is a >5 hours hard earned hacks
        # Yes, a friend at AMD had this problem too. Painful. ~cd
        for d in set(os.path.dirname(fn) for fn in fns):
            try:
                os.listdir(d)
            except OSError:
                pass

    def run(self, *argv, **kwargv):
        """Determine whether a task should be run when called.
        If the dependency is not satisified,
        then the _taskFun() will be called to generate the output data objects.

        Derived class can over-ride this method, but if __call__ is over-ridden,
        then derived must call this explicitly.
        """
        argv = list(argv)
        argv.extend(self._argv)
        kwargv.update(self._kwargv)

        inputDataObjs = self.inputDataObjs
        self.syncDirectories([o.localFileName for o in inputDataObjs.values()])

        outputDataObjs = self.outputDataObjs
        parameters = self.parameters

        logger.info('Running task from function %s()' %(self._taskFun.__name__))
        rtn = self._runTask(self, *argv, **kwargv)

        if self.inputDataObjs != inputDataObjs or self.parameters != parameters:
            raise TaskFunctionError("The 'inputDataObjs' and 'parameters' should not be modified in %s" % self.URL)
        missing = [(k,o) for (k,o) in self.outputDataObjs.iteritems() if not o.exists]
        if missing:
            logger.debug("%s fails to generate all outputs; missing:\n%s" %(self.URL, pprint.pformat(missing)))
            self._status = TaskFail
        else:
            self._status = TaskDone

        return True # to indicate that it run, since we no longer rely on runFlag

    def __repr__(self):
        r = dict()
        r['_status'] = self._status
        r['inputDataObjs'] = self.inputDataObjs
        r['outputDataObjs'] = self.outputDataObjs
        r['mutableDataObjs'] = self.mutableDataObjs
        r['parameters'] = self.parameters
        r['__class__.__name__'] = self.__class__.__name__
        return pprint.pformat(r)

    def finalize(self): 
        """ 
        This method is intended to be overriden by subclass to provide extra processing that is not 
        directed related to the processing the input and output data. For the thread workflow, this
        method will be called in the main thread after a take is finished regardless the job status.
        """
        pass

class PypeThreadTaskBase(PypeTaskBase):

    """
    Represent a PypeTask that can be run within a thread. 
    Subclass it to for different kind of task.
    """

    @property
    def nSlots(self):
        """
        Return the required number of slots to run, total number of slots is determined by 
        PypeThreadWorkflow.MAX_NUMBER_TASK_SLOT, increase this number by passing desired number 
        through the "parameters" argument (e.g parameters={"nSlots":2}) to avoid high computationa 
        intensive job running concurrently in local machine One can set the max number of thread 
        of a workflow by PypeThreadWorkflow.setNumThreadAllowed()
        """
        try:
            nSlots = self.parameters["nSlots"]
        except AttributeError:
            nSlots = 1
        except KeyError:
            nSlots = 1
        return nSlots


    def setMessageQueue(self, q):
        self._queue = q

    def setShutdownEvent(self, e):
        self.shutdown_event = e

    def __call__(self, *argv, **kwargv):
        """Trap all exceptions, set fail flag, SEND MESSAGE, log, and re-raise.
        """
        try:
            return self.runInThisThread(*argv, **kwargv)
        except: # and re-raise
            logger.exception('PypeTaskBase failed:\n%r' %self)
            self._status = TaskFail  # TODO: Do not touch internals of base class.
            self._queue.put( (self.URL, TaskFail) )
            raise

    def runInThisThread(self, *argv, **kwargv):
        """
        Similar to the PypeTaskBase.run(), but it provide some machinary to pass information
        back to the main thread that run this task in a sepearated thread through the standard python
        queue from the Queue module.
        """
        if self._queue == None:
            logger.debug('Testing threads w/out queue?')
            self.run(*argv, **kwargv)
            # return
            # raise until we know what this should do.
            raise Exception('There seems to be a case when self.queue==None, so we need to let this block simply return.')

        self._queue.put( (self.URL, "started, runflag: %d" % True) )
        self.run(*argv, **kwargv)

        self.syncDirectories([o.localFileName for o in self.outputDataObjs.values()])

        self._queue.put( (self.URL, self._status) )

class PypeDistributiableTaskBase(PypeThreadTaskBase):

    """
    Represent a PypeTask that can be run within a thread or submit to
    a grid-engine like job scheduling system. 
    Subclass it to for different kind of task.
    """

    def __init__(self, URL, *argv, **kwargv):
        PypeTaskBase.__init__(self, URL, *argv, **kwargv)
        self.distributed = True


class PypeTaskCollection(PypeObject):

    """
    Represent an object that encapsules a number of tasks
    """

    supportedURLScheme = ["tasks"]
    def __init__(self, URL, tasks = [], scatterGatherTasks = [], **kwargv):
        PypeObject.__init__(self, URL, **kwargv)
        self._tasks = tasks[:]
        self._scatterGatherTasks = scatterGatherTasks[:]

    def addTask(self, task):
        self._tasks.append(task)

    def getTasks(self):
        return self._tasks

    def addScatterGatherTask(self, task):
        self._scatterGatherTasks.append(task)

    def getScatterGatherTasks(self):
        return self._scatterGatherTasks

    def __getitem__(self, k):
        return self._tasks[k]

_auto_names = set()
def _unique_name(name):
    """
    >>> def foo(): pass
    >>> _unique_name('foo')
    'foo'
    >>> _unique_name('foo')
    'foo.01'
    >>> _unique_name('foo')
    'foo.02'
    """
    if name in _auto_names:
        n = 0
        while True:
            n += 1
            try_name = '%s.%02d' %(name, n)
            if try_name not in _auto_names:
                break
        name = try_name
    _auto_names.add(name)
    return name
def _auto_task_url(taskFun):
    # Note: in doctest, the filename would be weird.
    return "task://" + inspect.getfile(taskFun) + "/"+ _unique_name(taskFun.func_name)

def PypeTask(*argv, **kwargv):

    """
    A decorator that converts a function into a PypeTaskBase object.

    >>> import os, time 
    >>> from pypeflow.data import PypeLocalFile, makePypeLocalFile, fn
    >>> from pypeflow.task import *
    >>> try:
    ...     os.makedirs("/tmp/pypetest")
    ...     _ = os.system("rm -f /tmp/pypetest/*")   
    ... except Exception:
    ...     pass
    >>> time.sleep(.1)
    >>> fin = makePypeLocalFile("/tmp/pypetest/testfile_in", readOnly=False)
    >>> fout = makePypeLocalFile("/tmp/pypetest/testfile_out", readOnly=False)
    >>> @PypeTask(outputs={"test_out":fout},
    ...           inputs={"test_in":fin},
    ...           parameters={"a":'I am "a"'}, **{"b":'I am "b"'})
    ... def test(self):
    ...     print test.test_in.localFileName
    ...     print test.test_out.localFileName
    ...     os.system( "touch %s" % fn(test.test_out) )
    ...     print self.test_in.localFileName
    ...     print self.test_out.localFileName
    ...     pass
    >>> type(test) 
    <class 'pypeflow.task.PypeTaskBase'>
    >>> test.test_in.localFileName
    '/tmp/pypetest/testfile_in'
    >>> test.test_out.localFileName
    '/tmp/pypetest/testfile_out'
    >>> os.system( "touch %s" %  ( fn(fin))  )
    0
    >>> timeStampCompare(test.inputDataObjs, test.outputDataObjs, test.parameters)
    True
    >>> print test._getRunFlag()
    True
    >>> test()
    /tmp/pypetest/testfile_in
    /tmp/pypetest/testfile_out
    /tmp/pypetest/testfile_in
    /tmp/pypetest/testfile_out
    True
    >>> timeStampCompare(test.inputDataObjs, test.outputDataObjs, test.parameters)
    False
    >>> print test._getRunFlag()
    False
    >>> print test.a
    I am "a"
    >>> print test.b
    I am "b"
    >>> os.system( "touch %s" %  (fn(fin))  )
    0
    >>> # test PypeTask.finalize()
    >>> from controller import PypeWorkflow
    >>> wf = PypeWorkflow()
    >>> wf.addTask(test)
    >>> def finalize(self):
    ...     def f():
    ...         print "in finalize:", self._status
    ...     return f
    >>> test.finalize = finalize(test)  # For testing only. Please don't do this in your code. The PypeTask.finalized() is intended to be overriden by subclasses. 
    >>> wf.refreshTargets( objs = [fout] )
    True
    >>> #The following code show how to set up a task with a PypeThreadWorkflow that allows running multitple tasks in parallel. 
    >>> from pypeflow.controller import PypeThreadWorkflow
    >>> wf = PypeThreadWorkflow()
    >>> @PypeTask(outputDataObjs={"test_out":fout},
    ...           inputDataObjs={"test_in":fin},
    ...           TaskType=PypeThreadTaskBase,
    ...           parameters={"a":'I am "a"'}, **{"b":'I am "b"'})
    ... def test(self):
    ...     print test.test_in.localFileName
    ...     print test.test_out.localFileName
    ...     os.system( "touch %s" % fn(test.test_out) )
    ...     print self.test_in.localFileName
    ...     print self.test_out.localFileName
    >>> wf.addTask(test)
    >>> def finalize(self):
    ...     def f():
    ...         print "in finalize:", self._status
    ...     return f
    >>> test.finalize = finalize(test)  # For testing only. Please don't do this in your code. The PypeTask.finalized() is intended to be overided by subclasses. 
    >>> wf.refreshTargets( objs = [fout] ) #doctest: +SKIP
    """

    def f(taskFun):

        TaskType = kwargv.get("TaskType", PypeTaskBase)

        if "TaskType" in kwargv:
            del kwargv["TaskType"]

        kwargv["_taskFun"] = taskFun

        if kwargv.get("URL",None) == None:
            kwargv["URL"] = _auto_task_url(taskFun)
        try:
            kwargv["_codeMD5digest"] = hashlib.md5(inspect.getsource(taskFun)).hexdigest()
        except IOError: #python2.7 seems having problem to get source code from docstring, this is a work around to make docstring test working
            kwargv["_codeMD5digest"] = ""
        kwargv["_paramMD5digest"] = hashlib.md5(repr(kwargv)).hexdigest()

        newKwargv = copy.copy(kwargv)
        inputDataObjs = kwargv.get("inputDataObjs",{}) 
        inputDataObjs.update(kwargv.get("inputs", {}))
        outputDataObjs = kwargv.get("outputDataObjs",{}) 
        outputDataObjs.update(kwargv.get("outputs", {}))
        newInputs = {}
        for inputKey, inputDO in inputDataObjs.items():
            if isinstance(inputDO, PypeSplittableLocalFile):
                newInputs[inputKey] = inputDO._completeFile
            else:
                newInputs[inputKey] = inputDO

        newOutputs = {}
        for outputKey, outputDO in outputDataObjs.items():
            if isinstance(outputDO, PypeSplittableLocalFile):
                newOutputs[outputKey] = outputDO._completeFile
            else:
                newOutputs[outputKey] = outputDO

        newKwargv["inputDataObjs"] = newInputs
        newKwargv["outputDataObjs"] = newOutputs
        task = TaskType(*argv, **newKwargv)
        task.__doc__ = taskFun.__doc__
        return task

    return f

def PypeShellTask(*argv, **kwargv):

    """
    A function that converts a shell script into a PypeTaskBase object.

    >>> import os, time 
    >>> from pypeflow.data import PypeLocalFile, makePypeLocalFile, fn
    >>> from pypeflow.task import *
    >>> try:
    ...     os.makedirs("/tmp/pypetest")
    ...     _ = os.system("rm -f /tmp/pypetest/*")
    ... except Exception:
    ...     pass
    >>> time.sleep(.1)
    >>> fin = makePypeLocalFile("/tmp/pypetest/testfile_in", readOnly=False)
    >>> fout = makePypeLocalFile("/tmp/pypetest/testfile_out", readOnly=False)
    >>> f = open("/tmp/pypetest/shellTask.sh","w")
    >>> f.write( "touch %s" % (fn(fout)))
    >>> f.close()
    >>> shellTask = PypeShellTask(outputDataObjs={"test_out":fout},
    ...                           inputDataObjs={"test_in":fin},
    ...                           parameters={"a":'I am "a"'}, **{"b":'I am "b"'}) 
    >>> shellTask = shellTask("/tmp/pypetest/shellTask.sh")
    >>> type(shellTask) 
    <class 'pypeflow.task.PypeTaskBase'>
    >>> print fn(shellTask.test_in)
    /tmp/pypetest/testfile_in
    >>> os.system( "touch %s" %  fn(fin)  ) 
    0
    >>> timeStampCompare(shellTask.inputDataObjs, shellTask.outputDataObjs, shellTask.parameters)
    True
    >>> print shellTask._getRunFlag()
    True
    >>> shellTask() # run task
    True
    >>> timeStampCompare(shellTask.inputDataObjs, shellTask.outputDataObjs, shellTask.parameters)
    False
    >>> print shellTask._getRunFlag()
    False
    >>> shellTask()
    True
    """

    def f(scriptToRun):
        def taskFun(self):
            """make shell script using a template"""
            """run shell command"""
            shellCmd = "/bin/bash %s" % scriptToRun
            runShellCmd(shlex.split(shellCmd))

        kwargv["script"] = scriptToRun
        return PypeTask(*argv, **kwargv)(taskFun)

    return f


def PypeSGETask(*argv, **kwargv):

    """
    Similar to PypeShellTask, but the shell script job will be executed through SGE.
    """

    def f(scriptToRun):

        def taskFun():
            """make shell script using the template"""
            """run shell command"""
            shellCmd = "qsub -sync y -S /bin/bash %s" % scriptToRun
            runShellCmd(shlex.split(shellCmd))

        kwargv["script"] = scriptToRun

        return PypeTask(*argv, **kwargv)(taskFun)

    return f

def PypeDistributibleTask(*argv, **kwargv):

    """
    Similar to PypeShellTask and PypeSGETask, with an additional argument "distributed" to decide
    whether a job to be run through local shell or SGE.
    """

    distributed = kwargv.get("distributed", False)
    def f(scriptToRun):
        def taskFun(self):
            """make shell script using the template"""
            """run shell command"""
            if distributed is True:
                shellCmd = "qsub -sync y -S /bin/bash %s" % scriptToRun
            else:
                shellCmd = "/bin/bash %s" % scriptToRun

            runShellCmd(shlex.split(shellCmd))

        kwargv["script"] = scriptToRun
        return PypeTask(*argv, **kwargv)(taskFun) 

    return f


def PypeScatteredTasks(*argv, **kwargv):

    def f(taskFun):

        TaskType = kwargv.get("TaskType", PypeTaskBase)

        if "TaskType" in kwargv:
            del kwargv["TaskType"]

        kwargv["_taskFun"] = taskFun

        inputDataObjs = kwargv["inputDataObjs"]
        outputDataObjs = kwargv["outputDataObjs"]
        nChunk = None
        scatteredInput  = []

        if kwargv.get("URL", None) == None:
            kwargv["URL"] = "tasks://" + inspect.getfile(taskFun) + "/"+ taskFun.func_name

        tasks = PypeTaskCollection(kwargv["URL"])

        for inputKey, inputDO in inputDataObjs.items():
            if hasattr(inputDO, "nChunk"):
                if nChunk != None:
                    assert inputDO.nChunk == nChunk
                else:
                    nChunk = inputDO.nChunk
                    if inputDO.getScatterTask() != None:
                        tasks.addScatterGatherTask( inputDO.getScatterTask() )

                scatteredInput.append( inputKey )

        for outputKey, outputDO in outputDataObjs.items():
            if hasattr(outputDO, "nChunk"):
                if nChunk != None:
                    assert outputDO.nChunk == nChunk
                    if outputDO.getGatherTask() != None:
                        tasks.addScatterGatherTask( outputDO.getGatherTask() )
                else:
                    nChunk = outputDO.nChunk


        for i in range(nChunk):

            newKwargv = copy.copy(kwargv)

            subTaskInput = {}
            for inputKey, inputDO in inputDataObjs.items():
                if inputKey in scatteredInput:
                    subTaskInput[inputKey] = inputDO.getSplittedFiles()[i]
                else:
                    subTaskInput[inputKey] = inputDO

            subTaskOutput = {}
            for outputKey, outputDO in outputDataObjs.items():
                subTaskOutput[outputKey] = outputDO.getSplittedFiles()[i]

            newKwargv["inputDataObjs"] = subTaskInput
            newKwargv["outputDataObjs"] = subTaskOutput

            #newKwargv["URL"] = "task://" + inspect.getfile(taskFun) + "/"+ taskFun.func_name + "/%03d" % i
            newKwargv["URL"] = kwargv["URL"].replace("tasks","task") + "/%03d" % i

            try:
                newKwargv["_codeMD5digest"] = hashlib.md5(inspect.getsource(taskFun)).hexdigest()
            except IOError: 
                # python2.7 seems having problem to get source code from docstring, 
                # this is a work around to make docstring test working
                newKwargv["_codeMD5digest"] = ""

            newKwargv["_paramMD5digest"] = hashlib.md5(repr(kwargv)).hexdigest()
            newKwargv["chunk_id"] = i

            
            tasks.addTask( TaskType(*argv, **newKwargv) )
        return tasks
    return f

getPypeScatteredTasks = PypeScatteredTasks

def PypeFOFNMapTasks(*argv, **kwargv):
    """
    A special decorator that takes a FOFN (file of file names) as the main
    input and generate the tasks with the inputs are the files specified in
    the FOFN

    Example:

        def outTemplate(fn):
            return fn + ".out"

        def task(self, *argv, **kwargv):
            in_f = self.in_f
            out_f = self.out_f
            #do something with in_f, and write something to out_f

        tasks = PypeFOFNMapTasks(FOFNFileName = "./file.fofn", 
                outTemplateFunc = outTemplate, 
                TaskType = PypeThreadTaskBase,
                parameters = dict(nSlots = 8))( alignTask )
    """

    def f(taskFun):

        TaskType = kwargv.get("TaskType", PypeTaskBase)

        if "TaskType" in kwargv:
            del kwargv["TaskType"]

        kwargv["_taskFun"] = taskFun

        FOFNFileName = kwargv["FOFNFileName"]
        outTemplateFunc = kwargv["outTemplateFunc"]

        if kwargv.get("URL", None) == None:
            kwargv["URL"] = "tasks://" + inspect.getfile(taskFun) + "/"+ taskFun.func_name

        tasks = PypeTaskCollection(kwargv["URL"])

        with open(FOFNFileName,"r") as FOFN:

            newKwargv = copy.copy(kwargv)
            
            for fn in FOFN:

                fn = fn.strip()

                if len(fn) == 0:
                    continue

                newKwargv["inputDataObjs"] = {"in_f": makePypeLocalFile(fn) } 
                outfileName = outTemplateFunc(fn)
                newKwargv["outputDataObjs"] = {"out_f": makePypeLocalFile(outfileName) } 
                newKwargv["URL"] = kwargv["URL"].replace("tasks","task") + "/%s" % hashlib.md5(fn).hexdigest() 

                try:
                    newKwargv["_codeMD5digest"] = hashlib.md5(inspect.getsource(taskFun)).hexdigest()
                except IOError: 
                    # python2.7 seems having problem to get source code from docstring, 
                    # this is a work around to make docstring test working
                    newKwargv["_codeMD5digest"] = ""


                newKwargv["_paramMD5digest"] = hashlib.md5(repr(kwargv)).hexdigest()

                tasks.addTask( TaskType(*argv, **newKwargv) )

            allFOFNOutDataObjs = dict( [ ("FOFNout%03d" % t[0], t[1].in_f) for t in enumerate(tasks) ] )

            def pseudoScatterTask(*argv, **kwargv):
                pass

            newKwargv = dict( inputDataObjs = {"FOFNin": makePypeLocalFile(FOFNFileName)}, 
                              outputDataObjs = allFOFNOutDataObjs,
                              _taskFun = pseudoScatterTask,
                              _compareFunctions = [lambda inObjs, outObj, params: False], #this task is never meant to be run
                              URL = "task://pseudoScatterTask/%s" % FOFNFileName)

            tasks.addTask( TaskType(**newKwargv) )

        return tasks

    return f

getFOFNMapTasks = PypeFOFNMapTasks

def timeStampCompare( inputDataObjs, outputDataObjs, parameters) :

    """
    Given the inputDataObjs and the outputDataObjs, determine whether any
    object in the inputDataObjs is created or modified later than any object
    in outputDataObjects.
    """

    runFlag = False

    inputDataObjsTS = []
    for ft, f in inputDataObjs.iteritems():
        inputDataObjsTS.append((f.timeStamp, 'A', f))

    outputDataObjsTS = []
    for ft, f in outputDataObjs.iteritems():
        if not f.exists:
            logger.debug('output does not exist yet: %r'%f)
            runFlag = True
            break
        else:
            # 'A' < 'B', so outputs are 'later' if timestamps match.
            outputDataObjsTS.append((f.timeStamp, 'B', f))

    if not outputDataObjs:
        # 0 outputs => always run
        runFlag = True

    if not runFlag and inputDataObjs: # 0 inputs would imply that existence of outputs is enough.
        minOut = min(outputDataObjsTS)
        maxIn = max(inputDataObjsTS)
        if minOut < maxIn:
            logger.debug('timestamp of output < input: %r < %r'%(minOut, maxIn))
            runFlag = True

    return runFlag

if __name__ == "__main__":
    import doctest
    doctest.testmod()
