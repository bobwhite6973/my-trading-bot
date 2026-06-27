import time
from .base_module import BaseModule

class RiskManagerModule(BaseModule):
    def __init__(self,cfg):
        super().__init__(cfg,'RiskManager')
        self.max_pos=self.get_float('max_position_usd',500.0)
        self.max_loss=self.get_float('max_daily_loss_usd',200.0)
        self._daily_loss=0.0; self._day=time.time()
    def on_start(self): self.log('Max pos: $'+str(self.max_pos)+' | Cap: $'+str(self.max_loss))
    def approve(self,size_usd):
        if time.time()-self._day>=86400: self._daily_loss=0.0; self._day=time.time()
        if size_usd>self.max_pos: self.log('BLOCKED size',level='WARN'); return False
        if self._daily_loss>=self.max_loss: self.log('BLOCKED daily cap',level='WARN'); return False
        return True
    def record_loss(self,amt): self._daily_loss+=amt