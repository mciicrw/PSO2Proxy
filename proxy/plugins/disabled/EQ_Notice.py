# -*- coding: utf-8 -*-

import config
import data.clients
import plugins
import packetFactory
from ShipProxy import ShipProxy
import json
import os.path
import time
import data.clients as clients
from datetime import datetime, timedelta
from pprint import pformat
from twisted.internet import task, reactor, defer, protocol
from twisted.internet.protocol import Protocol
from twisted.web.client import Agent
# Do not use twisted.web.client.readBody, we need 13.2.0 for that
from twisted.web.http_headers import Headers
from config import globalConfig

from commands import Command

eqnotice_config = config.YAMLConfig("cfg/EQ_Notice.config.yml", {'enabled': True, 'timer' : 60
,'1': "http://acf.me.uk/Public/PSO2EQ/pso2eq.txt"
},  True)

#HTTP Headers
ETag_Headers     = ['','','','','','','','','','']
Modified_Headers = ['','','','','','','','','','']
#HTTP Modified in time
Modified_time    = ['','','','','','','','','','']
#HTTP Data
HTTP_Data        = ['','','','','','','','','','']
#was "【1時間前】" in the data?
ishour_eq        = [False,False,False,False,False,False,False,False,False,False]
#Hour of EQ
hour_eq          = ['','','','','','','','','','']
#Mins of EQ
mins_eq          = ['','','','','','','','','','']
#EQ Data
data_eq          = ['','','','','','','','','','']
#EQ Data
msg_eq           = ['','','','','','','','','','']

eqJP             = []

eq_mode = eqnotice_config.get_key('enabled')
tasksec = eqnotice_config.get_key('timer')

agent = Agent(reactor)

#Sample:        Ship02 08時00分【PSO2】第三採掘基地ダーカー接近予告
#Sample:        【1時間前】 Ship02 05時00分【PSO2】惑星リリーパ　作戦予告
#Sample:        【1時間前】 Ship02 11時00分【PSO2】旧マザーシップ　作戦予告
#Sample:        Ship02 11時00分【PSO2】旧マザーシップ　作戦予告

def load_eqJP_names():
    global eqJP
    RESloaded = False
    if os.path.exists("cfg/eqJP_custom.resources.json"):
        RESloaded = True
        f = open("cfg/eqJP_custom.resources.json", 'r')
        try:
            eqJP = json.load(f, "utf-8")
        except ValueError:
            print "[EQ Notice] Bad custom resource file, falling back"
            RESloaded = False
        f.close()
    if not RESloaded and os.path.exists("cfg/eqJP.resources.json"):
        f = open("cfg/eqJP.resources.json", 'r')
        try:
            eqJP = json.load(f, "utf-8")
        except ValueError:
            print "[EQ Notice] Bad resources file, blank mapping"
        f.close()
    if not eqJP:
        return
    for ship in config.globalConfig.get_key('enabledShips'):
        eqJPd = eqJP.get(data_eq[ship])
        if eqJPd: # Is there a mapping?
            msg_eq[ship] = "%s (JP: %s@%s:%s JST)" % (eqJPd, data_eq[ship], hour_eq[ship], mins_eq[ship])
        else:
            msg_eq[ship] = "JP: %s@%s:%s JST" % (data_eq[ship], hour_eq[ship], mins_eq[ship])



def cutup_EQ(message, ship = 0):
    cutstr = u"分【PSO2】"
    cutlen = message.rfind(cutstr)
    if (cutlen is not -1):
        message = message[cutlen+7:len(message)]
    return message

def ishour_EQ(message):
    hourstr = u"【1時間前】 Ship"
    if message.find(hourstr) is not -1:
        return True
    return False

def findhour_EQ(message):
    hrstr = u"時"
    hridx = message.rfind(hrstr)
    if hridx is -1:
        return ""
    return message[hridx-2:hridx]

def findmins_EQ(message):
    hrstr = u"分"
    hridx = message.find(hrstr)
    if hridx is -1:
        return ""
    return message[hridx-2:hridx]

def cleanup_EQ(message, ship): # 0 is ship1
    ishour_eq[ship] = ishour_EQ(message)
    hour_eq[ship] = findhour_EQ(message)
    mins_eq[ship] = findmins_EQ(message)
    return cutup_EQ(message).rstrip("\n")

def old_seconds(td):
    return (td.seconds + td.days * 24 * 3600)

def checkold_EQ(ship):
    if not Modified_Headers[ship] :
        return False
    timediff = (datetime.utcnow() - Modified_time[ship])
    if ishour_eq[ship]:
        if old_seconds(timediff) > 55*60:
            #print "EQ is 55 mins too old"
            return True
    else:
        # Short EQ notice is no good
        return True
        if old_seconds(timediff) > 10*60:
            #print "Short EQ is 10 mins too old"
            return True
    return False

