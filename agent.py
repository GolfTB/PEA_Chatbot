from os.path import join
import os
import paho.mqtt.client as mqtt
import threading
import json
import time
import socket
from dotenv import load_dotenv

operator = "llm"

class Agent():
    def __init__(self, topic, host, username, password, port=1883):  
        self.host = host
        self.port = int(port) if port else 1883
        self.topic = topic
        self.username = username
        self.password = password
        self.workername = socket.gethostname()
        self.client = mqtt.Client()
        if self.username and self.password:
            self.client.username_pw_set(self.username, self.password)
            

    def on_connect(self, client, userdata, flags, rc):
        print("mqtt connect♪♫")
        self.client.subscribe("#")
        pass
    
    def pub(self, topic, msg):
        self.client.publish(topic,payload=msg,qos=0)

    def on_publish(self, topic):
        pass

    def online(self, on_message):
        print("quitto clinet online")
        self.client = mqtt.Client()
        self.client.on_connect = self.on_connect
        self.client.on_message = on_message
        self.client.username_pw_set(username = self.username, password = self.password)
        self.client.connect(self.host, self.port)
        self.client.loop_forever()


def on_message(client, userdata, message):
    _topic = message.topic
    print("to: " + _topic)
    if _topic == operator:
        print("Module Process")
        msg = message.payload.decode("utf-8", "strict")


# def main():
#     agent = Agent(topic="listener", host= self.host)
#     thread = threading.Thread(target=agent.online, args=(on_message,))
#     thread.start()
#     time.sleep(3)
#     _online = {"frm" : agent.workername, "to" : "control", "topic" :"info" , "content" : {"title" : "online" ,"msg" : agent.workername + " online"}}
#     agent.pub("app", json.dumps(_online))


# if __name__  == "__main__":
#     print("test local quitto")
#     main()


        
