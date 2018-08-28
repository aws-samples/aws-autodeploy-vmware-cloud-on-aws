#!/usr/bin/env python
"""

Copyright 2018 Amazon.com, Inc. or its affiliates. All Rights Reserved.

Permission is hereby granted, free of charge, to any person obtaining a copy of this
software and associated documentation files (the "Software"), to deal in the Software
without restriction, including without limitation the rights to use, copy, modify,
merge, publish, distribute, sublicense, and/or sell copies of the Software, and to
permit persons to whom the Software is furnished to do so.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR IMPLIED,
INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY, FITNESS FOR A
PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT
HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION
OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION WITH THE
SOFTWARE OR THE USE OR OTHER DEALINGS IN THE SOFTWARE.

Bryan Wood
Partner Solutions Architect
Amazon Web Services

"""

from io import StringIO
import code, re, sys, contextlib, json
import awsvmc

class dict2class(dict):
    def __init__(self, dic):
        for key,val in dic.items():
            self.__dict__[key]=self[key]=dict2class(val) if isinstance(val,dict) else val

config = None
with open('config.json') as jsonData:
    config = dict2class(json.load(jsonData))

OrgId = config['WorkshopConfig']['OrgId']
RefreshToken = config['Organizations'][OrgId]['RefreshToken']
print(OrgId,RefreshToken)

@contextlib.contextmanager
def stdoutIO(stdout=None):
    old = sys.stdout
    if stdout is None:
        stdout = StringIO()
    sys.stdout = stdout
    yield stdout
    sys.stdout = old

def subPad(orig,repl,string,pad=False):
    if pad:
      formatString = "{:<"+str(len(orig))+"}"
      return re.sub(orig,formatString.format(repl),string)
    else:
      return re.sub(orig,repl,string)

def expunge(string,pad=False,highlight=False):
    result = string
    result = subPad(RefreshToken,'your_OAuth_refresh_token',result,pad)
    result = subPad(OrgId,'00000000-0000-0000-0000-000000000000',result,pad)

    if highlight:
      result = subPad(highlight,'\x1b[0;30;47m'+highlight+'\x1b[0m',result,pad)

    return result

print('''Python 3.6.5 (default, May  5 2018, 03:09:35)
[GCC 4.9.2] on linux
Type "help", "copyright", "credits" or "license" for more information.''')


########################## SCRIPT ##########################

script = """
v = awsvmc.VMC(RefreshToken)
o = awsvmc.ORG(v,OrgId)
o.listSddcs()

s = awsvmc.SDDC(o,'your_sddc_id')
vc = awsvmc.VC(s)
vc.listContentLibraries()

"""

for line in script.splitlines():
    print('>>> ' + expunge(line))
    with stdoutIO() as stdOut:
        try:
            exec(line)
        except:
            print("Something wrong with the code")
    print(
        expunge(
            stdOut.getvalue(),
            pad=True,
            highlight='your_sddc_id'),
        end='',
        flush=True)

code.interact(banner='',exitmsg='',local=locals())

