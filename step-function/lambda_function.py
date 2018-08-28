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

The sendResponse function originated from this blog post:

https://pprakash.me/tech/2015/12/20/sending-response-back-to-cfn-custom-resource-from-python-lambda-function/

"""

import os, boto3, sys, traceback, json
from botocore.vendored import requests
bucketName = 'vmware-cloud-on-aws-autodeploy'
sourceFile = 'VMware-Cloud-on-AWS-AutoDeploy_deployment-package.zip'
targetFile = '/tmp/awsvmc/deployment-package.zip'
targetDir = '/tmp/awsvmc'
client = boto3.client('stepfunctions')

try:
    os.stat(targetDir)
except:
    print("mkdir {}.".format(targetDir))
    os.mkdir(targetDir)

if not os.path.isfile(targetFile):
  print("download {}.".format(targetFile))
  s3 = boto3.resource('s3')
  s3.Bucket(bucketName).download_file(sourceFile, targetFile)

if not os.path.isfile(targetDir + '/awsvmc.py'):
  print("extract {}.".format(targetFile))
  import zipfile
  with zipfile.ZipFile(targetFile,'r') as zip_ref:
    zip_ref.extractall(targetDir)

import sys
sys.path.append(targetDir)
os.chdir(targetDir)

print("import awsvmc")
import awsvmc
v = None
o = None
orgId = None

def lambda_handler(event, context):
    responseStatus = 'SUCCESS'
    responseData = {}
    print(event)
    
    if event['RequestType'] == 'Delete':
        print("Delete Stack request received")
        sendResponse(event, context, responseStatus, responseData)
        
    elif event['RequestType'] in ['Create','Update']:
        print("Create/Update Stack request received")
        # call step function
        stepFunctionEvent = {
            "RequestType": "StepFunction",
            "WorkshopConfig": {
                "OrgId":             event['ResourceProperties']['OrgId'],
                "Provider":          event['ResourceProperties']['Provider'],
                "SsoDomain":         event['ResourceProperties']['SsoDomain'],
                "SddcName":          event['ResourceProperties']['SddcName'],
                "NumHosts":          event['ResourceProperties']['NumHosts'],
                "DeploymentType":    event['ResourceProperties']['DeploymentType'],
                "Region":            event['ResourceProperties']['Region'],
                "VpcCidr":           event['ResourceProperties']['VpcCidr'],
                "Datastore":         event['ResourceProperties']['Datastore'],
                "ContentLibraryName":event['ResourceProperties']['ContentLibraryName'],
                "ContentLibraryURL": event['ResourceProperties']['ContentLibraryURL'],
				"sslThumbprint":     event['ResourceProperties']['sslThumbprint'],
                "DnsConfig":         [ event['ResourceProperties']['DnsConfig'] ]
            },
            "Organizations": {
                event['ResourceProperties']['OrgId']: {
                  "RefreshToken":  event['ResourceProperties']['RefreshToken'],
                  "LinkedSubnets":  [ event['ResourceProperties']['LinkedSubnets'] ],
                  "SddcPods": {
                    event['ResourceProperties']['SddcName']: { "VxlanSubnet": event['ResourceProperties']['VxlanSubnet'],  
                    "ManagementCidr": event['ResourceProperties']['ManagementCidr']  }
                  }
                }
            },
            "step": {
                "currentStep":  "createSddc",
                "sleepSeconds": 5,
                "origEvent": event,
                "origContext": { "log_stream_name": context.log_stream_name }
            }
        }
        print(json.dumps(stepFunctionEvent))
        response = client.start_execution(
            stateMachineArn='arn:aws:states:us-west-2:000000000000:stateMachine:VMware-Cloud-on-AWS-AutoDeploy',
            name=event['RequestId'],
            input=json.dumps(stepFunctionEvent)
        )
        print(response)
        
    else:
        print("Step-Function step:",event['step']['currentStep'])
        global orgId, v, o
        if orgId is None:
            orgId = event['WorkshopConfig']['OrgId']
        if v is None:
            print("using refreshToken to instantiate VMC object")
            v = awsvmc.VMC(event['Organizations'][orgId]['RefreshToken'])
        if o is None:
            print("using OrgID to instantiate ORG object")
            o = awsvmc.ORG(v,orgId,True)
            o.config = awsvmc.dict2class(event) 
        
        sddcName = event['WorkshopConfig']['SddcName']
        nextStep =  event['step']['currentStep']
        sleepSeconds = event['step']['sleepSeconds']

        ####### createSddc
        if event['step']['currentStep'] == 'createSddc':
            print("create SDDC {}".format(sddcName))

            sleepSeconds = 300
            o.refreshSddcs()
            event['step']['createSddcTaskID'] = o.createSddc(sddcName)
            if event['step']['createSddcTaskID'] is not None:
                sleepSeconds = o.remainingSecondsTask(event['step']['createSddcTaskID'],6600)

            nextStep = 'checkSddc'

        ####### checkSddc
        elif event['step']['currentStep'] == 'checkSddc':
            print("check status of SDDC {}".format(sddcName))

            sleepSeconds = 10
            try:
                if 'createSddcTaskID' in event['step'] and event['step']['createSddcTaskID'] is not None:
                    print("check status of taskID {}".format(event['step']['createSddcTaskID']))
                    sleepSeconds = o.remainingSecondsTask(event['step']['createSddcTaskID'],270)
            except:
                print("Exception in user code:")
                print("-"*60)
                traceback.print_exc(file=sys.stdout)
                print("-"*60)
                pass

            try:
                o.refreshSddcs()
                event['step']['sddcState'] = o.getSddc(sddcName).sddc.sddc_state
                if event['step']['sddcState'] == 'READY':
                    nextStep = 'configureFirewall'
                    sleepSeconds = 1
            except:
                print("Exception in user code:")
                print("-"*60)
                traceback.print_exc(file=sys.stdout)
                print("-"*60)
                pass

        ####### configureFirewall
        elif event['step']['currentStep'] == 'configureFirewall':
            print("configure firewall rules for SDDC {}".format(sddcName))
            # cleanup for idempotence
            mgwRuleList = [
                'Allow Any to vCenter:443',
                'Allow Mgmt to VPC',
                'Allow VPC to Mgmt'
                ]
            cgwRuleList = [
                'Allow SDDC to Any',
                'Allow VPC to SDDC'
                ]
            try:
                for ruleName in mgwRuleList:
                    if o.getSddc(sddcName).getFwRule('sddc-mgw',ruleName):
                        o.getSddc(sddcName).deleteFwRule('sddc-mgw',ruleName)
                for ruleName in cgwRuleList:
                    if o.getSddc(sddcName).getFwRule('SDDC-CGW-1-esg',ruleName):
                        o.getSddc(sddcName).deleteFwRule('SDDC-CGW-1-esg',ruleName)
                
                vCenterIPList = [
                    o.getSddc(sddcName).sddc.resource_config.vc_public_ip,
                    o.getSddc(sddcName).sddc.resource_config.vc_management_ip,
                ]
                vpcCidr = event['WorkshopConfig']['VpcCidr']
                managementCidr = event['Organizations'][orgId]['SddcPods'][sddcName]['ManagementCidr']
        
                o.getSddc(sddcName).createFwRule('sddc-mgw','Allow Any to vCenter:443','any','any',vCenterIPList,'443')
                o.getSddc(sddcName).createFwRule('sddc-mgw','Allow VPC to Mgmt',vpcCidr,'any',managementCidr,'any')
                o.getSddc(sddcName).createFwRule('sddc-mgw','Allow Mgmt to VPC',managementCidr,'any',vpcCidr,'any')
            
                o.getSddc(sddcName).createFwRule('SDDC-CGW-1-esg','Allow SDDC to Any','192.168.2.0/24','any','any','any')
                o.getSddc(sddcName).createFwRule('SDDC-CGW-1-esg','Allow VPC to SDDC',vpcCidr,'any','192.168.2.0/24','any')

            except Exception as ex:
                print("Exception in user code:")
                print("-"*60)
                traceback.print_exc(file=sys.stdout)
                print("-"*60)
                pass
        
            nextStep = 'checkFirewall'
            sleepSeconds = 10
        
        ####### checkFirewall
        elif event['step']['currentStep'] == 'checkFirewall':
            print("check Firewall Rules for SDDC {}".format(sddcName))
            ruleCount = 0
            ruleList = [
                'Allow Any to vCenter:443',
                'Allow Mgmt to VPC',
                'Allow VPC to Mgmt'
                ]
        
            try:
                for ruleName in ruleList:
                    if o.getSddc(sddcName).getFwRule('sddc-mgw',ruleName):
                        ruleCount += 1
            except:
                print("Exception in user code:")
                print("-"*60)
                traceback.print_exc(file=sys.stdout)
                print("-"*60)
                pass
        
            print("{} of {} rules found in SDDC {}.".format(ruleCount,len(ruleList),sddcName))
            if ruleCount >= len(ruleList):
                nextStep = 'connectContentLibrary'
                sleepSeconds = 1
            else:
                sleepSeconds = 10
            
        ####### connectContentLibrary
        elif event['step']['currentStep'] == 'connectContentLibrary':
            print("connect an existing Subscribed Content library to SDDC {}".format(sddcName))
            # cleanup for idempotence
            try:
                o.getSddc(sddcName).getVC().dismountContentLibrary()
            except:
                print("Exception in user code:")
                print("-"*60)
                traceback.print_exc(file=sys.stdout)
                print("-"*60)
                pass
            try:
                o.getSddc(sddcName).getVC().mountContentLibrary()
            except:
                print("Exception in user code:")
                print("-"*60)
                traceback.print_exc(file=sys.stdout)
                print("-"*60)
                pass
            nextStep = 'checkContentLibrary'
            sleepSeconds = 10

        ####### checkContentLibrary
        elif event['step']['currentStep'] == 'checkContentLibrary':
            print("check Subscribed Content Library exists for SDDC {}".format(sddcName))
            try:
                o.refreshSddcs()
                libraries = o.getSddc(sddcName).getVC().getContentLibraries('CL')
            except:
                print("Exception in user code:")
                print("-"*60)
                traceback.print_exc(file=sys.stdout)
                print("-"*60)
                pass
            print("{} Subscribed Content Library found in SDDC {}.".format(len(libraries),sddcName))
            if len(libraries) >= 1:
                nextStep = 'deployVM'
                sleepSeconds = 10
            else:
                sleepSeconds = 10

        ####### deployVM
        elif event['step']['currentStep'] == 'deployVM':
            print("deploy VM within SDDC {}".format(sddcName))
            try:
                o.getSddc(sddcName).getVC().deployVM(sddcName)
            except:
                print("Exception in user code:")
                print("-"*60)
                traceback.print_exc(file=sys.stdout)
                print("-"*60)
                pass
            nextStep = 'checkVM'
            sleepSeconds = 10
        
        ####### checkVM
        elif event['step']['currentStep'] == 'checkVM':
            print("check VM exists within SDDC {}".format(sddcName))
            try:
                if o.getSddc(sddcName).getVC().getVM('centos'):
                    nextStep = 'notify'
                    sleepSeconds = 1
                else:
                    sleepSeconds = 10
            except:
                print("Exception in user code:")
                print("-"*60)
                traceback.print_exc(file=sys.stdout)
                print("-"*60)
                pass
        
        ####### notify
        elif event['step']['currentStep'] == 'notify':
            print("Send notification of completion for SDDC {}".format(sddcName))
            print("Signal to CFn we have completed all steps")
            try:
                sendResponse(event['step']['origEvent'], event['step']['origContext'], responseStatus, responseData)
            except:
                print("Exception in user code:")
                print("-"*60)
                traceback.print_exc(file=sys.stdout)
                print("-"*60)
                pass
            nextStep = 'done'
    
        if nextStep != event['step']['currentStep']:
            event['step']['previousStep'] = event['step']['currentStep']    
            event['step']['currentStep'] = nextStep
        
        event['step']['sleepSeconds'] = sleepSeconds
        

        
    return event
    
def sendResponse(event, context, responseStatus, responseData):
    responseBody = {
        'Status': responseStatus,
        'Reason': 'See the details in CloudWatch Log Stream: ' + context['log_stream_name'],
        'PhysicalResourceId': context['log_stream_name'],
        'StackId': event['StackId'],
        'RequestId': event['RequestId'],
        'LogicalResourceId': event['LogicalResourceId'],
        'Data': responseData}
    print('RESPONSE BODY:n' + json.dumps(responseBody))
    try:
        req = requests.put(event['ResponseURL'], data=json.dumps(responseBody))
        if req.status_code != 200:
            print(req.text)
            raise Exception('Recieved non 200 response while sending response to CFn.')
        return
    except requests.exceptions.RequestException as e:
        print(e)
        raise
