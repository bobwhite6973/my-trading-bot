import uuid
from .base_module import BaseModule

class LimitOrdersModule(BaseModule):
    def __init__(self,cfg):
        super().__init__(cfg,'LimitOrders')
        self.slippage=self.get_float('slippage_pct',1.0)/100
        self.orders={}
    def place(self,token,price,amount,side='buy'):
        oid=str(uuid.uuid4())[:8]
        o={'id':oid,'token':token,'price':price,'amount':amount,'side':side,'status':'open'}
        self.orders[oid]=o; return o
    def tick(self,token,price):
        for o in self.orders.values():
            if o['status']!='open' or o['token']!=token: continue
            if abs(price-o['price'])/o['price']<=self.slippage:
                o['status']='filled'; self.emit('filled',o)