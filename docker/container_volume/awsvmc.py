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


SDDC Workshop configuration create/destroy automation

Acknowledgement:

This code was largely based on examples from the VMware VMC SDK for Python:
    https://github.com/vmware/vsphere-automation-sdk-python/tree/master/samples


Bryan Wood
Partner Solutions Architect
Amazon Web Services
"""

import argparse, atexit, json, operator
import requests, re, ssl, uuid
from time import sleep
from retry import retry
from retry.api import retry_call
from tabulate import tabulate

from com.vmware import content_client
from com.vmware.cis_client import Session
from com.vmware.content import library_client
from com.vmware.content_client import LibraryModel
from com.vmware.content.library_client import StorageBacking, SubscriptionInfo, Item
from com.vmware.vapi.std.errors_client import InvalidRequest, InternalServerError
from com.vmware.vcenter.ovf_client import LibraryItem
from com.vmware.vmc.model_client import AwsSddcConfig, ErrorResponse, AccountLinkSddcConfig
from com.vmware.vmc.model_client import Nsxfirewallrule, AddressFWSourceDestination, Task
from com.vmware.vmc.model_client import Application, Nsxfirewallservice, FirewallRules
from com.vmware.vmc.model_client import DnsForwarders, Nsxnatrule, SddcAllocatePublicIpSpec
from com.vmware.vmc.model_client import NatRules, SddcNetworkDhcpConfig, SddcNetworkDhcpIpPool
from com.vmware.vmc.model_client import SddcNetwork, SddcNetworkAddressGroups, SddcNetworkAddressGroup
from com.vmware.vcenter_client import Datastore, Datacenter
from pyVim.connect import SmartConnect, Disconnect, vim, vmodl
from vmware.vapi.lib.connect import get_requests_connector
from vmware.vapi.vmc.client import create_vmc_client
from vmware.vapi.security.user_password import create_user_password_security_context
from vmware.vapi.security.session import create_session_security_context
from vmware.vapi.stdlib.client.factories import StubConfigurationFactory
from vmware.vapi.vsphere.client import create_vsphere_client

from requests.packages.urllib3.exceptions import InsecureRequestWarning
requests.packages.urllib3.disable_warnings(InsecureRequestWarning)

class VMC(object):
    """
    Instantiating an object of this class establishes a connection to 
    https://vmc.vmware.com using a predefined OAuth Refresh Token.
    """

    def __init__(self, refreshToken=None, verbose=False):

        self.refreshToken = refreshToken

        if not self.refreshToken:
            raise ValueError('You must supply your OAuth Refresh Token')

        session = requests.Session()
        self.vmc_client = create_vmc_client(self.refreshToken, session)

        atexit.register(session.close)

        self.orgs = []
        self.refreshOrgs()

        if not self.orgs:
            raise ValueError('You must have at least one Organization'
                             ' associated with the calling user')

        if verbose:
            self.listOrgs()

        self.config = None
        self.pod_filter = None
        self.create_pods = None
        self.delete_existing = None
        self.firewall_rules = None
        self.answer_yes = None
        self.pod_tasks = None
        self.connected_account = None

    def refreshOrgs(self):

        self.orgs = retry_call(self.vmc_client.Orgs.list,tries=5)

        return self.orgs

    def listOrgs(self,orgId=None):

        table = []
        for org in sorted(self.orgs, 
                          key=operator.attrgetter('display_name')):
            table.append([ org.id, 
                           org.display_name, 
                           org.name,
                           org.created.strftime('%m/%d/%Y'),
                           org.updated.strftime('%m/%d/%Y'),
                           org.project_state, org.sla ])
            if orgId is not None and org.id == orgId:
                table[-1][1]+=" **"

        headers = ['OrgId', 'Display Name', 'Name', 'Created',
                   'Updated', 'State', 'SLA']

        print('\n'+tabulate(table, headers))


class ORG(object):
    """
    Organization class
    """

    def __init__(self, vmc=None, orgId=None, jsonConfig=None, verbose=False):

        self.vmc = vmc

        if not self.vmc:
            raise ValueError('You must supply a valid VMC() object')

        self.org = None
        for org in self.vmc.orgs:
            if org.id == orgId:
                self.org = org
                break

        if not self.org:
            raise ValueError('You must supply a valid Organization ID')

        if not jsonConfig:
            with open('config.json') as jsonData:
                self.config = dict2class(json.load(jsonData))

        self.sddcs = []
        self.sddcName = {}
        self.refreshSddcs()

        self.connectedAccounts = []
        self.refreshConnectedAccounts()

        if verbose:
            self.vmc.listOrgs(self.org.id)
            self.listSddcs()
            self.listSddcVcURLs()

    def refreshSddcs(self):

        self.sddcs = sorted(self.vmc.vmc_client.orgs.Sddcs.list(self.org.id),
                            key=operator.attrgetter('name'))

        for sddc in self.sddcs:
            if sddc.name in self.sddcName:
                self.sddcName[sddc.name].sddc = sddc

        return self.sddcs

    def listSddcs(self,sddcIds=[],sddcNames=[]):

        table = []
        for sddc in self.sddcs:
            table.append([ self.org.id, 
                           sddc.id, 
                           sddc.name,
                           sddc.created.strftime('%m/%d/%Y'),
                           sddc.updated.strftime('%m/%d/%Y'),
                           sddc.sddc_state ])

            if sddc.id in sddcIds or sddc.name in sddcNames:
                 table[-1][2]+=" **"

        headers = ["OrgId", "SddcId", "Name", "Created", "Updated", "State"]
        print('\n'+tabulate(table, headers))

    def listSddcVcURLs(self,sddcIds=[],sddcNames=[]):

        table = []
        for sddc in self.sddcs:
            if sddc.resource_config is not None \
                and sddc.resource_config.vc_url is not None:
                table.append([ sddc.name,
                               sddc.resource_config.vc_url + "ui",
                               sddc.resource_config.cloud_password ])

                if sddc.id in sddcIds or sddc.name in sddcNames:
                    table[-1][0]+=" **"

        headers = ["Name", "VcURL", "password"]
        print('\n'+tabulate(table, headers))

    def isSddcReady(self, sddcName=None):

        sddc_state = None
        try:
            self.refreshSddcs()
            sddc_state = self.getSddc(sddcName).sddc.sddc_state
            if sddc_state == 'READY':
                return True
        except:
            pass

        return False

    def getSddc(self, sddcName=None):

        if sddcName in self.sddcName:
            return self.sddcName[sddcName]

        else:
            for sddc in self.sddcs:
                if sddc.name == sddcName:
                    self.sddcName[sddcName] = SDDC(self,sddcName=sddcName)
                    return self.sddcName[sddcName]
            
        raise ValueError('You must supply a valid SDDC Name')

    def refreshConnectedAccounts(self):

        self.connectedAccounts = self.vmc.vmc_client.orgs.account_link.ConnectedAccounts.get(
            org=self.org.id)

        return self.connectedAccounts

    def listConnectedAccounts(self):

        for account in self.connectedAccounts:
            print(account)

    def getConnectedAccountID(self,customerAccountId=None):

        if not customerAccountId:
            raise ValueError('You must supply a Customer AWS Account ID')

        accountId = None
        for account in self.connectedAccounts:
            if account.account_number == customerAccountId:
                accountId = account.id
                break

        return accountId

    def createSddc(self, sddcName=None, config=None, verbose=False):

        if not sddcName:
            raise ValueError('You must supply an SDDC name')

        for sddc in self.sddcs:
            if sddc.name == sddcName:
                print("SDDC {} already exists.".format(sddcName))
                return

        orgConfig = self.config.Organizations[self.org.id]
        podConfig = orgConfig.SddcPods[sddcName]

        sddcConfig = AwsSddcConfig(
                account_link_sddc_config=[
                    AccountLinkSddcConfig(
                        customer_subnet_ids=[orgConfig.LinkedSubnets[0] ],
                        connected_account_id=self.getConnectedAccountID(
                            orgConfig.LinkedAccount))
                ],
                name=sddcName,
                vxlan_subnet=podConfig.VxlanSubnet,
                vpc_cidr=podConfig.ManagementCidr,
                provider=self.config.WorkshopConfig.Provider,
                sso_domain=self.config.WorkshopConfig.SsoDomain,
                num_hosts=self.config.WorkshopConfig.NumHosts,
                deployment_type=self.config.WorkshopConfig.DeploymentType,
                region=self.config.WorkshopConfig.Region)

        # For single node cluster, an extra flag must be set
        if sddcConfig.num_hosts == 1:
            sddcConfig.sddc_type = "1NODE"

        # For legacy API/SDK combinations, there was a setting for vpc_name
        # that was later removed from the prototype.  The following check
        # ensures backwards compatability
        try:
            getattr(sddcConfig, 'vpc_name')
            sddcConfig.vpc_name = None
        except AttributeError:
            pass

        if verbose:
            print(sddcConfig)

        try:
            task = self.vmc.vmc_client.orgs.Sddcs.create(
                 org=self.org.id,
                 sddc_config=sddcConfig)

            if verbose:
                print(task.id)

            return task.id

        except InvalidRequest as e:
            # Convert InvalidRequest to ErrorResponse to get error message
            error_response = e.data.convert_to(ErrorResponse)
            raise Exception(error_response.error_messages)

    def deleteSddc(self, sddcName=None, confirm=True, verbose=False):

        if not sddcName:
            raise ValueError('You must supply an SDDC name')

        for sddc in self.sddcs:
            if sddc.name == sddcName:
                print("DELETE {} {} ".format(sddcName,sddc.id))

                if confirm:
                    response = input("\nDo you wish to proceed (Y/[N])? ")
                    if response != "Y":
                        print("\ndeleteSddc(): please answer \"Y\" when ready to proceed.")
                        return

                try:
                    task = self.vmc.vmc_client.orgs.Sddcs.delete(
                         org=self.org.id,
                         sddc=sddc.id)

                    if verbose:
                        print(task.id)

                    return task.id

                except InvalidRequest as e:
                    # Convert InvalidRequest to ErrorResponse to get error message
                    error_response = e.data.convert_to(ErrorResponse)
                    raise Exception(error_response.error_messages)

        print("Could not find an SDDC named {}.".format(sddcName))
        return

    def remainingSecondsTask(self, taskID, default=1):

        try:
            return self.vmc.vmc_client.orgs.Tasks.get(
                self.org.id,taskID).estimated_remaining_minutes * 60
        except:
            pass

        return default

    def listTask(self, filter=None):
        """
        List all tasks in a given org
        """
        headers = ['ID', 'Status', '%', 'RemainingMin', 'Type', 'Detail', 'Started', 'User']
        tasks = self.vmc.vmc_client.orgs.Tasks.list(self.org.id, filter)
        table = []
        for task in tasks:
            if task.status not in ['FAILED','FINISHED']:
                #print(dir(task.params.get_struct_value().get_field('sddcConfig').get_field('name')))
                #print(task.params.get_struct_value().get_field('sddcConfig').get_field('name').value)
                detail = ""
                if task.task_type == 'SDDC-DELETE':
                    detail = task.resource_id
                elif task.task_type == 'SDDC-PROVISION':
                    detail = task.params.get_struct_value().get_field('sddcConfig').get_field('name').value
    
                table.append([task.id, task.status, task.progress_percent,
                              task.estimated_remaining_minutes,task.task_type,
                              detail,task.start_time,task.user_name])
        print(tabulate(table, headers))

    def cancelTask(self, taskID, interval_sec=60):
        while True:
            try:
                task = self.vmc.vmc_client.orgs.Tasks.update(self.org.id, taskID, 'cancel')
            except:
                pass

    def waitTask(self, taskID, intervalSec=60):
        """
        Helper method to wait for a task to finish
        """
        print('Wait for task {} to finish'.format(taskID))
        print('Checking task status every {} seconds'.format(intervalSec))
    
        while True:
            try:
                task = self.vmc.vmc_client.orgs.Tasks.get(self.org.id, taskID)
    
                if task.status == Task.STATUS_FINISHED:
                    print('\nTask {} finished successfully'.format(taskID))
                    return True
                elif task.status == Task.STATUS_FAILED:
                    print('\nTask {} failed'.format(taskID))
                    return False
                elif task.status == Task.STATUS_CANCELED:
                    print('\nTask {} cancelled'.format(taskID))
                    return False
                else:
                    print("Estimated time remaining: {} minutes".
                          format(task.estimated_remaining_minutes))
            except:
                pass

            sleep(intervalSec)


class SDDC(object):
    """
    Software Defined Data Center class
    """

    def __init__(self, org=None, sddcId=None, sddcName=None, verbose=False):

        self.org = org

        if not self.org:
            raise ValueError('You must supply a valid ORG() object')

        self.vmc = org.vmc


        self.sddc = None
        for sddc in self.org.sddcs:
            if sddc.id == sddcId or sddc.name == sddcName:
                self.sddc = sddc
                break

        if not self.sddc:
            raise ValueError('You must supply a valid SDDC ID')

        self.edges = None
        self.vc = None

        self.refreshEdges()

        if verbose:
           self.listConfig()

    def getVC(self):

        if self.vc is None:
            self.vc = VC(self)

        return self.vc

    def refreshSddc(self):

        sddcId = self.sddc.id
        self.sddc = self.vmc.vmc_client.orgs.Sddcs.get(self.org.org.id, sddcId)

        return self.sddc

    #def updateNetwork(self):
    #    # TODO

    def refreshEdges(self):

        self.edges = self.vmc.vmc_client.orgs.sddcs.networks.Edges.get(
            org=self.org.org.id,
            sddc=self.sddc.id,
            edge_type='gatewayServices').edge_page.data

        return self.edges

    def listEdges(self):

        for edge in self.edges:
            print(edge.id,edge.name,edge.tenant_id)

    def getEdge(self, edgeName=None):

        if not edgeName:
            raise ValueError('You must supply a valid Edge Gateway name')

        for edge in self.edges:
            if re.search(edgeName,edge.name,re.IGNORECASE):
                return edge

        return None

    def getFwRules(self, edgeName=None):

        fw_config = self.vmc.vmc_client.orgs.sddcs.networks.edges.firewall.Config.get(
            org=self.org.org.id,
            sddc=self.sddc.id,
            edge_id=self.getEdge(edgeName).id)
        fw_rules = fw_config.firewall_rules.firewall_rules

        return fw_rules

    def getFwRule(self, edgeName=None, ruleName=None):

        if not ruleName:
            raise ValueError('You must supply a valid Firewall Rule Name')

        for rule in self.getFwRules(edgeName):
            if rule.name == ruleName:
                return rule

        return None

    def deleteFwRule(self, edgeName=None, ruleName=None):

        rule = self.getFwRule(edgeName,ruleName)

        if not rule:
            raise ValueError('You must supply a valid Firewall Rule Name')

        self.vmc.vmc_client.orgs.sddcs.networks.edges.firewall.config.Rules.delete(
            org=self.org.org.id,
            sddc=self.sddc.id,
            edge_id=self.getEdge(edgeName).id,
            rule_id=rule.rule_id)

        print('  {} {}     "{}" Firewall Rule deleted'.format(self.sddc.id,self.sddc.name,ruleName))

    def createFwRule(self, edgeName, ruleName, sourceIP,
        sourcePort, destinationIP, destinationPort, protocol='TCP'):

        if not ruleName:
            raise ValueError('You must supply a valid Firewall Rule Name')

        sourceIPs = sourceIP if isinstance(sourceIP,(list,)) else [sourceIP]
        sourcePorts = sourcePort if isinstance(sourcePort,(list,)) else [sourcePort]
        destinationIPs = destinationIP if isinstance(destinationIP,(list,)) else [destinationIP]
        destinationPorts = destinationPort if isinstance(destinationPort,(list,)) else [destinationPort]

        source = AddressFWSourceDestination(
            exclude=False,
            ip_address=sourceIPs,
            grouping_object_id=[],
            vnic_group_id=[])
        destination = AddressFWSourceDestination(
            exclude=False,
            ip_address=destinationIPs,
            grouping_object_id=[],
            vnic_group_id=[])
        service = Nsxfirewallservice(
            source_port=sourcePorts,
            protocol=protocol,
            port=destinationPorts,
            icmp_type=None)
        application = Application(
            application_id=[],
            service=[service])

        rule = Nsxfirewallrule(
            rule_type='user',
            name=ruleName,
            enabled=True,
            action='accept',
            source=source,
            destination=destination,
            logging_enabled=False,
            application=application)

        print(self.org.org.id,self.sddc.id,self.getEdge(edgeName).id,rule)
        self.vmc.vmc_client.orgs.sddcs.networks.edges.firewall.config.Rules.add(
            org=self.org.org.id,
            sddc=self.sddc.id,
            edge_id=self.getEdge(edgeName).id,
            firewall_rules=FirewallRules([rule]))

        print('  {} {}     "{}" Firewall Rule created'.format(self.sddc.id,self.sddc.name,ruleName))

    def listConfig(self):

        print(self.sddc)


class VC(object):
    """
    vCenter class
    """

    def __init__(self, sddc=None, verbose=False):

        self.sddc = sddc

        if not self.sddc:
            raise ValueError('You must supply a valid SDDC() object')

        self.org = sddc.org
        self.vmc = self.org.vmc

        self.vc_url = self.sddc.sddc.resource_config.vc_url
        self.vc_host = re.sub(r'https://(.*)/',r'\1',self.vc_url)
        self.vc_username = self.sddc.sddc.resource_config.cloud_username
        self.vc_password = self.sddc.sddc.resource_config.cloud_password

        session = requests.Session()
        connector = get_requests_connector(
            session=session,
            url='https://'+self.vc_host+'/api')
        user_password_security_context = create_user_password_security_context(
            self.vc_username,
            self.vc_password)
        context = ssl._create_unverified_context()

        self.stub_config = StubConfigurationFactory.new_std_configuration(connector)
        self.stub_config.connector.set_security_context(user_password_security_context)

        session_svc = Session(self.stub_config)
        session_id = session_svc.create()
        session_security_context = create_session_security_context(session_id)

        self.stub_config.connector.set_security_context(session_security_context)
        self.library_stub = content_client.Library(self.stub_config)
        self.subscribed_library_stub = content_client.SubscribedLibrary(self.stub_config)
        self.si = SmartConnect(host=self.vc_host,
            user=self.vc_username,
            pwd=self.vc_password,
            sslContext=context)
        self.content = self.si.RetrieveContent()

        self.references = {}

        self.referenceTypes = {
            'datastores':    [vim.Datastore],
            'resourcePools': [vim.ClusterComputeResource],
            'folders':       [vim.Folder],
            'VMs':           [vim.VirtualMachine]
        }

        for referenceName in self.referenceTypes:
            self.refreshReference(referenceName)

    def refreshReference(self, referenceName=None):

        if not referenceName or referenceName not in self.referenceTypes:
            raise ValueError('You must supply a valid Reference name')

        self.references[referenceName] = self.content.viewManager.CreateContainerView(
            self.content.rootFolder,
            self.referenceTypes[referenceName],
            True)

        return self.references[referenceName]

    def listDatastores(self):

        for c in self.references['datastores'].view:
            print(c._moId,c.name)

    def getDatastore(self, datastoreName=None):

        if not datastoreName:
            raise ValueError('You must supply a Datastore name')

        for c in self.references['datastores'].view:
            if c.name == datastoreName:
                return c

        raise Exception('  cannot find "{}"'.format(
            datastoreName))

    def listResourcePools(self):

        for c in self.references['resourcePools'].view:
            for p in c.resourcePool.resourcePool:
                print(p._moId,p.name)

    def getResourcePool(self, resourcePoolName=None):

        if not resourcePoolName:
            raise ValueError('You must supply a ResourcePool name')

        for c in self.references['resourcePools'].view:
            for p in c.resourcePool.resourcePool:
                if p.name == resourcePoolName:
                    return p

        raise Exception('  cannot find "{}"'.format(
            resourcePoolName))

    def listFolders(self):

        for c in self.references['folders'].view:
            print(c._moId,c.name)


    def getFolder(self, folderName=None):

        if not folderName:
            raise ValueError('You must supply a Folder name')

        for c in self.references['folders'].view:
            if c.name == folderName:
                return c

        raise Exception('  cannot find "{}"'.format(
            folderName))

    def listVMs(self):
        for c in self.references['VMs'].view:
            print(c._moId,c.name)

    def getVM(self, vmName=None):

        if not vmName:
            raise ValueError('You must supply a VM name')

        for c in self.references['VMs'].view:
            if c.name == vmName:
                return c

        raise Exception('  cannot find "{}"'.format(
            vmName))

    def destroyVM(self, vmName=None):

        self.wait_for_tasks(self.content, [self.getVM(vmName).PowerOff()])
        self.wait_for_tasks(self.content, [self.getVM(vmName).Destroy()])

    def listContentLibraries(self, contentLibraryName=None):

        table = []
        for library in sorted(self.getContentLibraries(),
                          key=operator.attrgetter('name')):
            table.append([ library.id,
                           library.name,
                           library.creation_time.strftime('%m/%d/%Y'),
                           library.last_sync_time.strftime('%m/%d/%Y'),
                           library.subscription_info.subscription_url])
            if contentLibraryName is not None and library.name == contentLibraryName:
                table[-1][1]+=" **"

        headers = ['LibraryId', 'Name', 'Created',
                   'Synced', 'SubscriptionURL']

        print('\n'+tabulate(table, headers))

    def getContentLibraries(self, contentLibraryName=None):

        contentLibraries = []
        for libraryID in self.subscribed_library_stub.list():
            library = self.subscribed_library_stub.get(libraryID)
            if contentLibraryName is None or library.name == contentLibraryName:
                contentLibraries.append(library)

        return contentLibraries

    def mountContentLibrary(self, contentLibraryName=None, datastoreName=None, 
                            subscriptionURL=None, sslThumbprint=None):

        if contentLibraryName is None:
            contentLibraryName = self.org.config['WorkshopConfig']['ContentLibraryName']
        if datastoreName is None:
            datastoreName = self.org.config['WorkshopConfig']['Datastore']
        if subscriptionURL is None:
            subscriptionURL = self.org.config['WorkshopConfig']['ContentLibraryURL']
        if sslThumbprint is None:
            sslThumbprint = self.org.config['WorkshopConfig']['sslThumbprint']

        print('  {} mounting content library: {} {} {}'.format(
            self.sddc.sddc.name,
            contentLibraryName,
            datastoreName,
            subscriptionURL
        ))

        datastore = self.getDatastore(datastoreName)._moId

        storageBackings = []
        storageBacking = StorageBacking(
            type=StorageBacking.Type.DATASTORE,
            datastore_id=datastore)
        storageBackings.append(storageBacking)

        createSpec = LibraryModel()
        createSpec.name = contentLibraryName
        createSpec.description = "Subscribed library backed by VC datastore"
        createSpec.type = createSpec.LibraryType.SUBSCRIBED
        createSpec.storage_backings = storageBackings
        createSpec.subscription_info = SubscriptionInfo(
            authentication_method=SubscriptionInfo.AuthenticationMethod('NONE'),
            automatic_sync_enabled=True,
            on_demand=True,
            ssl_thumbprint=sslThumbprint,
            subscription_url=subscriptionURL
        )

        return self.subscribed_library_stub.create(createSpec)

    def dismountContentLibrary(
        self,
        contentLibraryName=None):

        if contentLibraryName is None:
            contentLibraryName = self.org.config['WorkshopConfig']['ContentLibraryName']

        for library in self.getContentLibraries(contentLibraryName):
        
            print('  {} dismounting content library: {} {}'.format(
                self.sddc.sddc.name,
                library.id,
                library.name))

            self.subscribed_library_stub.delete(library.id)

    def deployVM(self,
        sddcName=None, 
        templateName='centos_master',
        vmName='centos',
        datastoreName='WorkloadDatastore', 
        resourcePoolName='Compute-ResourcePool', 
        folderName='Workloads',
		ipAddress='192.168.2.4',
		subnetMask='255.255.255.0',
		gateway='192.168.2.1'):

        if not sddcName:
            raise ValueError('You must supply an SDDC name')
        if not templateName:
            raise ValueError('You must supply a Template name')
        if not vmName:
            raise ValueError('You must supply a VM name')
        if not datastoreName:
            raise ValueError('You must supply a Datastore name')
        if not resourcePoolName:
            raise ValueError('You must supply a Resource Pool name')
        if not folderName:
            raise ValueError('You must supply a Folder name')

        #podNumber = re.sub(r'^[^0-9]*(.*)',r'\1',sddcName)
        datastore = self.getDatastore(datastoreName)._moId
        resourcePool = self.getResourcePool(resourcePoolName)._moId
        folder = self.getFolder(folderName)._moId

        deploymentTarget = LibraryItem.DeploymentTarget(
            resource_pool_id=resourcePool, folder_id=folder)
        findSpec = Item.FindSpec(name=templateName)
        libraryItemService = Item(self.stub_config)
        ovfLibraryItemService = LibraryItem(self.stub_config)
        itemIDs = libraryItemService.find(findSpec)
        libItemID = itemIDs[0] if itemIDs else None
        print('Library item ID: {0}'.format(libItemID))

        ovfSummary = ovfLibraryItemService.filter(
            ovf_library_item_id=libItemID,
            target=deploymentTarget)
        print('Found an OVF template: {0} to deploy.'.format(ovfSummary.name))

        adaptermap = vim.vm.customization.AdapterMapping()
        adaptermap.adapter = vim.vm.customization.IPSettings(
			ip=vim.vm.customization.FixedIp(ipAddress=ipAddress),
            subnetMask=subnetMask,
            gateway=gateway)
        globalip = vim.vm.customization.GlobalIPSettings(
            dnsServerList=self.org.config['WorkshopConfig']['DnsConfig'])
        ident = vim.vm.customization.LinuxPrep(
            domain='domain.local',
            hostName=vim.vm.customization.FixedName(name=vmName))
        customspec = vim.vm.customization.Specification(
            nicSettingMap=[adaptermap],
            globalIPSettings=globalip,
            identity=ident)

        deploymentSpec = LibraryItem.ResourcePoolDeploymentSpec(
            name='centos',
            annotation=ovfSummary.annotation,
            accept_all_eula=True,
            network_mappings=None,
            storage_mappings=None,
            storage_provisioning=None,
            storage_profile_id=None,
            locale=None,
            flags=None,
            additional_parameters=None,
            default_datastore_id=None)
        result = ovfLibraryItemService.deploy(
            libItemID,
            deploymentTarget,
            deploymentSpec,
            client_token=str(uuid.uuid4()))

        # The type and ID of the target deployment is available in the deployment result.
        if result.succeeded:
            print('Deployment successful. Result resource: {0}, ID: {1}'
                  .format(result.resource_id.type, result.resource_id.id))
            vm_id = result.resource_id.id
            error = result.error
            if error is not None:
                for warning in error.warnings:
                    print('OVF warning: {}'.format(warning.message))

            # Power on the VM and wait for the power on operation to be completed
            vm_obj = None
            container = self.content.viewManager.CreateContainerView(
                self.content.rootFolder,
                [vim.VirtualMachine],
                True)
            for c in container.view:
                if c._GetMoId() == vm_id:
                    vm_obj = c
                    break

            assert vm_obj is not None
            self.wait_for_tasks(self.content, [vm_obj.Customize(spec=customspec)])
            self.wait_for_tasks(self.content, [vm_obj.PowerOn()])

        else:
            print('Deployment failed.')
            for error in result.error.errors:
                print('OVF error: {}'.format(error.message))

    def wait_for_tasks(self, content, tasks):
        """
        Given the tasks, it returns after all the tasks are complete
        """
        taskList = [str(task) for task in tasks]

        # Create filter
        objSpecs = [vmodl.query.PropertyCollector.ObjectSpec(obj=task) for task in
                tasks]
        propSpec = vmodl.query.PropertyCollector.PropertySpec(type=vim.Task,
                                                          pathSet=[], all=True)
        filterSpec = vmodl.query.PropertyCollector.FilterSpec()
        filterSpec.objectSet = objSpecs
        filterSpec.propSet = [propSpec]
        task_filter = content.propertyCollector.CreateFilter(filterSpec, True)

        try:
            version, state = None, None

            # Loop looking for updates till the state moves to a completed state.
            while len(taskList):
                update = content.propertyCollector.WaitForUpdates(version)
                for filterSet in update.filterSet:
                    for objSet in filterSet.objectSet:
                        task = objSet.obj
                        for change in objSet.changeSet:
                            if change.name == 'info':
                                state = change.val.state
                            elif change.name == 'info.state':
                                state = change.val
                            else:
                                continue

                            if not str(task) in taskList:
                                continue

                            if state == vim.TaskInfo.State.success:
                                # Remove task from taskList
                                taskList.remove(str(task))
                            elif state == vim.TaskInfo.State.error:
                                raise task.info.error
                # Move to next version
                version = update.version
        finally:
            if task_filter:
                task_filter.Destroy()

from vmware.vapi.lib.rest import OperationRestMetadata
from vmware.vapi.data.serializers.rest import RestSerializer
from vmware.vapi.data.value import StructValue, StringValue

class dict2class(dict):
    def __init__(self, dic):
        for key,val in dic.items():
            self.__dict__[key]=self[key]=dict2class(val) if isinstance(val,dict) else val

