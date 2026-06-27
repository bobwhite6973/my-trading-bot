import json,urllib.request
from .base_module import BaseModule

class TelegramAlertsModule(BaseModule):
    def __init__(self,cfg):
        super().__init__(cfg,'TelegramAlerts')
        self.token=self.get('tg_bot_token','')
        self.chat_id=self.get('tg_chat_id','')
    def on_start(self):
        if self.token: self.send('Bot started - CTO.New Factory')
        else: self.log('No token - local mode',level='WARN')
    def send(self,msg):
        if not self.token: self.log('(local) '+msg); return
        url='https://api.telegram.org/bot'+self.token+'/sendMessage'
        data=json.dumps({'chat_id':self.chat_id,'text':msg,'parse_mode':'HTML'}).encode()
        urllib.request.urlopen(urllib.request.Request(url,data=data,headers={'Content-Type':'application/json'}),timeout=5)
    def alert_trade(self,action,token,price,amount):
        em='🟢' if action in('buy','open') else '🔴'
        self.send(em+' '+action.upper()+' '+token+' @ '+str(price))