def EQBody(body, ship): # 0 is ship1
    if HTTP_Data[ship] == body:
        return; # same data, do not react on it
    HTTP_Data[ship] == body

    data_eq[ship] = cleanup_EQ(unicode(body, 'utf-8-sig', 'replace'), ship)

    if checkold_EQ(ship):
        return

    load_eqJP_names() # Reload file

    print("[EQ Notice] Sending players MSG on Ship %02d : %s" % (ship+1, msg_eq[ship]))
    SMPacket = packetFactory.SystemMessagePacket("[Proxy] Incoming EQ Report from PSO2es: %s" % (msg_eq[ship]), 0x0).build()
    for client in data.clients.connectedClients.values():
        chandle = client.get_handle()
        if isinstance(chandle, ShipProxy) and client.preferences.get_preference('eqnotice') \
            and (ship == data.clients.get_ship_from_port(chandle.transport.getHost().port)-1):
            client.get_handle().send_crypto_packet(SMPacket)


class SimpleBodyProtocol(protocol.Protocol):
    def __init__(self, status, message, deferred):
        self.deferred = deferred
        self.dataBuffer = []

    def dataReceived(self, data):
        self.dataBuffer.append(data)

    def connectionLost(self, reason):
        self.deferred.callback( b''.join(self.dataBuffer))

def SimplereadBody(response):
    d = defer.Deferred()
    response.deliverBody(SimpleBodyProtocol(response.code, response.phrase, d))
    return d

def EQResponse(response, ship = -1): # 0 is ship1
    #print pformat(list(response.headers.getAllRawHeaders()))
    if response.code != 200:
        return
    #print response.code
    if response.headers.hasHeader('ETag'):
        ETag_Headers[ship] = response.headers.getRawHeaders('ETag')[0]
    else:
        ETag_Headers[ship] = None
    if response.headers.hasHeader('Last-Modified'):
        Modified_Headers[ship] = response.headers.getRawHeaders('Last-Modified')[0]
        Modified_time[ship] = datetime.strptime(Modified_Headers[ship], "%a, %d %b %Y %H:%M:%S %Z")
    else:
        Modified_Headers[ship] = None
        Modified_time[ship] = None
    d = SimplereadBody(response)
    d.addCallback(EQBody, ship)
    return d

def CheckupURL():
    HTTPHeader0 = Headers({'User-Agent': ['PSO2Proxy']})
    load_eqJP_names() # Reload file
    for shipNum in config.globalConfig.get_key('enabledShips'):
        if eqnotice_config.key_exists(str(shipNum)):
            eq_URL = eqnotice_config.get_key(str(shipNum))
        else:
            eq_URL = None
        if eq_URL:
            HTTPHeaderX = HTTPHeader0.copy()
            if ETag_Headers[shipNum]:
                HTTPHeaderX.addRawHeader('If-None-Match', ETag_Headers[shipNum])
            if Modified_Headers[shipNum]:
                HTTPHeaderX.addRawHeader('If-Modified-Since', Modified_Headers[shipNum])
            #print pformat(list(HTTPHeaderX.getAllRawHeaders()))
            EQ0 = agent.request('GET', eq_URL, HTTPHeaderX, None)
            EQ0.addCallback(EQResponse, shipNum)

@plugins.on_start_hook
def on_start():
    global taskrun
    taskrun = task.LoopingCall(CheckupURL)
    if eq_mode:
        taskrun.start(tasksec) # call every 60 seconds
    else:
        print("EQ Notice is disabled by config")

@plugins.on_initial_connect_hook
def notify_and_config(client):
    """
    :type client: ShipProxy.ShipProxy
    """
    client_preferences = data.clients.connectedClients[client.playerId].preferences
    if not client_preferences.has_preference("eqnotice"):
        client_preferences.set_preference("eqnotice", True)
    ship = data.clients.get_ship_from_port(client.transport.getHost().port)-1
    if client_preferences.get_preference('eqnotice') and data_eq[ship] and not checkold_EQ(ship):
        SMPacket = packetFactory.SystemMessagePacket("[Proxy] Incoming EQ Report from PSO2es: %s" % (msg_eq[ship]), 0x0).build()
        client.send_crypto_packet(SMPacket)

@plugins.CommandHook("eqnotice", "Toggles display of EQ notices from PSO2es sources")
class ToggleTranslate(Command):
    def call_from_client(self, client):
        if client.playerId in data.clients.connectedClients:
            user_prefs = data.clients.connectedClients[client.playerId].preferences
            user_prefs.set_preference('eqnotice', not user_prefs.get_preference('eqnotice'))
            if user_prefs.get_preference('eqnotice'):
                client.send_crypto_packet(packetFactory.SystemMessagePacket("[EQ Notice] Enabled EQ notices.", 0x3).build())
            else:
                client.send_crypto_packet(packetFactory.SystemMessagePacket("[EQ Notice] Disabled EQ notices.", 0x3).build())

    def call_from_console(self):
        if taskrun.running:
            taskrun.stop()
            print("[EQ Notice] Stop EQ Notice Ticker")
        else:
            taskrun.start(tasksec)
            print("[EQ Notice] Started EQ Notice Ticker")
